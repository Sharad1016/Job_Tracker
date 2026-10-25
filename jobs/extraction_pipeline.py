"""
Phase 12: deterministic-first extraction pipeline for scraped jobs.

GOAL OF THIS REFACTOR: scraping and structuring job data should work
WITHOUT Gemini. Gemini becomes an optional, last-resort layer - used
only when deterministic extraction can't recover a REQUIRED field
(title/company/description), or for role classification specifically
(genuine semantic understanding, not "scraping").

Order of attempts, matching the requested architecture:
    1. JSON-LD structured data (if the scraper captured any - see the
       "IMPORTANT CAVEAT" note in jsonld_extraction.py)
    2. The scraper's own website-specific selectors - by the time an
       item reaches this pipeline, title/company are ALREADY guaranteed
       non-empty, because every scraper already discards cards missing
       them before returning. So this stage is really just "confirm
       what the scraper already found."
    3. Generic raw_text fallback (safety net only, reuses the existing
       line-prefix parser in extraction.py - "Role:", "Company:", etc.)
    4. Regex/rule extraction - experience, salary, employment type
    5. Skill dictionary matching
    6. Validation (title + company + description required)
    7. Gemini, ONLY if validation still fails after 1-6

Role classification is a SEPARATE step after validation passes,
because it's the one place this refactor deliberately keeps a
default (but quota-aware, always-optional) Gemini call - see
classify_role_category() in extraction.py for why.
"""

import logging
import re

from .jsonld_extraction import jobposting_to_fields
from .rules_extraction import extract_experience, extract_salary, extract_employment_type
from .skill_dictionary import match_skills
from .extraction import extract_job_fields, classify_role_category, _fallback_job_fields
from .models import ROLE_CATEGORY_CHOICES

logger = logging.getLogger(__name__)

_ROLE_CATEGORY_KEYS = [key for key, _label in ROLE_CATEGORY_CHOICES]

# Deliberately simple keyword heuristic, used ONLY when Gemini is
# unavailable (quota exhausted / no key / down). Imperfect by design -
# it exists purely so role-based filtering keeps working even when
# Gemini can't be called at all, per requirement #10.
_CATEGORY_KEYWORDS = {
    "software_engineering": ["developer", "engineer", "programmer", "software", "backend", "frontend", "full stack", "sde", "devops"],
    "data_analytics": ["data scientist", "data analyst", "machine learning", "ml engineer", "data engineer", "analytics"],
    "design": ["designer", "ui/ux", "ux designer", "ui designer", "graphic design"],
    "sales": ["sales", "business development executive", "bde"],
    "marketing": ["marketing", "seo", "content writer", "social media"],
    "operations": ["operations", "ops executive", "logistics"],
    "finance": ["finance", "accountant", "accounting", "audit"],
    "hr": ["human resources", "recruiter", "talent acquisition", "hr executive", "hr"],
    "customer_support": ["customer support", "customer service", "support executive", "telecaller"],
}

# Splits a title into "the actual role part" vs. a trailing employer
# name that's sometimes appended to scraped titles (e.g. "Sales
# Executive - Software Company"). Classification must be based on the
# role, not whatever comes after the separator - see requirement #5.
_TITLE_ROLE_SPLIT = re.compile(r"\s+(?:[-\u2013\u2014|]|\bat\b|\bfor\b)\s+", re.IGNORECASE)


def _match_keywords(text: str) -> str:
    """
    Word/phrase-boundary keyword match (not a raw substring check) -
    avoids things like "hr" matching inside a longer word, or "software"
    matching as part of an unrelated word. Returns the first matching
    category, or "" if nothing matches.
    """
    padded = f" {text} ".lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            pattern = r"(?<![a-z0-9])" + re.escape(kw.strip()) + r"(?![a-z0-9])"
            if re.search(pattern, padded):
                return category
    return ""


