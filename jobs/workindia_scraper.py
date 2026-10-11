"""
WorkIndia scraper - same interface/pattern as scrape_internshala() in
scraper.py.

IMPORTANT - TWO caveats here, not just one:

1. UNVERIFIED AGAINST THE LIVE SITE, same situation as naukri_scraper.py
   and foundit_scraper.py - selectors are written from general knowledge,
   not confirmed against the live DOM.

2. WorkIndia is primarily a blue-collar / entry-level job platform
   (delivery, retail, telecalling, field sales, etc.), not a software-
   engineering-focused board. It's included here because it was asked
   for, but realistically the role_category filter (see matching.py /
   scraping_service.py) will discard most of what it returns for an
   SDE/backend-developer target role - don't be surprised if this
   source adds few or zero jobs for that kind of search, while
   Naukri/Internshala/Foundit contribute more. Worth knowing before
   relying on it for tech-role coverage.
"""

from playwright.sync_api import sync_playwright

WORKINDIA_SEARCH_URL = "https://www.workindia.in/job-search/{keyword}-jobs"


def scrape_workindia(keyword: str = "software engineer", max_results: int = 10) -> list[dict]:
    """Returns a list of dicts: [{"title": ..., "company": ..., "raw_text": ...}, ...]"""
    results = []
    url = WORKINDIA_SEARCH_URL.format(keyword=keyword.strip().lower().replace(" ", "-"))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ))
        page.goto(url, timeout=20000)
        page.wait_for_selector(".job-card, .job-list-card, .jobCard", timeout=15000)

        cards = page.query_selector_all(".job-card, .job-list-card, .jobCard")[:max_results]

        for card in cards:
            title_el = card.query_selector(".job-title, .title, h2")
            company_el = card.query_selector(".company-name, .comp-name")
            location_el = card.query_selector(".job-location, .location")
            salary_el = card.query_selector(".salary, .job-salary")

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
            if salary_el:
                raw_text_parts.append(f"Salary: {salary_el.inner_text().strip()}")

            results.append({
                "title": title,
                "company": company,
                "raw_text": "\n".join(raw_text_parts),
            })

        browser.close()

    return results
