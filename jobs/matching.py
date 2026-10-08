"""
Phase 3: compares a job's required skills against your Profile's skills
and produces a 0-100 match score. Deliberately plain Python logic -
no AI call here. Scoring thousands of jobs against your profile should
be instant and free, not something that costs an API call every time.
"""

from .models import Profile


def get_profile_for(user):
    """
    Returns the given user's Profile, or None if they haven't set one up
    yet (e.g. just signed up and haven't visited the profile page).
    """
    return Profile.objects.filter(user=user).first()


def match_breakdown(job_skills: list, profile_skills: list) -> dict:
    """
    Like calculate_match_score, but returns the actual evidence behind
    the number: which skills matched, and which are missing. This is
    what makes a match score verifiable instead of just a claim.
    """
    if not job_skills:
        return {"score": 0.0, "matched": [], "missing": []}

    # Keep the ORIGINAL casing for display (e.g. show "Python" not
    # "python"), but compare using a normalized lowercase lookup.
    profile_lookup = {s.strip().lower() for s in profile_skills}

    matched, missing = [], []
    for skill in job_skills:
        if skill.strip().lower() in profile_lookup:
            matched.append(skill)
        else:
            missing.append(skill)

    score = round((len(matched) / len(job_skills)) * 100, 1)
    return {"score": score, "matched": matched, "missing": missing}


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


def score_job(job, user) -> float:
    """
    Convenience wrapper: takes a JobPosting instance and the user pasting it,
    pulls that user's profile, and returns a match score. Returns 0.0 if
    they don't have a Profile yet (rather than raising an error - a missing
    profile shouldn't crash the extraction flow in views.py).
    """
    profile = get_profile_for(user)
    if profile is None:
        return 0.0
    return calculate_match_score(job.skills_required, profile.skills)