def _keyword_classify(title: str, raw_text: str) -> str:
    """
    CLASSIFY THE JOB, NOT THE COMPANY.

    Priority order:
      1. The role part of the title (title with any trailing "-
         Employer Name" segment stripped off) - the strongest, most
         reliable signal.
      2. The full title, as a safety net for titles that don't split
         cleanly.
      3. raw_text, but with any "Company: ..." line stripped out first,
         so an employer name (e.g. "Hoog Software Private Limited")
         can never masquerade as a role signal. Only reached when the
         title itself was inconclusive.
    """
    title = title or ""

    role_part = _TITLE_ROLE_SPLIT.split(title, maxsplit=1)[0]
    category = _match_keywords(role_part)
    if category:
        return category

    category = _match_keywords(title)
    if category:
        return category

    description_text = re.sub(r"(?im)^\s*company\s*:.*$", "", raw_text or "")
    return _match_keywords(description_text) or "other"


def _is_quota_error(error_text) -> bool:
    if not error_text:
        return False
    text = str(error_text).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "QUOTA" in text


class GeminiRunState:
    """
    Circuit breaker for Gemini usage WITHIN A SINGLE run_scrape() call.
    Created fresh per run (never a module-level global), so one
    request's quota exhaustion can't leak into a different, unrelated
    request later. Once disabled, no further Gemini calls happen for
    the rest of that run - deterministic/keyword fallbacks take over
    for every remaining job.
    """

    def __init__(self):
        self.available = True
        self.disabled_reason = None
        self.calls_made = 0

    def disable(self, reason):
        if self.available:
            msg = f"[AI] Gemini disabled for this scraping run: {reason}"
            print(msg)
            logger.warning(msg)
        self.available = False
        self.disabled_reason = reason


def validate_job(job: dict) -> tuple[bool, list[str]]:
    """
    Minimum bar, consistent with what this project has always actually
    enforced (see the pre-existing "if not title or not company: skip"
    checks) - title, company, and a non-empty description. NOT
    requiring application_url since neither scraping nor manual paste
    has ever populated that field in this project.
    """
    missing = []
    if not job.get("title"):
        missing.append("title")
    if not job.get("company"):
        missing.append("company")
    if not job.get("raw_text"):
        missing.append("description")
    return (len(missing) == 0, missing)


