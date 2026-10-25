"""WorkIndia scraper."""

from playwright.sync_api import sync_playwright

from .jsonld_extraction import extract_json_ld_jobpostings
from .detail_page_extraction import extract_description_and_skills

# FIX (confirmed via real page inspection, not guessed): WorkIndia has NO
# "/job-search/{keyword}-jobs" path at all - that was hitting their custom
# 200-status "soft 404" page every time. The real site only has a FIXED set
# of category landing pages (e.g. "/software-developer-jobs", "/hr-jobs"),
# not free-text keyword search - confirmed by reading the actual footer
# navigation links on a real WorkIndia page.
WORKINDIA_SEARCH_URL = "https://www.workindia.in/{slug}"

# Maps common target roles to the closest REAL category slug that exists on
# the site. Unmatched roles fall back to "software-developer-jobs" - the
# closest real category to this project's target audience (software/backend
# roles); WorkIndia's own category list is overwhelmingly non-tech
# (cook-jobs, driving-jobs, maid-jobs, etc.), so most software-specific
# search terms won't have an exact category match.
_KNOWN_CATEGORIES = {
    "software developer": "software-developer-jobs",
    "software engineer": "software-developer-jobs",
    "python developer": "software-developer-jobs",
    "backend developer": "software-developer-jobs",
    "developer": "software-developer-jobs",
    "sde": "software-developer-jobs",
    "engineer": "engineer-jobs",
    "hr": "hr-jobs",
    "marketing": "marketing-jobs",
    "digital marketing": "digital-marketing-jobs",
    "sales": "business-development-jobs",
    "business development": "business-development-jobs",
    "data entry": "data-entry-jobs",
    "teacher": "teacher-jobs",
    "technician": "technician-jobs",
    "nurse": "nurse-jobs",
    "receptionist": "receptionist-jobs",
    "telecalling": "telecalling-jobs",
    "graphic designer": "graphic-designer-jobs",
}
_DEFAULT_CATEGORY = "software-developer-jobs"


def _resolve_category_slug(keyword: str) -> str:
    normalized = (keyword or "").strip().lower()
    if normalized in _KNOWN_CATEGORIES:
        return _KNOWN_CATEGORIES[normalized]
    for key, slug in _KNOWN_CATEGORIES.items():
        if key in normalized:
            return slug
    return _DEFAULT_CATEGORY


def _clean_text(value):
    return " ".join(str(value or "").split())


def _pick_text(card, selectors):
    for selector in selectors:
        el = card.locator(selector).first
        if el.count() and el.inner_text().strip():
            return _clean_text(el.inner_text())
    return ""


def scrape_workindia(keyword: str = "software engineer", max_results: int = 10, known_pairs: set = None) -> list[dict]:
    known_pairs = known_pairs or set()
    results = []
    slug = _resolve_category_slug(keyword)
    url = WORKINDIA_SEARCH_URL.format(slug=slug)
    print(f"[WorkIndia][DIAG] Resolved '{keyword}' -> category slug '{slug}'")

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

        # Phase 12: capture Schema.org JobPosting JSON-LD if present (often empty on listing pages - see jsonld_extraction.py)
        json_ld_postings = extract_json_ld_jobpostings(page.content())

        # CONFIRMED real card structure (from actual saved HTML, not guessed -
        # see scraper_debug/workindia_debug.html). WorkIndia renders each
        # listing as a ".JobItemV2" block with a distinct class per field.
        CARD_SELECTOR = ".JobItemV2"
        found = page.locator(CARD_SELECTOR).count()
        if not found:
            browser.close()
            return results

        cards = page.locator(CARD_SELECTOR).all()[:max_results]
        for card in cards:
            title_link = card.locator("h2.text-brand a").first
            title = _clean_text(title_link.inner_text()) if title_link.count() else ""
            href = title_link.get_attribute("href") if title_link.count() else ""
            company = _pick_text(card, [".CompanyDetail"])
            if not title or not company:
                continue

            location = _pick_text(card, [".LocationDetail"])
            # Salary sits as a sibling of the title, not inside JobDetailsSection -
            # ".f14.f-bold" is the confirmed class for it (e.g. "Rs. 28000 - Rs. 38000").
            salary = _pick_text(card, [".f14.f-bold"])
            experience = _pick_text(card, [".ExperienceDetail"])
            job_type = _pick_text(card, [".JobTypeDetail"])

            raw_text_parts = [f"Role: {title}", f"Company: {company}"]
            if location:
                raw_text_parts.append(f"Location: {location}")
            if salary:
                raw_text_parts.append(f"Salary: {salary}")
            if experience:
                raw_text_parts.append(f"Experience: {experience}")
            if job_type:
                # Deliberately NOT treated as employment_type (that's a
                # Full Time/Part Time/Internship concept) - WorkIndia's
                # "JobTypeDetail" is actually an industry/category label
                # (e.g. "Delivery", "Telecalling"). Left as raw context so
                # the shared rules_extraction.py handles real employment-type
                # detection instead of duplicating that logic here.
                raw_text_parts.append(f"Category: {job_type}")

            results.append({
                "title": title,
                "company": company,
                "raw_text": "\n".join(raw_text_parts),
                "json_ld": json_ld_postings,
                "application_url": href or "",
            })

        # --- Stage 2 (Phase 15): each job's own detail page. NOTE:
        # WorkIndia's detail-page HTML structure is UNVERIFIED (no real
        # saved WorkIndia detail page has been inspected, unlike its
        # listing page) - only generic best-effort selectors are used
        # here (see detail_page_extraction.py). One failed/empty detail
        # page does not drop the job - it just keeps the shorter
        # listing-page raw_text it already has.
        for item in results:
            pair = (item["title"].strip().lower(), item["company"].strip().lower())
            if pair in known_pairs:
                print(f"[WorkIndia] Skipping detail page for already-saved job: {item['title']} at {item['company']}")
                continue

            detail_url = item.get("application_url") or ""
            if not detail_url:
                continue
            try:
                page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1200)

                print(f"[WorkIndia] Fetching detail page: {detail_url}")

                detail_json_ld = extract_json_ld_jobpostings(page.content())
                description, skills = extract_description_and_skills(page)

                if detail_json_ld:
                    item["json_ld"] = detail_json_ld

                if skills:
                    item["raw_text"] += "\nSkills: " + ", ".join(skills)
                if description:
                    item["raw_text"] += "\n" + description

                print(
                    f"[WorkIndia] Detail page fetched: {bool(description or skills)} | "
                    f"Description length: {len(description)} | Skills found: {skills or 'none'}"
                )
            except Exception as e:
                print(f"[WorkIndia] Detail page failed for {detail_url}: {e}")
                continue

        browser.close()

    return results
