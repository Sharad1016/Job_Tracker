"""
Job extraction pipeline: attempts deterministic extraction before calling Gemini.

The extraction order:
1. Deterministic parsing of raw_text (line values, regex, skill dictionary)
2. Gemini fallback ONLY if critical fields are missing
3. Graceful handling of Gemini quota exhaustion (429 errors)
"""

import os
import json
import logging
import re
from google import genai
from google.genai import errors as genai_errors
from dotenv import load_dotenv

from .models import ROLE_CATEGORY_CHOICES
from .skills_dictionary import extract_skills_from_text
from .regex_extractor import extract_structured_fields
from .validation import validate_extracted_fields

load_dotenv()

logger = logging.getLogger(__name__)

_client = None
_gemini_available = True  # Track if Gemini is available (for quota exhaustion)


def _get_client():
    """Create the Gemini client once and reuse it (avoids reconnecting on every call)."""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.warning("[AI] GEMINI_API_KEY not found in environment")
            return None
        _client = genai.Client(api_key=api_key)
    return _client


_ROLE_CATEGORY_KEYS = [key for key, _label in ROLE_CATEGORY_CHOICES]

EXTRACTION_PROMPT = """You are extracting structured data from a job posting.
Read the job description below and return ONLY a JSON object with these exact keys:

- title: job title (string, "" if not found)
- company: company name (string, "" if not found)
- location: job location (string, "" if not found)
- skills_required: list of skill names mentioned, e.g. ["Python", "SQL"] (list of strings, [] if none found)
- requirements: list of qualification/requirement bullet points as separate short strings, e.g. ["Bachelor's degree in CS", "2+ years experience"] ([] if none found)
- salary_range: salary info as written, or "" if not mentioned
- deadline: application deadline in YYYY-MM-DD format if mentioned, otherwise null
- role_category: classify the ROLE into EXACTLY ONE of these categories (return the key, not the label): {role_categories}
  Base this on what the job actually IS (e.g. a "Sales Engineer" role that happens to mention Python is "sales", not "software_engineering"). If genuinely unclear, use "other".

Do not guess or invent information that isn't in the text. Leave fields empty/null if unsure.

Job description:
---
{text}
---
"""


def _clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def _extract_line_value(raw_text: str, prefixes: list[str]) -> str:
    """Extract value from lines that start with a prefix like 'Role: X' or 'Company: Y'."""
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        for prefix in prefixes:
            prefix_lower = prefix.lower()
            if lowered.startswith(f"{prefix_lower}:"):
                return _clean_text(stripped.split(":", 1)[1])
            if lowered.startswith(f"{prefix_lower} -"):
                return _clean_text(stripped.split("-", 1)[1])
    return ""


def _extract_text_skills(raw_text: str) -> list[str]:
    """Extract skills that appear after 'Skills:' section in raw_text."""
    lines = raw_text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith(("skills:", "skill:", "tech stack:", "technologies:", "required skills:")):
            remainder = stripped.split(":", 1)[1].strip() if ":" in stripped else stripped.strip()
            values = []
            if remainder:
                values.extend(re.split(r"[,|\n•]+", remainder))

            for next_line in lines[index + 1:]:
                next_stripped = next_line.strip()
                if not next_stripped:
                    continue
                lower_next = next_stripped.lower()
                if lower_next.endswith((":", ":")) or lower_next.startswith(("about the job", "responsibilities", "requirements")):
                    break
                values.extend(re.split(r"[,|\n•]+", next_stripped))
                if len(values) >= 20:
                    break
            skills = [p.strip() for p in values if p.strip()]
            return skills[:20]
    return []


def _deterministic_extraction(raw_text: str) -> dict:
    """
    Extract job fields using deterministic methods (no AI required).
    
    This uses:
    1. Line-based extraction (Role: X, Company: Y)
    2. Skill dictionary matching
    3. Regex/rule-based extraction
    
    Returns a dict with extracted fields, even if some are empty.
    """
    if not raw_text:
        return _empty_extraction()
    
    text = raw_text or ""
    
    # Stage 1: Line-value extraction (Role: X, Company: Y format)
    title = _extract_line_value(text, [
        "Role", "Title", "Job Title", "Position", "Hiring for"
    ])
    company = _extract_line_value(text, [
        "Company", "Organization", "Employer", "Hiring company"
    ])
    location = _extract_line_value(text, [
        "Location", "Work Location", "Office Location", "City"
    ])
    
    # Stage 2: Regex/rule-based extraction (salary, experience, employment type)
    structured = extract_structured_fields(text)
    salary = structured.get("salary_range") or _extract_line_value(text, [
        "Salary", "Stipend", "Compensation", "Pay"
    ])
    experience = structured.get("experience") or ""
    employment_type = structured.get("employment_type") or ""
    
    # Stage 3: Skill extraction - use text skills section, then skill dictionary
    skills_from_text_section = _extract_text_skills(text)
    skills_from_dict = extract_skills_from_text(text)
    # Merge and deduplicate
    skills_required = list(set(skills_from_text_section + skills_from_dict))
    skills_required = sorted(skills_required)[:20]
    
    # Stage 4: Requirements extraction
    requirements = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith(("requirements:", "responsibilities:", "must have:", "qualifications:", "eligibility:")):
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else stripped.strip()
            if value:
                requirements.append(value)
        elif stripped.startswith(("- ", "* ", "• ")) and not lowered.startswith(("skills:", "company:", "role:", "location:", "salary:", "stipend:")):
            requirements.append(stripped[2:].strip())

    # Fallback: if no title, try first line
    if not title:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        title = first_line.replace("Role:", "").strip() if first_line.lower().startswith("role:") else title

    return {
        "title": title,
        "company": company,
        "location": location,
        "skills_required": skills_required,
        "requirements": requirements[:10],
        "salary_range": salary,
        "deadline": structured.get("posted_date") or None,
        "role_category": "other",
        "_extraction_method": "deterministic",
    }