def process_scraped_item(item: dict, site: str, gemini_state: GeminiRunState) -> tuple[dict | None, list[str]]:
    """
    Runs one scraped item through the deterministic-first pipeline.
    Returns (job_fields_dict, log_lines). job_fields_dict is None if
    the item couldn't be turned into a valid job even with Gemini as
    a last resort (or Gemini being unavailable).
    """
    log = []
    label = site.title()
    title = (item.get("title") or "").strip()
    raw_text = (item.get("raw_text") or "").strip()
    log.append(f"[{label}] {title or 'Untitled listing'}")

    job = {
        "title": title,
        "company": (item.get("company") or "").strip(),
        "location": "",
        "skills_required": [],
        "requirements": [],
        "salary_range": "",
        "deadline": None,
        "raw_text": raw_text,
        "application_url": (item.get("application_url") or "").strip(),
    }

    # 1. JSON-LD
    matched_jsonld = None
    for posting in item.get("json_ld") or []:
        posting_title = (posting.get("title") or "").lower()
        if posting_title and title and (posting_title in title.lower() or title.lower() in posting_title):
            matched_jsonld = posting
            break
    if matched_jsonld:
        fields = jobposting_to_fields(matched_jsonld)
        if fields.get("location"):
            job["location"] = fields["location"]
        if fields.get("salary_range"):
            job["salary_range"] = fields["salary_range"]
        if fields.get("company") and not job["company"]:
            job["company"] = fields["company"]
        log.append("[Extraction] JSON-LD -> success")
    else:
        log.append("[Extraction] JSON-LD -> failed")

    # 2. Website-specific parser (already ran inside the scraper itself)
    log.append("[Extraction] Website parser -> " + ("success" if job["title"] and job["company"] else "partial"))

    # 3. Generic raw_text fallback.
    # BUGFIX: this used to only run when title/company were still missing,
    # which meant location (and salary, below) from the raw-text
    # "Location:"/"Salary:" lines never got parsed for scrapers - like
    # WorkIndia's - that already find title/company directly via their own
    # selectors. Location/salary need their own independent check: run the
    # line-prefix parser whenever THOSE specific fields are still empty,
    # not just when title/company are missing.
    if not job["title"] or not job["company"] or not job["location"] or not job["salary_range"]:
        generic = _fallback_job_fields(raw_text)
        job["title"] = job["title"] or generic.get("title") or ""
        job["company"] = job["company"] or generic.get("company") or ""
        job["location"] = job["location"] or generic.get("location") or ""
        job["salary_range"] = job["salary_range"] or generic.get("salary_range") or ""
        log.append("[Extraction] Generic raw_text parser -> " + ("success" if job["title"] and job["company"] else "partial/failed"))

    # 4. Regex/rules
    if not job["salary_range"]:
        job["salary_range"] = extract_salary(raw_text)
    experience = extract_experience(raw_text)
    employment_type = extract_employment_type(raw_text)
    if experience:
        job["requirements"].append(f"Experience: {experience}")
    if employment_type:
        job["requirements"].append(f"Employment Type: {employment_type}")
    log.append("[Extraction] Regex -> " + ("success" if (job["salary_range"] or experience or employment_type) else "no matches"))

    # 5. Skill dictionary
    dict_skills = match_skills(raw_text)
    if dict_skills:
        job["skills_required"] = dict_skills
        log.append(f"[EXTRACTION] Description length: {len(raw_text)} | Skills found: {dict_skills}")
    else:
        log.append(f"[EXTRACTION] Description length: {len(raw_text)} | Skills found: none")

    # 6. Validation
    valid, missing = validate_job(job)

    # 7. Gemini - ONLY as a last resort if a REQUIRED field is still missing
    if not valid:
        if not gemini_state.available:
            log.append(f"[AI] Gemini unavailable ({gemini_state.disabled_reason}) - required field(s) missing: {', '.join(missing)}")
        else:
            log.append(f"[AI] Gemini fallback -> attempted (missing: {', '.join(missing)})")
            gemini_state.calls_made += 1
            ai_result = extract_job_fields(raw_text)
            if ai_result.get("_fallback_used") and _is_quota_error(ai_result.get("_extraction_error")):
                gemini_state.disable(ai_result["_extraction_error"])
            job["title"] = job["title"] or ai_result.get("title") or ""
            job["company"] = job["company"] or ai_result.get("company") or ""
            job["location"] = job["location"] or ai_result.get("location") or ""
            job["skills_required"] = job["skills_required"] or ai_result.get("skills_required") or []
            job["salary_range"] = job["salary_range"] or ai_result.get("salary_range") or ""
            valid, missing = validate_job(job)
            log.append("[AI] Gemini -> " + ("success" if valid else f"still missing: {', '.join(missing)}"))
    else:
        log.append("[AI] Gemini not required")

    if not valid:
        log.append(f"[Validation] Required field(s) missing after all methods: {', '.join(missing)} -> job discarded")
        return None, log

    # Role classification. The keyword classifier runs FIRST since it's
    # free (no network call) - Gemini is only invoked when the keyword
    # classifier can't confidently place the job, i.e. it falls through
    # to "other". This is not a fake confidence score: "other" is
    # already _keyword_classify's own existing signal for "no clear
    # match", so using it as the trigger adds no new concept.
    keyword_result = _keyword_classify(job["title"], raw_text)

    if keyword_result != "other":
        role_category = keyword_result
        log.append(f'Role classification: "{job["title"]}"')
        log.append("Method: keyword")
        log.append(f"Category: {role_category}")
        log.append("Gemini: skipped")
    elif gemini_state.available:
        gemini_state.calls_made += 1
        ai_category, ai_error = classify_role_category(f"{job['title']}\n{raw_text}")
        log.append(f'Role classification: "{job["title"]}"')
        log.append("Method: keyword")
        log.append("Result: uncertain")
        log.append("Gemini: called")
        if ai_error and _is_quota_error(ai_error):
            gemini_state.disable(ai_error)
            role_category = keyword_result  # "other" - no regression, same answer keyword already gave
            log.append("Gemini: quota exhausted, kept keyword result")
        elif ai_category in _ROLE_CATEGORY_KEYS:
            role_category = ai_category
        else:
            role_category = keyword_result  # Gemini gave nothing usable - fall back to keyword's "other"
        log.append(f"Category: {role_category}")
    else:
        role_category = keyword_result
        log.append(f'Role classification: "{job["title"]}"')
        log.append("Method: keyword")
        log.append("Result: uncertain")
        log.append("Gemini: unavailable")
        log.append(f"Category: {role_category}")

    job["role_category"] = role_category if role_category in _ROLE_CATEGORY_KEYS else "other"

    return job, log
