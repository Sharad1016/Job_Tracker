"""
Generic CSS selector-based extraction as a fallback.
Used when JSON-LD and website-specific parsers don't cover all fields.
"""

import re
from typing import Optional


def extract_by_selectors(html: str, field_name: str, selectors: list[str]) -> str:
    """
    Extract text from HTML using a list of CSS selectors as fallbacks.
    Tries each selector in order and returns text from the first match.
    
    Args:
        html: Raw HTML string
        field_name: Descriptive name of field (for logging only)
        selectors: List of CSS selectors to try in order
        
    Returns:
        Extracted and cleaned text, or empty string if no selector matched
    """
    if not html or not selectors:
        return ""
    
    for selector in selectors:
        text = _extract_by_single_selector(html, selector)
        if text:
            return text
    
    return ""


def _extract_by_single_selector(html: str, selector: str) -> str:
    """
    Simple CSS selector extraction using regex.
    Supports basic selectors:
    - .classname
    - #idname
    - tagname
    - tagname.classname
    - [attribute='value']
    """
    if not selector:
        return ""
    
    # Remove common attributes that might be in selector
    selector = selector.strip()
    
    # Handle ID selector
    if selector.startswith("#"):
        id_val = selector[1:]
        pattern = rf'id=["\']?{re.escape(id_val)}["\']?[^>]*>([^<]+)<'
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    
    # Handle class selector
    elif selector.startswith("."):
        class_val = selector[1:]
        # Match elements with this class
        pattern = rf'class=["\']?[^"\']*{re.escape(class_val)}[^"\']*["\']?[^>]*>([^<]+)<'
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    
    # Handle attribute selector
    elif "[" in selector and "]" in selector:
        # [data-testid='value'] or similar
        pattern = selector.replace("[", r"\[").replace("]", r"\]")
        # Extract content between > and <
        outer_pattern = rf"{pattern}[^>]*>([^<]+)<"
        match = re.search(outer_pattern, html, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    
    # Handle simple tag selector
    elif re.match(r"^[a-z]+$", selector, re.IGNORECASE):
        # Just a tag name like 'h1', 'h2', 'span', etc.
        pattern = rf"<{selector}[^>]*>([^<]+)</{selector}>"
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    
    # Handle tag.class selector
    elif "." in selector:
        parts = selector.split(".")
        tag = parts[0]
        class_name = ".".join(parts[1:])
        if tag:
            pattern = rf"<{tag}[^>]*class=['\"]?[^'\"]*{re.escape(class_name)}[^'\"]*['\"]?[^>]*>([^<]+)</{tag}>"
        else:
            pattern = rf'class=["\']?[^"\']*{re.escape(class_name)}[^"\']*["\']?[^>]*>([^<]+)<'
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    
    return ""


def _clean_text(text: str) -> str:
    """Clean and normalize extracted text."""
    if not text:
        return ""
    # Remove extra whitespace, HTML entities, etc.
    text = text.strip()
    text = " ".join(text.split())  # Collapse multiple spaces
    return text
