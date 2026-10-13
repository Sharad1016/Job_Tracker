"""Internshala scraper."""

from playwright.sync_api import sync_playwright

INTERNSHALA_SEARCH_URL = "https://internshala.com/jobs/{keyword}-jobs"


def _clean_text(value):
    return " ".join(str(value or "").split())


def scrape_internshala(keyword: str = "python", max_results: int = 10) -> list[dict]:
    results = []
    url = INTERNSHALA_SEARCH_URL.format(keyword=keyword.strip().lower().replace(" ", "-"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 2200})
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        card_selector = ".individual_internship, .internship-card, article, [data-job-id]"
        cards = page.locator(card_selector).all()[:max_results]

        for card in cards:
            title = ""
            for selector in [".job-title-href", ".heading_4_5 a", "h3 a", "h2 a", "a[href*='internship']"]:
                text = card.locator(selector).first
                if text.count() and text.inner_text().strip():
                    title = _clean_text(text.inner_text())
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
            })

        browser.close()

    return results
