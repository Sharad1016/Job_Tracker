"""
Phase 17: Python is now the PRIMARY resume skill extractor.

Resume text -> skill_dictionary.extract_resume_skills() (pure Python,
no network call) -> known skills used immediately. ONLY the leftover
"unknown" candidate terms (if any) are optionally sent to Gemini, in
ONE batched call, to check whether they're genuine technical skills
worth adding.

This means: a resume upload with skills Gemini has ever recognized
before now works with ZERO Gemini calls, and even when there ARE
unknown terms, Gemini failing (503/quota/timeout/anything) no longer
breaks the upload - it just means the unknown terms don't get
validated this time; the known skills are unaffected.
"""

import os
import json
import time
import pdfplumber
import docx
from google import genai
from google.genai import errors as genai_errors
from dotenv import load_dotenv

from . import skill_dictionary

load_dotenv()

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not found. Add it to your .env file.")
        _client = genai.Client(api_key=api_key)
    return _client


def extract_text_from_file(uploaded_file) -> str:
    """
    Pulls raw text out of an uploaded PDF or DOCX file. Raises ValueError
    for unsupported types or files that yield no readable text - both
    are things the calling view needs to show the user, not silently
    swallow.
    """
    name = uploaded_file.name.lower()

    if name.endswith(".pdf"):
        text_parts = []
        with pdfplumber.open(uploaded_file) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        text = "\n".join(text_parts)

    elif name.endswith(".docx"):
        document = docx.Document(uploaded_file)
        text = "\n".join(p.text for p in document.paragraphs if p.text)

    else:
        raise ValueError("Unsupported file type - please upload a PDF or DOCX resume.")

    if not text.strip():
        raise ValueError(
            "Couldn't read any text from that file - it may be a scanned "
            "image rather than actual text. Try a different file, or add "
            "skills manually below."
        )

    return text


_UNKNOWN_SKILL_VALIDATION_PROMPT = """You are validating candidate skill terms found on a resume.
For each term below, decide whether it genuinely represents a real technical /
software-engineering skill, tool, framework, language, or technology (not a
generic word, soft skill, or unrelated phrase).

Terms to check:
{terms}

Return ONLY a JSON object with this exact shape:
{{"valid_skills": ["term1", "term2"]}}

Include ONLY the terms from the list above that are genuine technical skills.
Use the exact spelling/casing given. Omit anything that is not a real
technical skill - do not explain, do not add terms that weren't given.
"""


def _call_gemini_with_retry(fn, retries: int = 1, delay_seconds: float = 3.0):
    """
    Calls fn() and retries once on a Gemini 503/UNAVAILABLE ("high
    demand") error, since Gemini's own error message says these spikes
    are usually transient. Any OTHER failure (auth error, quota
    exhausted, genuinely down) is not worth retrying and is re-raised
    immediately. Never retries more than `retries` times.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except genai_errors.APIError as e:
            is_high_demand = "UNAVAILABLE" in str(e) or "503" in str(e)
            if is_high_demand and attempt < retries:
                attempt += 1
                time.sleep(delay_seconds)
                continue
            raise


def _validate_unknown_skills_with_gemini(unknown_terms: list) -> list:
    """
    Sends ONLY the unknown candidate terms (never the whole resume, and
    never terms Python already recognized) to Gemini in ONE batched
    call, asking which of them are genuine technical skills.

    Never raises - Gemini being unavailable, timing out, returning bad
    JSON, or anything else just means "no unknown terms validated this
    time". The resume upload as a whole must never fail just because
    this optional enhancement step didn't work.
    """
    if not unknown_terms:
        return []

    try:
        client = _get_client()
        terms_list = "\n".join(f"- {t}" for t in unknown_terms)

        def _do_call():
            return client.models.generate_content(
                model="gemini-flash-latest",
                contents=_UNKNOWN_SKILL_VALIDATION_PROMPT.format(terms=terms_list),
                config={"response_mime_type": "application/json"},
            )

        response = _call_gemini_with_retry(_do_call)
        data = json.loads(response.text)
        valid = data.get("valid_skills") or []
        # Only keep terms that were actually in what we asked about -
        # never trust Gemini to introduce a term we didn't send it.
        asked_lower = {t.strip().lower() for t in unknown_terms}
        return [t for t in valid if isinstance(t, str) and t.strip().lower() in asked_lower]
    except Exception:
        # Deliberately broad: APIError, JSON errors, missing key,
        # anything - the unknown terms just don't get validated this
        # run. This is the enhancement layer, not the core feature.
        return []


def extract_skills_from_resume(resume_text: str) -> list:
    """
    PRIMARY entrypoint (unchanged name/signature, so views.py needs no
    changes). New internal flow:

      1. skill_dictionary.extract_resume_skills() - pure Python, always
         runs, never fails, finds known skills + flags unknown terms.
      2. If there are unknown terms, ONE batched Gemini call tries to
         validate them. If that fails for any reason, they're simply
         skipped - the known skills from step 1 are returned regardless.
      3. Any Gemini-confirmed term is registered into the shared
         dictionary (skill_dictionary.register_learned_skill) so it's
         recognized by future resume AND job extraction in this same
         server process, then included in the returned list.

    Always returns a plain list of skill strings - never raises for a
    Gemini-availability reason anymore. extract_text_from_file() above
    still raises ValueError for a genuinely unreadable file, which is
    a separate, real problem the user does need to see.
    """
    extraction = skill_dictionary.extract_resume_skills(resume_text)
    known = extraction["known"]
    unknown = extraction["unknown"]

    confirmed = _validate_unknown_skills_with_gemini(unknown)
    for term in confirmed:
        canonical = term.strip()
        skill_dictionary.register_learned_skill(canonical)

    result = list(known)
    seen_lower = {s.lower() for s in result}
    for term in confirmed:
        if term.strip().lower() not in seen_lower:
            result.append(term.strip())
            seen_lower.add(term.strip().lower())

    return result
