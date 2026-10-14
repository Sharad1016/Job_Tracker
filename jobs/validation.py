"""
Job posting validation.
Ensures extracted jobs have required fields before saving to database.
"""

from typing import Tuple, List


def validate_job(job: dict) -> Tuple[bool, List[str]]:
    """
    Validate a job posting dict.
    
    Args:
        job: Job dict to validate (from extraction pipeline)
        
    Returns:
        Tuple of (is_valid: bool, error_messages: list[str])
        
    Validates:
        - title: Required, non-empty
        - company: Required, non-empty
        - raw_text: Required, non-empty (original source data)
    """
    errors = []
    
    if not job:
        return False, ["Job dict is empty"]
    
    # Required fields
    title = (job.get("title") or "").strip()
    company = (job.get("company") or "").strip()
    raw_text = (job.get("raw_text") or "").strip()
    
    if not title:
        errors.append("Title is required and cannot be empty")
    
    if not company:
        errors.append("Company is required and cannot be empty")
    
    if not raw_text:
        errors.append("Raw text (original source) is required and cannot be empty")
    
    # Optional fields - at least one of these should exist for a complete job
    location = (job.get("location") or "").strip()
    description = (job.get("description") or "").strip()
    skills = job.get("skills_required") or []
    
    has_details = bool(location or description or (isinstance(skills, list) and len(skills) > 0))
    
    if not has_details and title and company:
        # This is a warning, not an error - job is still valid but sparse
        pass
    
    is_valid = len(errors) == 0
    return is_valid, errors


def validate_extracted_fields(extracted: dict) -> Tuple[bool, List[str]]:
    """
    Validate fields extracted by extraction pipeline.
    Checks for critical fields that should be present after extraction.
    
    Args:
        extracted: Dict returned by extract_job_fields()
        
    Returns:
        Tuple of (is_valid: bool, error_messages: list[str])
    """
    errors = []
    
    # Check critical fields that extraction should fill
    for field in ["title", "company"]:
        value = (extracted.get(field) or "").strip()
        if not value:
            errors.append(f"Extracted {field} is empty")
    
    # Check that lists are lists
    for field in ["skills_required", "requirements"]:
        value = extracted.get(field)
        if value is not None and not isinstance(value, list):
            errors.append(f"Extracted {field} should be a list, got {type(value).__name__}")
    
    is_valid = len(errors) == 0
    return is_valid, errors
