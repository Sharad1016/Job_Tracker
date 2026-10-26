"""Internshala scraper."""

from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright

from .jsonld_extraction import extract_json_ld_jobpostings
from .detail_page_extraction import fetch_detail_page

INTERNSHALA_BASE_URL = "https://internshala.com"
INTERNSHALA_SEARCH_URL = "https://internshala.com/jobs/{keyword}-jobs"

# Phase 16: cap on how many detail pages get fetched AT THE SAME TIME
# within one site's Stage 2. Each concurrent fetch opens its own
# complete, independent Playwright browser (see
# detail_page_extraction.fetch_detail_page) - 4 at once is a deliberate
# middle ground between speed and not launching e.g. 8 full browsers
# simultaneously just for one site.
MAX_DETAIL_CONCURRENCY = 4


def _clean_text(value):
    return " ".join(str(value or "").split())


def scrape_internshala(keyword: str = "python", max_results: int = 10, known_pairs: set = None) -> list[dict]:
    """
    Two-stage scraper (Phase 15):
      Stage 1 - visit the listing/search page, read each job CARD for
                title/company/location/stipend, and capture that card's
                own detail-page URL.
      Stage 2 - visit each job's individual detail page and pull the
                REAL description + explicit skill tags from there (see
                detail_page_extraction.py) - the listing page never has
                this, only short card summaries.
    A detail page failing (403, timeout, selector miss) only drops that
    one job's extra detail - it does NOT stop the run; the job still
    gets returned with whatever Stage 1 found.

    known_pairs: optional set of (title.lower(), company.lower()) tuples
    already in the database (see run_scrape()). A card matching one of
    these skips its Stage 2 detail-page visit entirely - it's already
    saved with full detail, so re-fetching would be wasted work.
    """
    known_pairs = known_pairs or set()
    results = []
    url = INTERNSHALA_SEARCH_URL.format(keyword=keyword.strip().lower().replace(" ", "-"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 2200})
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        # Phase 12: capture any Schema.org JobPosting JSON-LD present on
        # the LISTING page - often empty here (see jsonld_extraction.py);
        # Stage 2 below also checks each DETAIL page for JSON-LD, which is
        # far more likely to actually have it.
        listing_json_ld = extract_json_ld_jobpostings(page.content())

        card_selector = ".individual_internship, .internship-card, article, [data-job-id]"
        cards = page.locator(card_selector).all()[:max_results]

        # --- Stage 1: listing page cards ---
        for card in cards:
            title = ""
            detail_url = ""
            for selector in [".job-title-href", ".heading_4_5 a", "h3 a", "h2 a", "a[href*='internship']", "a[href*='job']"]:
                link = card.locator(selector).first
                if link.count() and link.inner_text().strip():
                    title = _clean_text(link.inner_text())
                    href = link.get_attribute("href") or ""
                    if href:
                        detail_url = urljoin(INTERNSHALA_BASE_URL, href)
                    break

            company = ""
            for selector in [".company-name", ".company", ".company_name", ".employer-name", ".company_name_text"]:
                text = card.locator(selector).first
                if text.count() and text.inner_text().strip():
                    company = _clean_text(text.inner_text())
                    break

            location = ""
            for selector in [".location", ".locations", ".location_name", ".internship_location"]:
                el = card.locator(selector).first
                if el.count() and el.inner_text().strip():
                    location = _clean_text(el.inner_text())
                    break

            stipend = ""
            for selector in [".stipend", ".salary", ".pay", ".internship_details a"]:
                el = card.locator(selector).first
                if el.count() and el.inner_text().strip():
                    stipend = _clean_text(el.inner_text())
                    break

            if not title or not company:
                continue

            raw_text_parts = [
                f"Role: {title}",
                f"Company: {company}",
            ]
            if location:
                raw_text_parts.append(f"Location: {location}")
            if stipend:
                raw_text_parts.append(f"Stipend: {stipend}")

            # Short card-level summary text, kept as a fallback in case
            # the Stage 2 detail-page visit below fails for this job.
            details = ""
            for selector in [".internship_other_details_container", ".details", ".job-details", ".description"]:
                el = card.locator(selector).first
                if el.count() and el.inner_text().strip():
                    details = _clean_text(el.inner_text())
                    break
            if details:
                raw_text_parts.append(details)

            results.append({
                "title": title,
                "company": company,
                "raw_text": "\n".join(raw_text_parts),
                "json_ld": listing_json_ld,
                "application_url": detail_url,
            })

        # --- Stage 2 (Phase 16): each job's own detail page, fetched
        # CONCURRENTLY - up to MAX_DETAIL_CONCURRENCY (4) at once, not
        # all of them simultaneously. ThreadPoolExecutor(max_workers=4)
        # naturally keeps at most 4 in flight at any moment: as soon as
        # one finishes, the next queued job starts - a rolling window
        # capped at 4, rather than a strict "wait for all 4, then start
        # the next 4" barrier. This never exceeds the 4-at-once limit
        # and is more efficient than forced batching (a slow job 2
        # doesn't block job 5 from starting once job 1 finishes).
        to_fetch = []
        for item in results:
            pair = (item["title"].strip().lower(), item["company"].strip().lower())
            if pair in known_pairs:
                print(f"[Internshala] Skipping detail page for already-saved job: {item['title']} at {item['company']}")
                continue
            if item.get("application_url"):
                to_fetch.append(item)

        if to_fetch:
            with ThreadPoolExecutor(max_workers=min(MAX_DETAIL_CONCURRENCY, len(to_fetch))) as executor:
                future_to_item = {
                    executor.submit(fetch_detail_page, item["application_url"]): item
                    for item in to_fetch
                }
                for future in as_completed(future_to_item):
                    item = future_to_item[future]
                    detail_url = item["application_url"]
                    result = future.result()  # fetch_detail_page() never raises - always a dict

                    if result["error"]:
                        print(f"[Internshala] Detail page failed for {detail_url}: {result['error']}")
                        continue

                    print(f"[Internshala] Fetching detail page: {detail_url}")

                    if result["json_ld"]:
                        item["json_ld"] = result["json_ld"]  # detail-page JSON-LD is more reliable than the listing page's
                    if result["skills"]:
                        item["raw_text"] += "\nSkills: " + ", ".join(result["skills"])
                    if result["description"]:
                        item["raw_text"] += "\n" + result["description"]

                    print(
                        f"[Internshala] Detail page fetched: {bool(result['description'] or result['skills'])} | "
                        f"Description length: {len(result['description'])} | Skills found: {result['skills'] or 'none'}"
                    )

        browser.close()

    return results
