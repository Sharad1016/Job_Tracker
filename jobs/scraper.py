"""
Phase 7: pulls job listings from Internshala and hands them to the SAME
extract_job_fields() pipeline the paste form uses (jobs/extraction.py).
This module only produces raw text + a dedup key (title, company) - it
does not do any AI extraction itself, matching the separation of
concerns set up back in Phase 1 (source field, raw_text-first saving).

IMPORTANT: the CSS selectors below are written from Internshala's known
general page structure, but have only been verified against a local
mock fixture (jobs/tests_fixtures/mock_internshala.html), not the live
site - this sandbox's network can't reach internshala.com. Expect to
need to adjust selectors after running this against the real site once,
by inspecting the actual page with your browser's DevTools.
"""

from playwright.sync_api import sync_playwright

INTERNSHALA_SEARCH_URL = "https://internshala.com/internships/{keyword}-internship"


def scrape_internshala(keyword: str = "python", max_results: int = 10) -> list[dict]:
    """
    Returns a list of dicts: [{"title": ..., "company": ..., "raw_text": ...}, ...]

    title/company are pulled directly from the listing card (cheap, no AI
    needed) specifically so callers can deduplicate BEFORE spending an
    AI API call on a job that's already in the database.
    """
    results = []
    url = INTERNSHALA_SEARCH_URL.format(keyword=keyword)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, timeout=20000)
        page.wait_for_selector(".individual_internship", timeout=10000)

        cards = page.query_selector_all(".individual_internship")[:max_results]

        for card in cards:
            title_el = card.query_selector(".job-title-href, .heading_4_5 a")
            company_el = card.query_selector(".company-name")
            location_el = card.query_selector(".locations")
            stipend_el = card.query_selector(".stipend")
            details_el = card.query_selector(".internship_other_details_container")

            title = title_el.inner_text().strip() if title_el else ""
            company = company_el.inner_text().strip() if company_el else ""

            if not title or not company:
                # Skip cards we couldn't parse rather than saving garbage -
                # a partially-broken selector shouldn't crash the whole run.
                continue

            # Rebuild a raw_text blob that looks like what someone would
            # paste by hand - this is what extract_job_fields() expects.
            raw_text_parts = [
                f"Role: {title}",
                f"Company: {company}",
            ]
            if location_el:
                raw_text_parts.append(f"Location: {location_el.inner_text().strip()}")
            if stipend_el:
                raw_text_parts.append(f"Stipend: {stipend_el.inner_text().strip()}")
            if details_el:
                raw_text_parts.append(details_el.inner_text().strip())

            results.append({
                "title": title,
                "company": company,
                "raw_text": "\n".join(raw_text_parts),
            })

        browser.close()

    return results
