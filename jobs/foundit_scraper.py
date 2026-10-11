"""
Foundit.in (formerly Monster India) scraper - same interface/pattern
as scrape_internshala() in scraper.py.

IMPORTANT - UNVERIFIED AGAINST THE LIVE SITE, same caveat as
naukri_scraper.py: written from general knowledge of Foundit's
structure, not confirmed against the live DOM (this sandbox can't
reach foundit.in). Expect to need to correct selectors after running
this once and inspecting the real page with DevTools.
"""

from playwright.sync_api import sync_playwright

FOUNDIT_SEARCH_URL = "https://www.foundit.in/srp/results?query={keyword}"


def scrape_foundit(keyword: str = "software engineer", max_results: int = 10) -> list[dict]:
    """Returns a list of dicts: [{"title": ..., "company": ..., "raw_text": ...}, ...]"""
    results = []
    url = FOUNDIT_SEARCH_URL.format(keyword=keyword.strip().replace(" ", "%20"))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ))
        page.goto(url, timeout=20000)
        page.wait_for_selector(".srpResultCardContainer, .card-panel, .job-tittle", timeout=15000)

        cards = page.query_selector_all(".srpResultCardContainer, .card-panel")[:max_results]

        for card in cards:
            title_el = card.query_selector(".jobTitle, .title, h3 a")
            company_el = card.query_selector(".companyName, .comp-name, .company-name")
            location_el = card.query_selector(".locationLabel, .loc, .location")
            skills_el = card.query_selector(".skills, .details")

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
            if skills_el:
                raw_text_parts.append(skills_el.inner_text().strip())

            results.append({
                "title": title,
                "company": company,
                "raw_text": "\n".join(raw_text_parts),
            })

        browser.close()

    return results
