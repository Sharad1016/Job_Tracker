from django.shortcuts import render, redirect, get_object_or_404
from django.http import Http404
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Avg, Q

from .forms import PasteJobForm, SignupForm, ProfileForm, ResumeUploadForm
from .models import JobPosting, Profile, Application
from .extraction import extract_job_fields
from .resume_parser import extract_text_from_file, extract_skills_from_resume
from .matching import score_job, get_profile_for, match_breakdown

# Palette for the letter-avatar circles on job cards. Picked once per
# company via a hash, so the same company always gets the same color
# within a session (not saved to the DB - purely a display detail).
LOGO_COLORS = ["#EA4335", "#4285F4", "#34A853", "#FBBC05", "#6D5DFC", "#F97316", "#10B981", "#EC4899"]


def _logo_color(company: str) -> str:
    if not company:
        return "#9494A6"
    return LOGO_COLORS[hash(company) % len(LOGO_COLORS)]


def home(request):
    """
    Public landing page. Logged-in users don't need to see a sales
    pitch for a tool they already use, so we send them straight to
    the page they actually want. Stats shown are real DB numbers,
    not placeholder marketing figures.
    """
    if request.user.is_authenticated:
        return redirect("job_matches")

    avg_match = JobPosting.objects.aggregate(avg=Avg("match_score"))["avg"] or 0
    stats = {
        "jobs_tracked": JobPosting.objects.count(),
        "active_users": User.objects.count(),
        "avg_match": round(avg_match, 1),
    }
    return render(request, "jobs/home.html", {"stats": stats})


def signup_view(request):
    """
    Django ships LoginView/LogoutView but no signup view - this is
    the one piece of auth we have to write ourselves. We reuse
    UserCreationForm (via SignupForm) rather than rolling our own
    password validation logic.
    """
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()  # creates the actual User row
            Profile.objects.create(user=user, skills=[])  # empty profile, ready to fill in
            login(request, user)  # sign them in immediately, no separate login step
            messages.success(request, "Account created! Add your skills to get started.")
            return redirect("profile_edit")
    else:
        form = SignupForm()

    return render(request, "jobs/signup.html", {"form": form})


@login_required
def profile_edit(request):
    """Custom profile page - replaces editing Profile through /admin/."""
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        form = ProfileForm(request.POST)
        if form.is_valid():
            profile.skills = form.skills_as_list()
            profile.target_role = form.cleaned_data["target_role"]
            profile.target_role_category = form.cleaned_data["target_role_category"]
            profile.save()
            messages.success(request, "Profile updated.")
            return redirect("profile_edit")
    else:
        form = ProfileForm(initial={
            "skills_text": ", ".join(profile.skills),
            "target_role": profile.target_role,
            "target_role_category": profile.target_role_category,
        })

    return render(
        request,
        "jobs/profile_form.html",
        {
            "form": form,
            "resume_form": ResumeUploadForm(),
            "profile": profile,
            "active_nav": "profile",
        },
    )


@login_required
def upload_resume(request):
    """
    Parses an uploaded resume and MERGES extracted skills into the
    existing profile - never overwrites. Case-insensitive dedup so
    "python" from the resume doesn't duplicate "Python" already there.
    """
    if request.method != "POST":
        return redirect("profile_edit")

    form = ResumeUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Please choose a file to upload.")
        return redirect("profile_edit")

    profile, _ = Profile.objects.get_or_create(user=request.user)

    try:
        text = extract_text_from_file(request.FILES["resume_file"])
        new_skills = extract_skills_from_resume(text)
    except (ValueError, RuntimeError) as e:
        messages.error(request, str(e))
        return redirect("profile_edit")

    existing_lower = {s.strip().lower() for s in profile.skills}
    added = [s for s in new_skills if s.strip().lower() not in existing_lower]

    profile.skills = profile.skills + added
    profile.save()

    if added:
        messages.success(request, f"Added {len(added)} skill(s) from your resume: {', '.join(added)}")
    else:
        messages.warning(request, "No new skills found in that resume beyond what's already listed.")

    return redirect("profile_edit")


@login_required
def paste_job(request):
    """
    The "Add Job" form. Save-first, extract-second, same pattern as
    before. On success, sends the user to Job Matches so they see the
    result in the polished list rather than a bare confirmation.
    """
    if request.method == "POST":
        form = PasteJobForm(request.POST)
        if form.is_valid():
            raw_text = form.cleaned_data["raw_text"]
            job = JobPosting.objects.create(raw_text=raw_text, source="manual")

            try:
                extracted = extract_job_fields(raw_text)
                job.title = extracted["title"]
                job.company = extracted["company"]
                job.location = extracted["location"]
                job.skills_required = extracted["skills_required"]
                job.requirements = extracted["requirements"]
                job.salary_range = extracted["salary_range"]
                job.deadline = extracted["deadline"]
                job.role_category = extracted["role_category"]
                job.match_score = score_job(job, request.user)
                job.save()
                messages.success(
                    request,
                    f"Extracted: {job.title} at {job.company} (match: {job.match_score}%)",
                )
                return redirect("job_matches")
            except (ValueError, RuntimeError) as e:
                messages.warning(request, f"Saved, but AI extraction failed: {e}")
                return redirect("job_matches")
    else:
        form = PasteJobForm()

    return render(
        request, "jobs/paste_job.html", {"form": form, "active_nav": "matches"}
    )


