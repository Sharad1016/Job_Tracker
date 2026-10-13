"""Naukri scraper."""

from playwright.sync_api import sync_playwright

NAUKRI_SEARCH_URL = "https://www.naukri.com/{keyword}-jobs"


def _clean_text(value):
    return " ".join(str(value or "").split())


def _pick_text(card, selectors):
    for selector in selectors:
        el = card.locator(selector).first
        if el.count() and el.inner_text().strip():
            return _clean_text(el.inner_text())
    return ""


def scrape_naukri(keyword: str = "software engineer", max_results: int = 10) -> list[dict]:
    results = []
    url = NAUKRI_SEARCH_URL.format(keyword=keyword.strip().lower().replace(" ", "-"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 2200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        )
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)

        selectors = [
            ".cust-job-tuple",
            ".jobTuple",
            "article",
            ".job-card",
            "[data-testid='job-card']",
            "div[data-job-id]",
        ]
        found = False
        for selector in selectors:
            if page.locator(selector).count():
                found = True
                break
        if not found:
            browser.close()
            return results

        cards = page.locator(", ".join(selectors)).all()[:max_results]
        for card in cards:
            title = _pick_text(card, ["a.title", ".title", "h2", "h3", "h4", "a[href*='job-description']", "a"])
            company = _pick_text(card, ["a.comp-name", ".comp-name", ".company-name", ".companyInfo .subTitle", ".subTitle", ".company"])
            if not title or not company:
                continue

            location = _pick_text(card, [".locWdth", ".location", ".loc-wrap", ".job-location", ".cities"])
            experience = _pick_text(card, [".exp-wrap", ".experience", ".exp", ".work-experience"])
            skills = _pick_text(card, [".tags-gt", ".tags", ".keyskills", ".skills"])

            raw_text_parts = [f"Role: {title}", f"Company: {company}"]
            if location:
                raw_text_parts.append(f"Location: {location}")
            if experience:
                raw_text_parts.append(f"Experience: {experience}")
            if skills:
                raw_text_parts.append(f"Skills: {skills}")

            results.append({"title": title, "company": company, "raw_text": "\n".join(raw_text_parts)})

        browser.close()

    return results
