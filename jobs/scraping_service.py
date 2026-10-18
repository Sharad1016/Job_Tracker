"""
Multi-site scraping orchestrator. As of Phase 12, this no longer calls
Gemini for every scraped job - see extraction_pipeline.py for the
deterministic-first extraction chain. This file's job is just: run
each site's scraper, dedupe, hand each item to the pipeline, apply
role filtering, save, alert.
"""

import logging

from .models import JobPosting
from .matching import score_job
from .telegram_notify import send_telegram_alert
from .scraper import scrape_internshala
from .naukri_scraper import scrape_naukri
from .foundit_scraper import scrape_foundit
from .workindia_scraper import scrape_workindia
from .extraction_pipeline import process_scraped_item, GeminiRunState

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
    Profile.target_role as the search keyword. Each result goes
    through the deterministic-first extraction_pipeline - Gemini is
    only touched when that pipeline genuinely can't recover a required
    field, or for role classification (see extraction_pipeline.py).

    NOTE: role_category is still classified and stored on every job
    here (unchanged), but jobs are NO LONGER discarded at scrape time
    for not matching the scraping user's target_role_category. Every
    structurally valid job gets saved to the shared JobPosting pool
    regardless of who scraped it - role filtering now happens live at
    DISPLAY time instead, per-viewer, in job_matches() (views.py). This
    means the person who happens to click "Fetch New Jobs" doesn't
    determine what role of jobs end up visible to every other account.

    Returns the SAME summary dict shape as before (added,
    skipped_duplicate, skipped_wrong_role, failed_extraction, errors,
    sites, keyword, target_category) so the CLI command, the web
    button, and their templates don't need any changes - plus two new
    additive keys (gemini_calls_made, gemini_disabled_reason) for
    visibility into how much Gemini was actually used this run.
    """
    profile = getattr(user, "profile", None)
    keyword = (profile.target_role if profile and profile.target_role else "software engineer")
    target_category = profile.target_role_category if profile else "software_engineering"

    sites = sites or list(SITE_SCRAPERS.keys())
    # ONE circuit breaker shared across every site/job in THIS run only -
    # a fresh GeminiRunState is created per call, so a quota exhaustion
    # here can never leak into a different request/user later.
    gemini_state = GeminiRunState()

    summary = {
        "keyword": keyword,
        "target_category": target_category,
        "sites": {site: 0 for site in SITE_SCRAPERS.keys()},
        "added": 0,
        "skipped_duplicate": 0,
        "skipped_wrong_role": 0,  # always 0 now - role filtering moved to display time, see job_matches() in views.py
        "failed_extraction": 0,
        "errors": [],
        "gemini_calls_made": 0,
        "gemini_disabled_reason": None,
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
            # A single site failing (timeout, selector drift, anti-bot
            # block, network error) must never stop the other sites.
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

            # Dedup BEFORE the extraction pipeline runs at all - this is
            # also what keeps a job that's already in the DB from ever
            # touching Gemini again (satisfies "avoid repeat AI calls
            # for jobs already processed" without adding new caching
            # infrastructure).
            already_exists = JobPosting.objects.filter(
                title__iexact=title, company__iexact=company
            ).exists()
            if already_exists:
                summary["skipped_duplicate"] += 1
                continue

            job_fields, log_lines = process_scraped_item(item, site, gemini_state)
            for line in log_lines:
                print(line)

            if job_fields is None:
                # One bad job must never stop the run - just count it
                # and move on to the next item/site.
                summary["failed_extraction"] += 1
                summary["errors"].append(f"[{_site_label(site)}] Extraction failed for '{title}' (see log above)")
                continue

            role_category = job_fields.get("role_category") or "other"
            # NOTE: role filtering used to happen HERE at scrape time,
            # permanently discarding jobs that didn't match whoever
            # clicked "Fetch New Jobs". That's been moved to display
            # time (see job_matches() in views.py) so the same shared
            # job pool can show different results to different users
            # based on EACH viewer's own target_role_category, without
            # needing to re-scrape when someone changes their target
            # role. role_category is still stored on every job below -
            # nothing about extraction/classification changed.

            job = JobPosting.objects.create(
                raw_text=job_fields["raw_text"],
                source=site,
                title=job_fields["title"],
                company=job_fields["company"],
                location=job_fields.get("location") or "",
                skills_required=job_fields.get("skills_required") or [],
                requirements=job_fields.get("requirements") or [],
                salary_range=job_fields.get("salary_range") or "",
                deadline=job_fields.get("deadline") or None,
                role_category=role_category,
            )
            job.match_score = score_job(job, user)
            job.save()
            print(f"[Database] Job saved: {job.title} at {job.company}")

            summary["added"] += 1
            site_added += 1

            if job.match_score >= MATCH_ALERT_THRESHOLD:
                send_telegram_alert(job)

        summary["sites"][site] = site_added

    summary["gemini_calls_made"] = gemini_state.calls_made
    summary["gemini_disabled_reason"] = gemini_state.disabled_reason
    print(summary)
    return summary
