"""
Resume upload: extracts raw text from a PDF/DOCX resume, then reuses
the SAME Gemini extraction approach as extract_job_fields() (Phase 2) -
just with a prompt asking for skills instead of job details. This is
deliberately built as an ADDITION to manual skill entry, not a
replacement: AI-extracted skills get merged into the existing list,
and you can still edit the result by hand afterward.
"""

import os
import json
import pdfplumber
import docx
from google import genai
from google.genai import errors as genai_errors
from dotenv import load_dotenv

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


SKILLS_EXTRACTION_PROMPT = """You are extracting a skills list from a resume.
Read the resume text below and identify every technical skill, tool, framework,
programming language, or technology the person has actual experience with -
whether it's explicitly listed under a "Skills" section, or clearly implied by
their project/work descriptions (e.g. "built REST APIs with Flask" implies
Python, Flask, and REST APIs as skills).

Return ONLY a JSON object with this exact key:
- skills: a list of skill names as strings, e.g. ["Python", "Django", "SQL"]

Do not invent skills that aren't supported by the text. Deduplicate similar
entries (e.g. don't list both "JS" and "JavaScript" separately - pick one).

Resume text:
---
{text}
---
"""


def extract_skills_from_resume(resume_text: str) -> list:
    """
    Sends resume text to Gemini and returns a list of skill strings.
    Raises ValueError if the response isn't valid JSON. Raises
    RuntimeError if the Gemini API call itself fails - e.g. the "503
    UNAVAILABLE / model is currently experiencing high demand" error,
    which previously wasn't caught anywhere and crashed straight to
    Django's raw 500 error page instead of showing a friendly message
    on the profile page.
    """
    client = _get_client()

    try:
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=SKILLS_EXTRACTION_PROMPT.format(text=resume_text),
            config={"response_mime_type": "application/json"},
        )
    except genai_errors.APIError as e:
        raise RuntimeError(
            f"Gemini is temporarily unavailable ({e}). Please try uploading your resume again in a minute, "
            f"or add your skills manually below in the meantime."
        )

    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"Gemini did not return valid JSON: {e}")

    return data.get("skills") or []
