"""
Phase 2: turns a messy, pasted job description into clean structured
fields using Gemini. This same function will later be reused by the
scraper in Phase 5 - it doesn't care whether the text was pasted by
hand or pulled off a job board.
"""

import os
import json
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


def extract_job_fields(raw_text: str) -> dict:
    """
    Sends raw_text to Gemini and returns a dict matching JobPosting's fields.
    Raises ValueError if Gemini's response can't be parsed as valid JSON.
    Raises RuntimeError if the Gemini API call itself fails (rate limit,
    503 "model overloaded", network error, etc.) - previously this
    wasn't caught at all, so a transient Gemini outage would crash the
    whole request with an unhandled 500 instead of a friendly message
    (and, since this function is also called in a loop by the scraper,
    it would have killed an entire multi-site scrape run over ONE
    failed job instead of just skipping that job and continuing).
    """
    client = _get_client()

    try:
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
        # Covers both ServerError (5xx, e.g. "model overloaded") and
        # ClientError (4xx, e.g. bad/expired API key, rate limit).
        raise RuntimeError(f"Gemini API request failed: {e}")

    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"Gemini did not return valid JSON: {e}")

    role_category = data.get("role_category") or ""
    if role_category not in _ROLE_CATEGORY_KEYS:
        # Never trust the model to stay inside the enum - fall back to
        # "other" rather than saving a category value that doesn't
        # exist in ROLE_CATEGORY_CHOICES (which would break filtering
        # and the Django admin's choice validation).
        role_category = "other"

    # Defensive defaults - never trust external output blindly
    return {
        "title": data.get("title") or "",
        "company": data.get("company") or "",
        "location": data.get("location") or "",
        "skills_required": data.get("skills_required") or [],
        "requirements": data.get("requirements") or [],
        "salary_range": data.get("salary_range") or "",
        "deadline": data.get("deadline") or None,
        "role_category": role_category,
    }