@login_required
def job_matches(request):
    """
    The main browsing page - card list of every job, sorted by match
    score by default, with a simple title/company search. This is
    the page the mockup calls "Job Matches".
    """
    jobs = JobPosting.objects.all()  # uses Meta.ordering: -match_score, -created_at

    query = request.GET.get("q", "").strip()
    if query:
        jobs = jobs.filter(Q(title__icontains=query) | Q(company__icontains=query))

    # Which jobs the logged-in user has already saved, so the button
    # can show "Saved" instead of "Save" without a query per card.
    saved_job_ids = set(
        Application.objects.filter(user=request.user, status="saved").values_list("job_id", flat=True)
    )

    # IMPORTANT: score live against THIS user's profile, not the stored
    # job.match_score. JobPosting is shared across all accounts, but
    # skills are per-user - the stored field only reflects whoever's
    # profile was used at extraction/scrape time, which may not be you.
    profile = get_profile_for(request.user)
    profile_skills = profile.skills if profile else []

    # Live per-viewer role filtering: JobPosting is a shared pool across
    # ALL accounts (same principle as the live scoring above - the
    # stored fields on a job never change per-viewer, but what a given
    # viewer SEES does). role_category is stored permanently on every
    # job at extraction time and never changes; what changes here is
    # WHICH jobs a given viewer sees, filtered against THEIR OWN
    # current target_role_category, evaluated fresh on every request.
    # If a user changes their target role, this list updates
    # immediately with no re-scrape needed - the same job row is just
    # included/excluded differently depending on who's looking at it.
    if profile and profile.target_role_category:
        jobs = jobs.filter(role_category=profile.target_role_category)

    jobs_with_display = []
    for job in jobs:
        breakdown = match_breakdown(job.skills_required, profile_skills)
        jobs_with_display.append({
            "job": job,
            "logo_color": _logo_color(job.company),
            "is_saved": job.id in saved_job_ids,
            "live_score": breakdown["score"],
        })

    # Re-sort by the live score, not the stale stored one, so "Best
    # Match" ordering is correct for whoever's actually looking.
    jobs_with_display.sort(key=lambda x: x["live_score"], reverse=True)

    profile_exists = bool(profile and profile.skills)

    return render(
        request,
        "jobs/job_matches.html",
        {
            "jobs_with_display": jobs_with_display,
            "query": query,
            "profile_exists": profile_exists,
            "profile": profile,
            "active_nav": "matches",
        },
    )


@login_required
def job_detail(request, job_id):
    """
    Full detail view for one job - the page the mockup shows when you
    click into a match. Match Breakdown only shows Skills as a real
    computed row; other categories from the mockup (Experience,
    Education, Keywords, Location) aren't implemented yet, so they're
    not faked here.
    """
    job = get_object_or_404(JobPosting, id=job_id)
    is_saved = Application.objects.filter(user=request.user, job=job, status="saved").exists()

    profile = get_profile_for(request.user)
    profile_skills = profile.skills if profile else []
    breakdown = match_breakdown(job.skills_required, profile_skills)
    live_score = breakdown["score"]

    # SVG circle math for the match-score ring: circumference = 2*pi*r,
    # and we reveal a fraction of it proportional to the score.
    radius = 42
    circumference = round(2 * 3.14159 * radius, 1)
    ring_offset = round(circumference * (1 - live_score / 100), 1)

    return render(
        request,
        "jobs/job_detail.html",
        {
            "job": job,
            "logo_color": _logo_color(job.company),
            "is_saved": is_saved,
            "active_nav": "matches",
            "ring_circumference": circumference,
            "ring_offset": ring_offset,
            "live_score": live_score,
            "matched_skills": breakdown["matched"],
            "missing_skills": breakdown["missing"],
        },
    )


@login_required
def toggle_save(request, job_id):
    """
    Save/unsave a job for THIS user specifically. Creates or removes
    their (user, job) Application row.

    NOTE: fixed a latent bug here while touching this code - the old
    version only ever looked for an existing Application with
    status="saved" before deciding whether to create one. If a job's
    Application had already moved to "applied"/"oa"/"interview" (now
    reachable directly from the nav instead of a separate Kanban page,
    so users will hit this far more often), clicking "Save Job" again
    would try to create a SECOND Application for the same (user, job)
    pair and crash on the unique_together constraint. Now it checks
    for ANY existing Application for this job first.
    """
    if request.method == "POST":
        job = get_object_or_404(JobPosting, id=job_id)
        existing = Application.objects.filter(user=request.user, job=job).first()
        if existing:
            if existing.status == "saved":
                existing.delete()
                messages.success(request, f"Removed {job.title} from saved jobs.")
            else:
                messages.info(
                    request,
                    f"{job.title} is already being tracked as '{existing.get_status_display()}'.",
                )
        else:
            Application.objects.create(user=request.user, job=job, status="saved")
            messages.success(request, f"Saved {job.title}.")

    next_url = request.POST.get("next") or "job_matches"
    return redirect(next_url)


