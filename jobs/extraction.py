"""
Phase 2: turns a messy, pasted job description into clean structured
fields using Gemini. This same function will later be reused by the
scraper in Phase 5 - it doesn't care whether the text was pasted by
hand or pulled off a job board.
"""

import os
import json
import re
from google import genai
from google.genai import errors as genai_errors
from dotenv import load_dotenv

from .models import ROLE_CATEGORY_CHOICES

load_dotenv()

_client = None


def _get_client():
    """Create the Gemini client once and reuse it (avoids reconnecting on every call)."""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not found. Add it to your .env file."
            )
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


def _extract_skills(raw_text: str) -> list[str]:
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


def _fallback_job_fields(raw_text: str) -> dict:
    text = raw_text or ""
    title = _extract_line_value(text, [
        "Role", "Title", "Job Title", "Position", "Hiring for"
    ])
    company = _extract_line_value(text, [
        "Company", "Organization", "Employer", "Hiring company"
    ])
    location = _extract_line_value(text, [
        "Location", "Work Location", "Office Location", "City"
    ])
    salary = _extract_line_value(text, [
        "Salary", "Stipend", "Compensation", "Pay"
    ])
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

    skills_required = _extract_skills(text)
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
        "deadline": None,
        "role_category": "other",
    }


def classify_role_category(text: str) -> tuple[str, str | None]:
    """
    A small, focused Gemini call used ONLY for role classification -
    deliberately kept separate from extract_job_fields() so ordinary
    field extraction (title/company/salary/skills/etc.) never needs to
    call Gemini at all in the new deterministic-first pipeline
    (see extraction_pipeline.py). Classification is the one place the
    refactor still calls Gemini by default, because "job classification
    requires AI" was explicitly called out as a legitimate use.

    Never raises - returns (category_key, error_message). Callers
    (extraction_pipeline.GeminiRunState) decide whether an error means
    "disable Gemini for the rest of this scraping run" (e.g. a 429).
    """
    try:
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=(
                "Classify this job into EXACTLY ONE category from this list "
                f"(reply with ONLY the category key, nothing else): {', '.join(_ROLE_CATEGORY_KEYS)}\n\n"
                "Base your answer on the job's ACTUAL ROLE/TITLE and its "
                "responsibilities. Do NOT classify based on the company's "
                "name or industry (e.g. a company with 'Software' in its "
                "name is not automatically a software_engineering role if "
                "the job itself, e.g. 'Registration & License Executive', "
                "is a different function).\n\n"
                f"Job:\n{text[:2000]}"
            ),
        )
        key = (response.text or "").strip().lower()
        return (key if key in _ROLE_CATEGORY_KEYS else ""), None
    except genai_errors.APIError as e:
        return "", f"Gemini API request failed: {e}"
    except Exception as e:
        return "", f"Gemini classification failed: {e}"


def extract_job_fields(raw_text: str) -> dict:
    """
    Sends raw_text to Gemini and returns a dict matching JobPosting's fields.
    If the AI response is unavailable or malformed, falls back to deterministic
    parsing of the raw_text so the job still keeps valid title/company data
    instead of silently becoming Untitled/Unknown company.
    """
    try:
        client = _get_client()
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
        fallback = _fallback_job_fields(raw_text)
        fallback["_fallback_used"] = True
        fallback["_extraction_error"] = f"Gemini API request failed: {e}"
        return fallback
    except Exception as e:
        fallback = _fallback_job_fields(raw_text)
        fallback["_fallback_used"] = True
        fallback["_extraction_error"] = f"Gemini extraction failed: {e}"
        return fallback

    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        fallback = _fallback_job_fields(raw_text)
        fallback["_fallback_used"] = True
        fallback["_extraction_error"] = f"Gemini did not return valid JSON: {e}"
        return fallback

    role_category = data.get("role_category") or ""
    if role_category not in _ROLE_CATEGORY_KEYS:
        role_category = "other"

    cleaned = {
        "title": _clean_text(data.get("title") or ""),
        "company": _clean_text(data.get("company") or ""),
        "location": _clean_text(data.get("location") or ""),
        "skills_required": data.get("skills_required") or [],
        "requirements": data.get("requirements") or [],
        "salary_range": _clean_text(data.get("salary_range") or ""),
        "deadline": data.get("deadline") or None,
        "role_category": role_category,
    }

    if not cleaned["title"] and not cleaned["company"] and not cleaned["location"]:
        fallback = _fallback_job_fields(raw_text)
        fallback["_fallback_used"] = True
        fallback["_extraction_error"] = "Gemini returned empty required fields."
        return fallback

    return cleaned
