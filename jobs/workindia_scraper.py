"""WorkIndia scraper."""

from playwright.sync_api import sync_playwright

WORKINDIA_SEARCH_URL = "https://www.workindia.in/job-search/{keyword}-jobs"


def _clean_text(value):
    return " ".join(str(value or "").split())


def _pick_text(card, selectors):
    for selector in selectors:
        el = card.locator(selector).first
        if el.count() and el.inner_text().strip():
            return _clean_text(el.inner_text())
    return ""


def scrape_workindia(keyword: str = "software engineer", max_results: int = 10) -> list[dict]:
    results = []
    url = WORKINDIA_SEARCH_URL.format(keyword=keyword.strip().lower().replace(" ", "-"))

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
            ".job-card",
            ".job-list-card",
            ".jobCard",
            "article",
            "li",
            "[data-testid='job-card']",
        ]
        found = any(page.locator(selector).count() for selector in selectors)
        if not found:
            browser.close()
            return results

        cards = page.locator(", ".join(selectors)).all()[:max_results]
        for card in cards:
            title = _pick_text(card, [".job-title", ".title", "h2", "h3", "h4", "a"])
            company = _pick_text(card, [".company-name", ".company", ".comp-name", ".employer-name"])
            if not title or not company:
                continue

            location = _pick_text(card, [".job-location", ".location", ".city", ".place"])
            salary = _pick_text(card, [".salary", ".job-salary", ".stipend", ".pay"])

            raw_text_parts = [f"Role: {title}", f"Company: {company}"]
            if location:
                raw_text_parts.append(f"Location: {location}")
            if salary:
                raw_text_parts.append(f"Salary: {salary}")

            results.append({"title": title, "company": company, "raw_text": "\n".join(raw_text_parts)})

        browser.close()

    return results
