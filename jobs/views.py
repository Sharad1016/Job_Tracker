from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Avg, Q

from .forms import PasteJobForm, SignupForm, ProfileForm
from .models import JobPosting, Profile, Application
from .extraction import extract_job_fields
from .matching import score_job, get_profile_for

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
            profile.save()
            messages.success(request, "Profile updated.")
            return redirect("profile_edit")
    else:
        form = ProfileForm(initial={"skills_text": ", ".join(profile.skills)})

    return render(
        request,
        "jobs/profile_form.html",
        {"form": form, "profile": profile, "active_nav": "profile"},
    )


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
        Application.objects.filter(status="saved").values_list("job_id", flat=True)
    )

    jobs_with_display = [
        {"job": job, "logo_color": _logo_color(job.company), "is_saved": job.id in saved_job_ids}
        for job in jobs
    ]

    profile = get_profile_for(request.user)
    profile_exists = bool(profile and profile.skills)

    return render(
        request,
        "jobs/job_matches.html",
        {
            "jobs_with_display": jobs_with_display,
            "query": query,
            "profile_exists": profile_exists,
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
    is_saved = Application.objects.filter(job=job, status="saved").exists()

    # SVG circle math for the match-score ring: circumference = 2*pi*r,
    # and we reveal a fraction of it proportional to the score.
    radius = 42
    circumference = round(2 * 3.14159 * radius, 1)
    ring_offset = round(circumference * (1 - job.match_score / 100), 1)

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
        },
    )


@login_required
def toggle_save(request, job_id):
    """
    Save/unsave a job. Creates or removes its Application(status='saved').
    This is deliberately the ONLY Application-related feature built so
    far - full status tracking (Applied/Interview/Offer) is Phase 7's
    Kanban board, not built yet.
    """
    if request.method == "POST":
        job = get_object_or_404(JobPosting, id=job_id)
        existing = Application.objects.filter(job=job, status="saved").first()
        if existing:
            existing.delete()
            messages.success(request, f"Removed {job.title} from saved jobs.")
        else:
            Application.objects.create(job=job, status="saved")
            messages.success(request, f"Saved {job.title}.")

    next_url = request.POST.get("next") or "job_matches"
    return redirect(next_url)


@login_required
def saved_jobs(request):
    """Lists every job currently saved (Application.status == 'saved')."""
    applications = Application.objects.filter(status="saved").select_related("job")
    jobs_with_display = [
        {"job": app.job, "logo_color": _logo_color(app.job.company)} for app in applications
    ]
    return render(
        request,
        "jobs/saved_jobs.html",
        {"jobs_with_display": jobs_with_display, "active_nav": "saved"},
    )
