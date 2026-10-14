"""
Multi-site job scraping without AI-dependent filtering.

Scrapes jobs from all four supported sites using the user's target_role
as the search keyword, extracts structured fields deterministically,
and saves all jobs to the database (users filter by role in the dashboard).
"""

import logging

from .models import JobPosting
from .extraction import extract_job_fields
from .matching import score_job
from .telegram_notify import send_telegram_alert
from .scraper import scrape_internshala
from .naukri_scraper import scrape_naukri
from .foundit_scraper import scrape_foundit
from .workindia_scraper import scrape_workindia

MATCH_ALERT_THRESHOLD = 70

SITE_SCRAPERS = {
    "internshala": scrape_internshala,
    "naukri": scrape_naukri,
    "foundit": scrape_foundit,
    "workindia": scrape_workindia,
}

logger = logging.getLogger(__name__)


def _site_label(site: str) -> str:
    return (site or "unknown").title()


def run_scrape(user, sites=None, max_results_per_site=5) -> dict:
    """
    Scrapes the given sites (defaults to all four) using the user's
    Profile.target_role as the search keyword.
    
    All jobs with extractable title/company are saved (no role filtering).
    Users can filter by role category in the dashboard.
    """
    profile = getattr(user, "profile", None)
    keyword = (profile.target_role if profile and profile.target_role else "software engineer")

    sites = sites or list(SITE_SCRAPERS.keys())

    summary = {
        "keyword": keyword,
        "sites": {site: 0 for site in SITE_SCRAPERS.keys()},
        "added": 0,
        "skipped_duplicate": 0,
        "failed_extraction": 0,
        "errors": [],
    }


    for site in sites:
        scraper_fn = SITE_SCRAPERS.get(site)
        if scraper_fn is None:
            summary["errors"].append(f"Unknown site '{site}', skipped.")
            continue

        site_added = 0
        try:
            scraped = scraper_fn(keyword=keyword, max_results=max_results_per_site)
            scraped = scraped or []
            if not isinstance(scraped, list):
                raise TypeError(f"Scraper returned {type(scraped).__name__}, expected list")
        except Exception as e:
            summary["errors"].append(f"[{_site_label(site)}] FAILED: {e}")
            logger.exception("[%s] FAILED: %s", _site_label(site), e)
            summary["sites"][site] = 0
            continue

        print(f"[{_site_label(site)}] Found {len(scraped)} jobs")
        for item in scraped:
            if not isinstance(item, dict):
                summary["errors"].append(f"[{_site_label(site)}] Invalid job payload: {item!r}")
                continue

            title = (item.get("title") or "").strip()
            company = (item.get("company") or "").strip()
            raw_text = (item.get("raw_text") or "").strip()

            if not raw_text:
                summary["errors"].append(f"[{_site_label(site)}] Empty raw_text for '{title or 'unknown'}'")
                continue

            if not title or not company:
                summary["errors"].append(f"[{_site_label(site)}] Missing title/company: {title!r} / {company!r}")
                continue

            already_exists = JobPosting.objects.filter(
                title__iexact=title, company__iexact=company
            ).exists()
            if already_exists:
                summary["skipped_duplicate"] += 1
                continue

            try:
                extracted = extract_job_fields(raw_text)
            except (ValueError, RuntimeError) as e:
                summary["failed_extraction"] += 1
                summary["errors"].append(f"[{_site_label(site)}] Extraction failed for '{title}': {e}")
                logger.exception("[%s] Extraction failed for %s", _site_label(site), title)
                continue

            title_value = (extracted.get("title") or "").strip()
            company_value = (extracted.get("company") or "").strip()
            if not title_value or not company_value:
                summary["failed_extraction"] += 1
                summary["errors"].append(
                    f"[{_site_label(site)}] Missing title/company after extraction: '{title_value}' / '{company_value}'"
                )
                continue

            # Extract job - all jobs are saved (no role filtering)
            job = JobPosting.objects.create(
                raw_text=raw_text,
                source=site,
                title=title_value,
                company=company_value,
                location=(extracted.get("location") or "").strip(),
                skills_required=extracted.get("skills_required") or [],
                requirements=extracted.get("requirements") or [],
                salary_range=(extracted.get("salary_range") or "").strip(),
                deadline=extracted.get("deadline") or None,
                role_category=extracted.get("role_category") or "other",
            )
            job.match_score = score_job(job, user)
            job.save()

            summary["added"] += 1
            site_added += 1

            if job.match_score >= MATCH_ALERT_THRESHOLD:
                send_telegram_alert(job)

        summary["sites"][site] = site_added

    print(summary)
    return summary
