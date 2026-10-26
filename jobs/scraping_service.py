"""
Multi-site scraping orchestrator. As of Phase 12, this no longer calls
Gemini for every scraped job - see extraction_pipeline.py for the
deterministic-first extraction chain. This file's job is just: run
each site's scraper, dedupe, hand each item to the pipeline, apply
role filtering, save.

Phase 14: site scraping itself now runs CONCURRENTLY (one thread per
site) instead of one-after-another. This only changes the I/O-bound
scraping step - dedup, extraction, role classification, and saving
still happen afterward, sequentially, in the main thread, completely
unchanged. See _scrape_site() and the "Phase 1" block in run_scrape()
for why that split makes this safe.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .models import JobPosting
from .matching import score_job, match_breakdown, get_profile_for
from .scraper import scrape_internshala
from .workindia_scraper import scrape_workindia
from .extraction_pipeline import process_scraped_item, GeminiRunState

SITE_SCRAPERS = {
    "internshala": scrape_internshala,
    "workindia": scrape_workindia,
}

logger = logging.getLogger(__name__)


def _site_label(site: str) -> str:
    return (site or "unknown").title()


def _scrape_site(site: str, scraper_fn, keyword: str, max_results: int, known_pairs: set = None):
    """
    Runs ONE site's scraper function and nothing else - no DB access,
    no extraction, no Gemini. Meant to be submitted to a
    ThreadPoolExecutor, one call per site, so that WorkIndia and
    Internshala's page loads happen at the same time instead of back
    to back.

    known_pairs is a plain read-only set of (title.lower(), company.lower())
    tuples already in the database, computed ONCE before any thread
    starts (see run_scrape()) - not a live query. It's handed to the
    scraper so its detail-page Stage 2 can skip visiting a job that's
    already saved, rather than fetching a detail page just to have the
    result discarded as a duplicate afterward.

    Thread safety note: scrape_workindia() and scrape_internshala()
    each open their own Playwright browser with their own
    `with sync_playwright() as p:` block INSIDE the function, and
    close it before returning. Nothing about the browser/page/context
    is ever created outside this call or shared with another thread -
    each thread gets a fully independent Playwright instance from
    start to finish. That's what makes running them concurrently via
    threads safe with Playwright's synchronous API; if a browser were
    created in the main thread and handed to workers, that WOULD be
    unsafe - so this function deliberately never does that.

    Returns (site, results_list, error_or_None, duration_seconds).
    Never raises - any exception from the scraper itself is caught
    here so one site's failure can never affect the other's thread.
    """
    start = time.monotonic()
    try:
        scraped = scraper_fn(keyword=keyword, max_results=max_results, known_pairs=known_pairs or set())
        scraped = scraped or []
        if not isinstance(scraped, list):
            raise TypeError(f"Scraper returned {type(scraped).__name__}, expected list")
        duration = time.monotonic() - start
        print(f"{_site_label(site)} scraping completed: {len(scraped)} jobs ({duration:.1f}s)")
        return site, scraped, None, duration
    except Exception as e:
        duration = time.monotonic() - start
        print(f"{_site_label(site)} scraping failed after {duration:.1f}s: {e}")
        return site, [], e, duration


def run_scrape(user, sites=None, max_results_per_site=5) -> dict:
    """
    Scrapes the given sites (defaults to all four) using the user's
    Profile.target_role as the search keyword. Each result goes
    through the deterministic-first extraction_pipeline - Gemini is
    only touched when that pipeline genuinely can't recover a required
    field, or for role classification (see extraction_pipeline.py).

    max_results_per_site accepts EITHER:
      - a plain int - the same limit is used for every site (this is
        the original behavior, kept for backward compatibility with
        existing callers/tests that pass a single number), OR
      - a dict like {"internshala": 8, "workindia": 8} - each site gets
        its OWN limit. This is how views.py now calls it (Phase 16) to
        get an 8+8=16 total instead of one shared number applied to
        every site identically.

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

    def _limit_for(site: str) -> int:
        if isinstance(max_results_per_site, dict):
            return max_results_per_site.get(site, 5)
        return max_results_per_site

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

    # --- Phase 1: scrape every site CONCURRENTLY. This is purely I/O
    # (loading pages, reading HTML) - no DB writes, no extraction, no
    # Gemini calls happen here. Keeping this phase side-effect-free is
    # what guarantees concurrency can't cause duplicate DB rows or
    # extra Gemini calls: all of that still happens in Phase 2, below,
    # one item at a time, in the main thread, exactly as it always did.
    valid_sites = []
    for site in sites:
        scraper_fn = SITE_SCRAPERS.get(site)
        if scraper_fn is None:
            summary["errors"].append(f"Unknown site '{site}', skipped.")
            continue
        valid_sites.append((site, scraper_fn))

    # Phase 15: computed ONCE, up front, as a plain read-only Python set -
    # not a live DB query from inside a worker thread. Handed to each
    # scraper so its Stage 2 (detail-page visits) can skip a job that's
    # already saved, instead of fetching its detail page just to have it
    # thrown away as a duplicate a moment later in Phase 2 below. This
    # can't catch a job that's new-this-run but appears on BOTH sites
    # (that's still deduped normally at save time in Phase 2) - it only
    # covers jobs that were ALREADY in the database before this run started,
    # which is exactly what "already complete, don't re-fetch" means.
    known_pairs = {
        (t.strip().lower(), c.strip().lower())
        for t, c in JobPosting.objects.values_list("title", "company")
    }

    scraped_by_site = {}
    if valid_sites:
        print("Starting concurrent scraping...")
        for site, _ in valid_sites:
            print(f"{_site_label(site)} scraping started")

        overall_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(valid_sites)) as executor:
            futures = [
                executor.submit(_scrape_site, site, scraper_fn, keyword, _limit_for(site), known_pairs)
                for site, scraper_fn in valid_sites
            ]
            for future in as_completed(futures):
                site, scraped, error, duration = future.result()
                if error is not None:
                    summary["errors"].append(f"[{_site_label(site)}] FAILED: {error}")
                    # exc_info=error (not logger.exception()) since the
                    # exception was caught inside the worker thread and
                    # handed back as a value here, not raised in this
                    # frame - there's no active traceback context for
                    # logger.exception() to capture at this point.
                    logger.error("[%s] FAILED: %s", _site_label(site), error, exc_info=error)
                    summary["sites"][site] = 0
                else:
                    scraped_by_site[site] = scraped
        overall_duration = time.monotonic() - overall_start

        total_found = sum(len(v) for v in scraped_by_site.values())
        print("Concurrent scraping completed")
        print(f"Total scraped jobs: {total_found}")
        print(f"Total concurrent scraping duration: {overall_duration:.1f}s")

    # --- Phase 2: process results SEQUENTIALLY, exactly as before this
    # change - dedup, extraction pipeline, role classification, saving.
    # Only the scraping I/O above became concurrent; everything from
    # here down is untouched single-threaded logic.
    for site, scraped in scraped_by_site.items():
        print(f"[{_site_label(site)}] Found {len(scraped)} jobs")
        site_added = 0

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
            # infrastructure). Also covers the same job appearing on
            # BOTH sites: whichever site's item is processed first (in
            # this now-sequential Phase 2 loop) gets saved, and the
            # other site's matching item is caught here as a duplicate,
            # exactly as it would be if both came from one site.
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

            skills_required = job_fields.get("skills_required") or []

            # Phase 15: build the instance UNSAVED first so match_score
            # can be computed from its actual skills_required and set
            # BEFORE the single .save() call - the job should never hit
            # the database showing 0% just because scoring happened in
            # a later step. score_job() only reads job.skills_required
            # (a plain attribute), so this works fine pre-save.
            job = JobPosting(
                raw_text=job_fields["raw_text"],
                source=site,
                title=job_fields["title"],
                company=job_fields["company"],
                location=job_fields.get("location") or "",
                skills_required=skills_required,
                requirements=job_fields.get("requirements") or [],
                salary_range=job_fields.get("salary_range") or "",
                deadline=job_fields.get("deadline") or None,
                application_url=job_fields.get("application_url") or "",
                role_category=role_category,
            )
            job.match_score = score_job(job, user)
            job.save()

            profile_for_log = get_profile_for(user)
            breakdown = match_breakdown(skills_required, profile_for_log.skills if profile_for_log else [])
            print(
                f"[MATCHING] User skills: {profile_for_log.skills if profile_for_log else []} | "
                f"Matched skills: {breakdown['matched']} | Match score: {job.match_score}%"
            )
            print(f"[SAVE] Job saved: {job.title} at {job.company}")

            summary["added"] += 1
            site_added += 1

        summary["sites"][site] = site_added

    summary["gemini_calls_made"] = gemini_state.calls_made
    summary["gemini_disabled_reason"] = gemini_state.disabled_reason
    print(summary)
    return summary
