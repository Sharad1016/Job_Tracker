"""
Phase 12: extracts Schema.org JobPosting structured data from
<script type="application/ld+json"> blocks on a page, if present.

Honest caveat: JSON-LD JobPosting markup is far more common on
individual job DETAIL pages than on search/listing pages (which is
what all four scrapers currently crawl). So on many runs this stage
will legitimately find nothing to match - that's expected, not a bug,
and the pipeline treats "JSON-LD -> failed" as a normal, non-fatal
step, exactly like any other stage in the fallback chain.
"""

import json
import re

_LD_JSON_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def extract_json_ld_jobpostings(html: str) -> list[dict]:
    """Returns a list of raw Schema.org JobPosting dicts found anywhere on the page."""
    if not html:
        return []

    postings = []
    for block in _LD_JSON_BLOCK.findall(html):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue

        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("@type") == "JobPosting":
                postings.append(candidate)
            elif "@graph" in candidate and isinstance(candidate["@graph"], list):
                for node in candidate["@graph"]:
                    if isinstance(node, dict) and node.get("@type") == "JobPosting":
                        postings.append(node)

    return postings


def jobposting_to_fields(posting: dict) -> dict:
    """Maps a Schema.org JobPosting dict to this project's field names."""
    title = posting.get("title") or ""

    company = ""
    org = posting.get("hiringOrganization")
    if isinstance(org, dict):
        company = org.get("name") or ""
    elif isinstance(org, str):
        company = org

    location = ""
    loc = posting.get("jobLocation")
    if isinstance(loc, list) and loc:
        loc = loc[0]
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            location = ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressRegion")]))

    salary_range = ""
    base = posting.get("baseSalary")
    if isinstance(base, dict):
        value = base.get("value")
        if isinstance(value, dict):
            min_v, max_v, unit = value.get("minValue"), value.get("maxValue"), value.get("unitText", "")
            if min_v and max_v:
                salary_range = f"{min_v}-{max_v} {unit}".strip()

    return {"title": title, "company": company, "location": location, "salary_range": salary_range}
