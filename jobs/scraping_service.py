"""
Phase 11 (multi-site + role-filtered scraping): orchestrates scraping
across all four supported sites, using the SAME extract_job_fields()
pipeline every other job-add path uses, then filters out anything
whose AI-classified role_category doesn't match the user's declared
target_role_category.

This function is deliberately the ONE place that knows how to run a
full scrape - both the CLI (`manage.py scrape_jobs`) and the web
button (views.run_scraper, so scraping no longer requires opening a
terminal) call this same function, instead of duplicating the loop.
Matches the project's existing "one shared pipeline" pattern
(extract_job_fields being reused by both paste and scrape).
"""

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


def run_scrape(user, sites=None, max_results_per_site=5) -> dict:
    """
    Scrapes the given sites (defaults to all four) using user's
    Profile.target_role as the search keyword, extracts + scores each
    new listing, and DISCARDS any job whose AI-classified role_category
    doesn't match the user's Profile.target_role_category - e.g. a
    Software Engineering search shouldn't leave Sales/Marketing rows
    sitting in Job Matches just because a keyword happened to overlap.

    Returns a summary dict so callers (CLI command or the web view) can
    report exactly what happened without duplicating this loop.
    """
    profile = getattr(user, "profile", None)
    keyword = (profile.target_role if profile and profile.target_role else "software engineer")
    target_category = profile.target_role_category if profile else "software_engineering"

    sites = sites or list(SITE_SCRAPERS.keys())

    summary = {
        "keyword": keyword,
        "target_category": target_category,
        "sites": {},
        "added": 0,
        "skipped_duplicate": 0,
        "skipped_wrong_role": 0,
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
        except Exception as e:
            # A single site failing (selector timeout, site down, anti-bot
            # block, etc.) shouldn't kill the whole multi-site run - the
            # other sites should still get a chance to contribute jobs.
            summary["errors"].append(f"{site}: {e}")
            summary["sites"][site] = 0
            continue

        for item in scraped:
            already_exists = JobPosting.objects.filter(
                title__iexact=item["title"], company__iexact=item["company"]
            ).exists()
            if already_exists:
                summary["skipped_duplicate"] += 1
                continue

            job = JobPosting.objects.create(raw_text=item["raw_text"], source=site)

            try:
                extracted = extract_job_fields(item["raw_text"])
            except (ValueError, RuntimeError) as e:
                summary["failed_extraction"] += 1
                summary["errors"].append(f"{site}/'{item['title']}': extraction failed: {e}")
                continue  # raw_text row stays saved (save-first pattern), just unpopulated

            # Role filtering: only keep the job if its AI-classified
            # category matches what the user is actually looking for.
            if extracted["role_category"] != target_category:
                job.delete()  # noise from an unrelated field - don't keep it
                summary["skipped_wrong_role"] += 1
                continue

            job.title = extracted["title"]
            job.company = extracted["company"]
            job.location = extracted["location"]
            job.skills_required = extracted["skills_required"]
            job.requirements = extracted["requirements"]
            job.salary_range = extracted["salary_range"]
            job.deadline = extracted["deadline"]
            job.role_category = extracted["role_category"]
            job.match_score = score_job(job, user)
            job.save()

            summary["added"] += 1
            site_added += 1

            if job.match_score >= MATCH_ALERT_THRESHOLD:
                send_telegram_alert(job)  # best-effort, never raises

        summary["sites"][site] = site_added

    return summary
