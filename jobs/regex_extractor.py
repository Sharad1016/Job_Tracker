"""
Regex and rule-based extraction for structured job fields.
Detects common patterns like salary ranges, experience levels, employment types, etc.
"""

import re
from typing import Optional
from datetime import datetime, timedelta


def extract_structured_fields(text: str) -> dict:
    """
    Extract structured fields from job text using regex patterns.
    
    Args:
        text: Job description/posting text
        
    Returns:
        Dict with extracted fields:
        {
            "salary_range": str,
            "experience": str,
            "employment_type": str,
            "posted_date": str,  # in YYYY-MM-DD format if detected
        }
    """
    result = {
        "salary_range": "",
        "experience": "",
        "employment_type": "",
        "posted_date": "",
    }
    
    if not text:
        return result
    
    text_lower = text.lower()
    
    # Extract salary range
    result["salary_range"] = _extract_salary(text)
    
    # Extract experience requirement
    result["experience"] = _extract_experience(text)
    
    # Extract employment type
    result["employment_type"] = _extract_employment_type(text)
    
    # Extract posted date if mentioned
    result["posted_date"] = _extract_posted_date(text)
    
    return result


def _extract_salary(text: str) -> str:
    """Detect salary/stipend ranges."""
    if not text:
        return ""
    
    patterns = [
        # Indian salary format: ₹3-5 LPA, ₹4,00,000 - ₹7,00,000
        r"₹[\d,]+\s*[-–]\s*₹?[\d,]+\s*(lpa|pa|per annum|per year)?",
        # Per month: ₹25,000 per month, ₹10,000-20,000/month
        r"₹[\d,]+\s*[-–]?\s*₹?[\d,]*\s*(?:/|per)?\s*month",
        # Generic: 3-5 LPA, 3 LPA - 6 LPA
        r"\d+\s*[-–]\s*\d+\s*(lpa|pa|per annum|per year)",
        # USD format
        r"\$[\d,]+\s*[-–]\s*\$?[\d,]+\s*(per year|annually|per month)?",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    
    return ""


def _extract_experience(text: str) -> str:
    """Detect experience requirements."""
    if not text:
        return ""
    
    patterns = [
        # Fresher
        r"\bfresher\b",
        # X-Y years: 0-2 years, 1-3 years, 2+ years, etc.
        r"\d+\s*[-–\+]\s*\d*\s*years?\s*(?:of\s+)?(?:experience|exp)?",
        # X+ years
        r"\d+\+\s*years?\s*(?:of\s+)?(?:experience|exp)?",
        # X years experience
        r"\d+\s*years?\s+(?:of\s+)?(?:experience|exp)",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    
    return ""


def _extract_employment_type(text: str) -> str:
    """Detect employment type (Full Time, Part Time, Contract, etc.)."""
    if not text:
        return ""
    
    text_lower = text.lower()
    
    # Check in order of priority
    types = [
        ("Full-time", [r"\bfull[\s-]?time\b", r"\bfull time\b"]),
        ("Part-time", [r"\bpart[\s-]?time\b", r"\bpart time\b"]),
        ("Internship", [r"\binternship\b", r"\bintern\b"]),
        ("Contract", [r"\bcontract\b", r"\bcontractual\b"]),
        ("Temporary", [r"\btemporary\b", r"\btemp\b"]),
        ("Permanent", [r"\bpermanent\b"]),
        ("Remote", [r"\bremote\b", r"\bwork from home\b", r"\bwfh\b"]),
        ("Hybrid", [r"\bhybrid\b"]),
        ("On-site", [r"\bon[\s-]?site\b", r"\bon site\b", r"\boffice\b"]),
    ]
    
    for emp_type, patterns in types:
        for pattern in patterns:
            if re.search(pattern, text_lower):
                return emp_type
    
    return ""


def _extract_posted_date(text: str) -> str:
    """
    Detect when job was posted (in relative or absolute format).
    Return as YYYY-MM-DD if possible, otherwise return the matched text.
    """
    if not text:
        return ""
    
    # Relative dates
    relative_patterns = [
        (r"posted\s+(?:today|today)", 0),
        (r"posted\s+(?:yesterday|yesterday)", 1),
        (r"posted\s+(\d+)\s+days?\s+ago", None),  # Captures days
        (r"posted\s+(\d+)\s+weeks?\s+ago", None),  # Captures weeks
    ]
    
    for pattern, days_offset in relative_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            if days_offset == 0:
                # Today
                return datetime.now().strftime("%Y-%m-%d")
            elif days_offset == 1:
                # Yesterday
                return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            elif pattern.endswith("ago") and "days" in pattern.lower():
                try:
                    days = int(match.group(1))
                    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                except (ValueError, IndexError):
                    pass
            elif pattern.endswith("ago") and "weeks" in pattern.lower():
                try:
                    weeks = int(match.group(1))
                    return (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
                except (ValueError, IndexError):
                    pass
    
    # Absolute dates
    absolute_patterns = [
        r"(\d{4}[-/]\d{2}[-/]\d{2})",  # YYYY-MM-DD or YYYY/MM/DD
        r"(\d{2}[-/]\d{2}[-/]\d{4})",  # DD-MM-YYYY
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4})",  # Month Day, Year
    ]
    
    for pattern in absolute_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    return ""