@login_required
def saved_jobs(request):
    """Lists jobs THIS user has saved."""
    applications = Application.objects.filter(user=request.user, status="saved").select_related("job")
    jobs_with_display = [
        {"job": app.job, "logo_color": _logo_color(app.job.company), "application": app}
        for app in applications
    ]
    return render(
        request,
        "jobs/saved_jobs.html",
        {"jobs_with_display": jobs_with_display, "active_nav": "saved"},
    )


# Labels for the statuses that now live directly in the nav (replacing
# the old Kanban board). "saved" isn't here since Saved Jobs already
# has its own dedicated page/view above.
NAV_STATUS_LABELS = {
    "applied": "Applied",
    "oa": "Online Assessment",
    "interview": "Interview",
}


@login_required
def applications_by_status(request, status):
    """
    Replaces the Kanban board: instead of one page with every status
    as a column, each status (Applied / Online Assessment / Interview)
    is now its own nav item and page, listing just this user's
    Applications at that status. A status-change dropdown on each card
    (same underlying update_application_status view the Kanban board
    used) lets the user move a job to a different stage directly from
    the list, without a separate board page.
    """
    if status not in NAV_STATUS_LABELS:
        raise Http404("Unknown application status.")

    applications = (
        Application.objects.filter(user=request.user, status=status)
        .select_related("job")
        .order_by("-updated_at")
    )
    jobs_with_display = [
        {"job": app.job, "logo_color": _logo_color(app.job.company), "application": app}
        for app in applications
    ]

    return render(
        request,
        "jobs/status_list.html",
        {
            "jobs_with_display": jobs_with_display,
            "status_label": NAV_STATUS_LABELS[status],
            "active_nav": status,
        },
    )


@login_required
def update_application_status(request, application_id):
    """
    Moves an Application to a new status. Previously always redirected
    back to the (now removed) Kanban board; now redirects back to
    wherever the request came from (the Saved Jobs page, or one of the
    Applied/Online Assessment/Interview pages), via the same `next`
    hidden-field pattern used by toggle_save.
    """
    if request.method == "POST":
        application = get_object_or_404(Application, id=application_id, user=request.user)
        new_status = request.POST.get("status")
        if new_status in dict(Application.STATUS_CHOICES):
            application.status = new_status
            application.save()
            messages.success(request, f"Moved to {application.get_status_display()}.")

    next_url = request.POST.get("next") or "saved_jobs"
    return redirect(next_url)


@login_required
def run_scraper(request):
    """
    The 'Fetch New Jobs' button - runs the exact same multi-site scrape
    the CLI's scrape_jobs command does (via scraping_service.run_scrape),
    so no terminal is needed for day-to-day use.

    IMPORTANT KNOWN LIMITATION: this runs SYNCHRONOUSLY inside the
    request/response cycle. Scraping with Playwright + an AI
    extraction call per new listing is genuinely slow (can be 30s-2min+
    depending on how many new jobs are found), so the browser tab will
    just sit on a loading spinner for that whole time - there's no
    progress bar or background task queue (e.g. Celery) here. That's a
    deliberate simplification appropriate for a personal/demo project -
    NOT how this should be architected for real production traffic
    with many concurrent users clicking the button at once.
    """
    if request.method == "POST":
        from .scraping_service import run_scrape, SITE_SCRAPERS

        site_param = request.POST.get("site", "all")
        sites = None if site_param == "all" else [site_param]

        # Phase 16: per-site limit instead of one shared number - 8+8=16
        # total, not 16 requested from each site individually.
        summary = run_scrape(request.user, sites=sites, max_results_per_site={"internshala": 8, "workindia": 8})

        if summary["added"]:
            # summary["added"] is already the correct combined total -
            # it's incremented once per successfully-SAVED job across
            # ALL sites (see scraping_service.py Phase 2), not per site.
            # Deliberately not showing the site-by-site breakdown here
            # anymore (Phase 16) - just one clean combined count.
            messages.success(
                request,
                f"{summary['added']} new job(s) found matching '{summary['keyword']}'.",
            )
        else:
            messages.warning(
                request,
                f"No new matching jobs found for '{summary['keyword']}' this time "
                f"(skipped {summary['skipped_duplicate']} duplicate(s), "
                f"{summary['skipped_wrong_role']} off-role result(s)).",
            )
        for err in summary["errors"]:
            messages.warning(request, f"Scraper issue: {err}")

    return redirect("job_matches")
