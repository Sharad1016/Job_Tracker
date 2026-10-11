"""
Naukri.com scraper - same interface/pattern as scrape_internshala() in
scraper.py, returning [{"title", "company", "raw_text"}, ...] for the
shared extract_job_fields() pipeline to process.

IMPORTANT - UNVERIFIED AGAINST THE LIVE SITE: same situation as the
original Internshala scraper before it was tested for real. This
sandbox's network cannot reach naukri.com, so these selectors are
written from general knowledge of Naukri's page structure, not
confirmed against the live DOM. Naukri also has real anti-bot
measures (more than Internshala) - expect this to need iteration:
1. Run it once with a low --max-results.
2. If wait_for_selector times out, open the search URL in your own
   browser, inspect a job card with DevTools, and send the actual
   class names back so the selectors below can be corrected.
3. If Naukri blocks headless Chromium outright (some anti-bot systems
   do), launching with headless=False, or adding a real User-Agent
   header, are the next things to try - not attempted here since it
   can't be tested from this sandbox either way.
"""

from playwright.sync_api import sync_playwright

NAUKRI_SEARCH_URL = "https://www.naukri.com/{keyword}-jobs"


def scrape_naukri(keyword: str = "software engineer", max_results: int = 10) -> list[dict]:
    """Returns a list of dicts: [{"title": ..., "company": ..., "raw_text": ...}, ...]"""
    results = []
    url = NAUKRI_SEARCH_URL.format(keyword=keyword.strip().lower().replace(" ", "-"))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ))
        page.goto(url, timeout=20000)
        # Naukri has used both "cust-job-tuple" and "jobTuple" as the
        # card wrapper class across different site versions - try both.
        page.wait_for_selector(".cust-job-tuple, .jobTuple", timeout=15000)

        cards = page.query_selector_all(".cust-job-tuple, .jobTuple")[:max_results]

        for card in cards:
            title_el = card.query_selector("a.title, .title a")
            company_el = card.query_selector("a.comp-name, .comp-name, .companyInfo .subTitle")
            location_el = card.query_selector(".locWdth, .loc-wrap, .location")
            experience_el = card.query_selector(".exp-wrap, .experience")
            skills_el = card.query_selector(".tags-gt, .tags")

            title = title_el.inner_text().strip() if title_el else ""
            company = company_el.inner_text().strip() if company_el else ""

            if not title or not company:
                continue

            raw_text_parts = [
                f"Role: {title}",
                f"Company: {company}",
            ]
            if location_el:
                raw_text_parts.append(f"Location: {location_el.inner_text().strip()}")
            if experience_el:
                raw_text_parts.append(f"Experience: {experience_el.inner_text().strip()}")
            if skills_el:
                raw_text_parts.append(f"Skills: {skills_el.inner_text().strip()}")

            results.append({
                "title": title,
                "company": company,
                "raw_text": "\n".join(raw_text_parts),
            })

        browser.close()

    return results
