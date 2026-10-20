"""
Phase 12: regex/rule-based extraction for fields that don't need AI -
experience range, salary/stipend, and employment type. Operates purely
on the raw_text string the scrapers already assemble, no network calls.
"""

import re

_EXPERIENCE_PATTERNS = [
    re.compile(r"\b(\d+)\s*[-to]{1,3}\s*(\d+)\s*years?\b", re.IGNORECASE),
    re.compile(r"\b(\d+)\+\s*years?\b", re.IGNORECASE),
    re.compile(r"\b(\d+)\s*years?\s*experience\b", re.IGNORECASE),
    re.compile(r"\bfresher\b", re.IGNORECASE),
]

_SALARY_PATTERNS = [
    re.compile(r"₹\s*[\d,]+(?:\.\d+)?\s*-\s*₹?\s*[\d,]+(?:\.\d+)?\s*(?:LPA|lakh|lakhs|per month|/month|p\.?a\.?)?", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*LPA\b", re.IGNORECASE),
    re.compile(r"₹\s*[\d,]+(?:\.\d+)?\s*(?:per month|/month|p\.?m\.?)", re.IGNORECASE),
    # WorkIndia's actual format, confirmed from real scraped cards:
    # "Rs. 28000 - Rs. 38000" - "Rs." not "₹", and repeated on both sides.
    re.compile(r"Rs\.?\s*[\d,]+(?:\.\d+)?\s*-\s*Rs\.?\s*[\d,]+(?:\.\d+)?\s*(?:per month|/month|p\.?m\.?|LPA)?", re.IGNORECASE),
    re.compile(r"Rs\.?\s*[\d,]+(?:\.\d+)?\s*(?:per month|/month|p\.?m\.?)", re.IGNORECASE),
]

_EMPLOYMENT_TYPE_KEYWORDS = [
    ("Internship", ["intern", "internship"]),
    ("Full Time", ["full time", "full-time", "fulltime"]),
    ("Part Time", ["part time", "part-time"]),
    ("Contract", ["contract", "contractual", "freelance"]),
    ("Work From Home", ["work from home", "wfh", "remote"]),
    ("Hybrid", ["hybrid"]),
]


def extract_experience(text: str) -> str:
    if not text:
        return ""
    for pattern in _EXPERIENCE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).strip()
    return ""


def extract_salary(text: str) -> str:
    if not text:
        return ""
    for pattern in _SALARY_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).strip()
    return ""


def extract_employment_type(text: str) -> str:
    if not text:
        return ""
    lowered = text.lower()
    found = [label for label, keywords in _EMPLOYMENT_TYPE_KEYWORDS if any(kw in lowered for kw in keywords)]
    return ", ".join(found)
