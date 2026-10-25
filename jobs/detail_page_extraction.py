"""
Phase 15: shared helper for Stage 2 of scraping. Given a Playwright
`page` that's already been navigated to an INDIVIDUAL job's own detail
page (not the listing/search page), pulls out the real job description
and any explicit skill tags the site renders. Used by BOTH scraper.py
(Internshala) and workindia_scraper.py so the two-stage pattern
(listing page -> detail page) isn't implemented twice.

WHY THIS EXISTS: the listing page only ever gives short card summaries
(title/company/location) - the actual description and skill tags only
exist on each job's own page. Feeding a real description + an explicit
"Skills: ..." line into raw_text is enough for the EXISTING pipeline
(extraction_pipeline.py's skill_dictionary.match_skills() call, already
unconditional on the full raw_text) to find them - no changes needed
to the skill-matching logic itself, just richer input.

SELECTOR CONFIDENCE - read before debugging "no skills found":
- Internshala's confirmed selectors (skills_heading / round_tabs /
  about_heading / text-container) were verified against a real saved
  Internshala INTERNSHIP detail page. Internshala's JOB detail pages
  (internshala.com/job/detail/... vs .../internship/detail/...) are
  EXPECTED to share the same template, but this has not been directly
  verified against a real saved Jobs detail page. If Internshala job
  skills come back consistently empty, that assumption is the first
  thing to re-check - the same way WorkIndia's listing selectors
  needed correcting once a real saved page was inspected earlier in
  this project.
- WorkIndia's detail-page structure is COMPLETELY UNVERIFIED - no real
  WorkIndia detail page has been inspected at all. Only generic,
  best-effort selectors are used for it. If WorkIndia skills come back
  empty, the real fix is the same recipe used for its listing page:
  save a real WorkIndia job detail page and inspect it directly.
"""


def _clean_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _clean_multiline(value: str) -> str:
    lines = [ln.strip() for ln in str(value or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_description_and_skills(page) -> tuple[str, list[str]]:
    """
    Returns (description_text, skills_list) from whatever detail page
    `page` is currently showing. Never raises - a selector that finds
    nothing just means an empty result for that piece, not a crash;
    the caller decides what to do if both come back empty.
    """
    description = ""
    skills: list[str] = []

    # --- Skills: Internshala-confirmed selector first ---
    # IMPORTANT: Internshala reuses the same ".round_tabs" pill styling
    # for BOTH the "Skill(s) required" section AND the unrelated
    # "Perks" section (Certificate, Free snacks, Job offer, etc.) on the
    # same page. A bare "div.round_tabs_container span.round_tabs"
    # selector matches BOTH and pollutes the skill list with perks - the
    # CSS adjacent-sibling combinator ("h3.skills_heading +
    # div.round_tabs_container") scopes this to ONLY the container that
    # comes immediately after the "Skill(s) required" heading.
    try:
        skill_spans = page.locator("h3.skills_heading + div.round_tabs_container span.round_tabs")
        if skill_spans.count():
            skills = [_clean_line(t) for t in skill_spans.all_inner_texts() if t.strip()]
    except Exception:
        pass

    if not skills:
        for sel in ["[class*='skill' i] span", "[class*='skill' i] li", "ul[class*='skill' i] li"]:
            try:
                el = page.locator(sel)
                if el.count():
                    found = [_clean_line(t) for t in el.all_inner_texts() if t.strip() and len(t.strip()) < 60]
                    if found:
                        skills = found
                        break
            except Exception:
                continue

    # --- Description: Internshala-confirmed selector first ---
    try:
        desc_el = page.locator("div.text-container").first
        if desc_el.count() and desc_el.inner_text().strip():
            description = _clean_multiline(desc_el.inner_text())
    except Exception:
        pass

    if not description:
        for sel in ["[class*='description' i]", "[class*='job-detail' i]", "[class*='about' i]", "article"]:
            try:
                el = page.locator(sel).first
                if el.count():
                    text = el.inner_text().strip()
                    if len(text) > 60:  # avoid grabbing a short label/heading by accident
                        description = _clean_multiline(text)
                        break
            except Exception:
                continue

    return description, skills
