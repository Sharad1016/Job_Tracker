"""Foundit scraper."""

from playwright.sync_api import sync_playwright

from .jsonld_extraction import extract_json_ld_jobpostings

FOUNDIT_SEARCH_URL = "https://www.foundit.in/srp/results?query={keyword}"


def _clean_text(value):
    return " ".join(str(value or "").split())


def _pick_text(card, selectors):
    for selector in selectors:
        el = card.locator(selector).first
        if el.count() and el.inner_text().strip():
            return _clean_text(el.inner_text())
    return ""


def scrape_foundit(keyword: str = "software engineer", max_results: int = 10) -> list[dict]:
    results = []
    url = FOUNDIT_SEARCH_URL.format(keyword=keyword.strip().replace(" ", "%20"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 2200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        )
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)

        # --- TEMPORARY DIAGNOSTIC LOGGING (read-only, no selector/logic changes) ---
        print(f"[Foundit][DIAG] Requested URL: {url}")
        print(f"[Foundit][DIAG] Final URL after redirects: {page.url}")
        print(f"[Foundit][DIAG] HTTP status: {response.status if response else 'unknown'}")
        print(f"[Foundit][DIAG] Page title: {page.title()!r}")
        _page_html = page.content()
        _anti_bot_hits = [kw for kw in ["captcha", "verify you are human", "access denied", "are you a robot", "unusual traffic", "just a moment", "blocked", "enable javascript"] if kw in _page_html.lower()]
        print(f"[Foundit][DIAG] Anti-bot/JS-wall indicators found: {_anti_bot_hits or 'none'}")
        print(f"[Foundit][DIAG] Page HTML length: {len(_page_html)} chars")
        # --- END TEMPORARY DIAGNOSTIC LOGGING (selector-count diagnostics below, after `selectors` is defined) ---

        # Phase 12: capture Schema.org JobPosting JSON-LD if present (often empty on listing pages - see jsonld_extraction.py)
        json_ld_postings = extract_json_ld_jobpostings(page.content())

        selectors = [
            ".srpResultCardContainer",
            ".job-tittle",
            ".card-panel",
            "article",
            ".srpResultCard",
            ".job-card",
        ]
        for _sel in selectors:
            print(f"[Foundit][DIAG] Selector '{_sel}' matches: {page.locator(_sel).count()}")
        found = any(page.locator(selector).count() for selector in selectors)
        if not found:
            browser.close()
            return results

        cards = page.locator(", ".join(selectors)).all()[:max_results]
        for card in cards:
            title = _pick_text(card, [".jobTitle", ".title", ".job-title", "h3 a", "h2 a", "a"])
            company = _pick_text(card, [".companyName", ".company-name", ".company", ".comp-name", ".employer-name"])
            if not title or not company:
                continue

            location = _pick_text(card, [".locationLabel", ".location", ".loc", ".job-location"])
            skills = _pick_text(card, [".skills", ".details", ".experience", ".jd", ".keywords"])

            raw_text_parts = [f"Role: {title}", f"Company: {company}"]
            if location:
                raw_text_parts.append(f"Location: {location}")
            if skills:
                raw_text_parts.append(skills)

            results.append({"title": title, "company": company, "raw_text": "\n".join(raw_text_parts), "json_ld": json_ld_postings})

        browser.close()

    return results
