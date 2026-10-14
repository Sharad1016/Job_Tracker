"""
JSON-LD (Schema.org) extraction for job postings.
Many job boards provide structured JobPosting data in <script type="application/ld+json">.
Use this before attempting Gemini or regex extraction.
"""

import json
from typing import Optional


def extract_from_jsonld(html: str) -> dict:
    """
    Extract job posting information from JSON-LD script tags.
    
    Looks for <script type="application/ld+json"> tags containing
    @type: "JobPosting" and extracts standard fields.
    
    Args:
        html: Raw HTML string from job posting page
        
    Returns:
        Dict with extracted fields (may have empty strings if field not found):
        {
            "title": str,
            "company": str,
            "location": str,
            "description": str,
            "salary_range": str,
            "employment_type": str,
            "posted_date": str,
            "valid_through": str,
        }
        Returns empty dict if no JobPosting JSON-LD found.
    """
    if not html:
        return {}
    
    result = {
        "title": "",
        "company": "",
        "location": "",
        "description": "",
        "salary_range": "",
        "employment_type": "",
        "posted_date": "",
        "valid_through": "",
    }
    
    # Find all <script type="application/ld+json"> blocks
    import re
    pattern = r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    matches = re.finditer(pattern, html, re.DOTALL | re.IGNORECASE)
    
    for match in matches:
        script_content = match.group(1).strip()
        try:
            data = json.loads(script_content)
        except (json.JSONDecodeError, ValueError):
            continue
        
        # Handle both direct JobPosting and nested in @graph array
        job_postings = []
        
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                job_postings.append(data)
            elif "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    if isinstance(item, dict) and item.get("@type") == "JobPosting":
                        job_postings.append(item)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    job_postings.append(item)
        
        # Extract from first JobPosting found
        if job_postings:
            job_posting = job_postings[0]
            
            # Title
            if "title" in job_posting:
                result["title"] = _clean_text(job_posting["title"])
            
            # Company name
            if "hiringOrganization" in job_posting:
                org = job_posting["hiringOrganization"]
                if isinstance(org, dict) and "name" in org:
                    result["company"] = _clean_text(org["name"])
                elif isinstance(org, str):
                    result["company"] = _clean_text(org)
            
            # Location
            if "jobLocation" in job_posting:
                loc = job_posting["jobLocation"]
                if isinstance(loc, dict):
                    # Could be address object with multiple parts
                    if "address" in loc:
                        addr = loc["address"]
                        if isinstance(addr, dict):
                            parts = []
                            for key in ["addressLocality", "addressRegion", "addressCountry"]:
                                if key in addr and addr[key]:
                                    parts.append(str(addr[key]))
                            if parts:
                                result["location"] = ", ".join(parts)
                    elif "name" in loc:
                        result["location"] = _clean_text(loc["name"])
                elif isinstance(loc, str):
                    result["location"] = _clean_text(loc)
            
            # Description / Job Description
            if "description" in job_posting:
                result["description"] = _clean_text(job_posting["description"])
            
            # Base salary / salary range
            if "baseSalary" in job_posting:
                salary = job_posting["baseSalary"]
                if isinstance(salary, dict):
                    if "currency" in salary and "value" in salary:
                        val = salary["value"]
                        if isinstance(val, dict):
                            if "minValue" in val and "maxValue" in val:
                                result["salary_range"] = f"{val['minValue']} - {val['maxValue']} {salary.get('currency', '')}"
                            elif "value" in val:
                                result["salary_range"] = f"{val['value']} {salary.get('currency', '')}"
                        else:
                            result["salary_range"] = f"{val} {salary.get('currency', '')}"
            
            # Employment type
            if "employmentType" in job_posting:
                emp_type = job_posting["employmentType"]
                if isinstance(emp_type, list):
                    result["employment_type"] = ", ".join(str(x) for x in emp_type)
                else:
                    result["employment_type"] = str(emp_type)
            
            # Posted date
            if "datePosted" in job_posting:
                result["posted_date"] = str(job_posting["datePosted"])
            
            # Valid through date
            if "validThrough" in job_posting:
                result["valid_through"] = str(job_posting["validThrough"])
            
            # Found valid data, return it
            return result
    
    # No JobPosting found
    return {}


def _clean_text(value) -> str:
    """Clean and normalize text value."""
    if value is None:
        return ""
    return " ".join(str(value).split())