def _empty_extraction() -> dict:
    """Return empty extraction template."""
    return {
        "title": "",
        "company": "",
        "location": "",
        "skills_required": [],
        "requirements": [],
        "salary_range": "",
        "deadline": None,
        "role_category": "other",
    }


def _attempt_gemini_fallback(raw_text: str, partial_extraction: dict) -> dict:
    """
    Call Gemini only if:
    1. Gemini is available (not quota exhausted)
    2. Critical fields are still missing
    
    Args:
        raw_text: Original job text
        partial_extraction: Current extraction dict (may have empty fields)
        
    Returns:
        Enhanced extraction dict, or original if Gemini fails
    """
    global _gemini_available
    
    # Don't call Gemini if we know it's unavailable
    if not _gemini_available:
        logger.debug("[Extraction] Gemini unavailable, skipping fallback")
        return partial_extraction
    
    # Check if critical fields are missing
    title = (partial_extraction.get("title") or "").strip()
    company = (partial_extraction.get("company") or "").strip()
    
    if title and company:
        # We have the critical fields already, no need for Gemini
        logger.debug("[Extraction] Critical fields present, Gemini not needed")
        return partial_extraction
    
    logger.info("[AI] Attempting Gemini fallback for missing critical fields")
    
    try:
        client = _get_client()
        if client is None:
            logger.warning("[AI] Gemini client unavailable, skipping fallback")
            return partial_extraction
        
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=EXTRACTION_PROMPT.format(
                text=raw_text, role_categories=", ".join(_ROLE_CATEGORY_KEYS)
            ),
            config={
                "response_mime_type": "application/json",
            },
        )
    except genai_errors.APIError as e:
        error_str = str(e)
        
        # Check for quota exhaustion
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
            logger.warning("[AI] Gemini quota exhausted (429 RESOURCE_EXHAUSTED)")
            logger.warning("[AI] Gemini disabled for this scraping run")
            _gemini_available = False
            return partial_extraction
        else:
            logger.warning(f"[AI] Gemini API error: {e}")
            return partial_extraction
    except Exception as e:
        logger.warning(f"[AI] Gemini extraction failed: {e}")
        return partial_extraction
    
    # Parse response
    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"[AI] Gemini returned invalid JSON: {e}")
        return partial_extraction
    
    # Merge Gemini results with partial extraction
    result = partial_extraction.copy()
    
    # Only use Gemini fields if we don't already have them
    if not title and data.get("title"):
        result["title"] = _clean_text(data.get("title"))
        logger.debug("[AI] Gemini filled title")
    
    if not company and data.get("company"):
        result["company"] = _clean_text(data.get("company"))
        logger.debug("[AI] Gemini filled company")
    
    if not result.get("location") and data.get("location"):
        result["location"] = _clean_text(data.get("location"))
    
    if not result.get("salary_range") and data.get("salary_range"):
        result["salary_range"] = _clean_text(data.get("salary_range"))
    
    if not result.get("requirements") and data.get("requirements"):
        result["requirements"] = data.get("requirements") or []
    
    if not result.get("skills_required") and data.get("skills_required"):
        result["skills_required"] = data.get("skills_required") or []
    
    # Gemini can help classify role_category
    role_category = data.get("role_category") or ""
    if role_category in _ROLE_CATEGORY_KEYS:
        result["role_category"] = role_category
        logger.debug(f"[AI] Gemini classified role as {role_category}")
    
    result["_extraction_method"] = "gemini_fallback"
    logger.info("[AI] Gemini fallback completed successfully")
    return result


def extract_job_fields(raw_text: str) -> dict:
    """
    Extract job fields using multi-stage pipeline:
    
    1. Deterministic extraction (line values, regex, skill dictionary)
    2. Gemini fallback ONLY if critical fields missing
    3. Graceful handling of Gemini quota exhaustion
    
    Returns a dict matching JobPosting's fields, even if some are empty.
    Critical fields: title, company
    """
    if not raw_text:
        result = _empty_extraction()
        result["_extraction_method"] = "empty"
        return result
    
    logger.debug("[Extraction] Starting deterministic extraction")
    
    # Stage 1: Deterministic extraction
    extracted = _deterministic_extraction(raw_text)
    
    logger.debug(f"[Extraction] Deterministic complete. Title: {bool(extracted.get('title'))}, Company: {bool(extracted.get('company'))}")
    
    # Stage 2: Gemini fallback if needed
    extracted = _attempt_gemini_fallback(raw_text, extracted)
    
    # Final validation
    is_valid, errors = validate_extracted_fields(extracted)
    if not is_valid:
        logger.warning(f"[Extraction] Validation warnings: {errors}")
    
    # Clean up internal tracking fields before returning
    extracted.pop("_extraction_method", None)
    
    return extracted
