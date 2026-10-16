"""WorkIndia scraper."""

from playwright.sync_api import sync_playwright

from .jsonld_extraction import extract_json_ld_jobpostings

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
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)

        # --- TEMPORARY DIAGNOSTIC LOGGING (read-only, no selector/logic changes) ---
        print(f"[Workindia][DIAG] Requested URL: {url}")
        print(f"[Workindia][DIAG] Final URL after redirects: {page.url}")
        print(f"[Workindia][DIAG] HTTP status: {response.status if response else 'unknown'}")
        print(f"[Workindia][DIAG] Page title: {page.title()!r}")
        _page_html = page.content()
        _anti_bot_hits = [kw for kw in ["captcha", "verify you are human", "access denied", "are you a robot", "unusual traffic", "just a moment", "blocked", "enable javascript"] if kw in _page_html.lower()]
        print(f"[Workindia][DIAG] Anti-bot/JS-wall indicators found: {_anti_bot_hits or 'none'}")
        print(f"[Workindia][DIAG] Page HTML length: {len(_page_html)} chars")
        # --- END TEMPORARY DIAGNOSTIC LOGGING (selector-count diagnostics below, after `selectors` is defined) ---

        # --- ADDITIONAL TEMPORARY DIAGNOSTIC LOGGING (WorkIndia only, read-only, no selector/logic changes) ---
        import os as _os
        import re as _re

        _debug_dir = _os.path.join(_os.getcwd(), "scraper_debug")
        _os.makedirs(_debug_dir, exist_ok=True)
        _debug_path = _os.path.join(_debug_dir, "workindia_debug.html")
        with open(_debug_path, "w", encoding="utf-8") as _f:
            _f.write(_page_html)
        print(f"[WorkIndia][DIAG] Full page HTML saved to: {_debug_path}")

        _keyword_counts = {kw: _page_html.lower().count(kw) for kw in
                            ["job", "title", "company", "salary", "location", "experience", "apply", "vacancy", "hiring"]}
        print(f"[WorkIndia][DIAG] Keyword occurrence counts in raw HTML: {_keyword_counts}")

        # SPA / client-side-rendering signal: a near-empty root div is typical of React/Angular/Vue apps
        # that fetch and render job data AFTER the initial page load, not in the HTML we captured.
        _root_match = _re.search(r'<div[^>]+id=["\'](root|app|__next)["\'][^>]*>(.*?)</div>', _page_html, _re.IGNORECASE | _re.DOTALL)
        if _root_match:
            _inner_len = len(_root_match.group(2).strip())
            _verdict = "looks EMPTY - likely JS-rendered after load" if _inner_len < 200 else "has real content already in initial HTML"
            print(f"[WorkIndia][DIAG] SPA-style root container found (id={_root_match.group(1)!r}), inner content length: {_inner_len} chars ({_verdict})")
        else:
            print("[WorkIndia][DIAG] No typical SPA root container (#root/#app/#__next) found in the HTML")

        _framework_hits = [fw for fw in ["react", "next.js", "_next/static", "ng-app", "angular", "vue", "__nuxt__", "__initial_state__"] if fw in _page_html.lower()]
        print(f"[WorkIndia][DIAG] Frontend framework indicators found: {_framework_hits or 'none'}")

        _job_idx = _page_html.lower().find("job")
        if _job_idx != -1:
            _snippet = _page_html[max(0, _job_idx - 200):_job_idx + 300]
            print(f"[WorkIndia][DIAG] HTML snippet around first 'job' occurrence:\n{_snippet}\n")
        else:
            print("[WorkIndia][DIAG] The word 'job' does not appear ANYWHERE in the returned HTML")
        # --- END ADDITIONAL DIAGNOSTIC LOGGING ---

        # Phase 12: capture Schema.org JobPosting JSON-LD if present (often empty on listing pages - see jsonld_extraction.py)
        json_ld_postings = extract_json_ld_jobpostings(page.content())

        selectors = [
            ".job-card",
            ".job-list-card",
            ".jobCard",
            "article",
            "li",
            "[data-testid='job-card']",
        ]
        for _sel in selectors:
            print(f"[Workindia][DIAG] Selector '{_sel}' matches: {page.locator(_sel).count()}")
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

            results.append({"title": title, "company": company, "raw_text": "\n".join(raw_text_parts), "json_ld": json_ld_postings})

        browser.close()

    return results
