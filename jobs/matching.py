"""
Phase 3: compares a job's required skills against your Profile's skills
and produces a 0-100 match score. Deliberately plain Python logic -
no AI call here. Scoring thousands of jobs against your profile should
be instant and free, not something that costs an API call every time.
"""

from .models import Profile


def get_my_profile():
    """
    Returns the single Profile this tracker is built around, or None
    if one hasn't been created yet. We don't enforce "only one Profile"
    at the database level (see models.py) - this function just always
    reads the first one, which is all a single-user tool needs.
    """
    return Profile.objects.first()


def calculate_match_score(job_skills: list, profile_skills: list) -> float:
    """
    Returns what percentage of the job's required skills you actually have.

    Comparison is case-insensitive ("python" == "Python") since AI-extracted
    and manually-entered skill names won't always match casing exactly.

    Returns 0.0 if the job lists no skills at all - there's nothing to
    score a match against, so 0 is more honest than skipping the job
    or guessing.
    """
    if not job_skills:
        return 0.0

    # Normalize both sides to lowercase, stripped of extra whitespace,
    # so "Python " and "python" are treated as the same skill.
    job_skills_normalized = {s.strip().lower() for s in job_skills}
    profile_skills_normalized = {s.strip().lower() for s in profile_skills}

    matched = job_skills_normalized & profile_skills_normalized  # set intersection
    score = (len(matched) / len(job_skills_normalized)) * 100

    return round(score, 1)


def score_job(job) -> float:
    """
    Convenience wrapper: takes a JobPosting instance, pulls your profile,
    and returns its match score. Returns 0.0 if no Profile exists yet
    (rather than raising an error - a missing profile shouldn't crash
    the extraction flow in views.py).
    """
    profile = get_my_profile()
    if profile is None:
        return 0.0
    return calculate_match_score(job.skills_required, profile.skills)
