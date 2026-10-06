from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required

from .forms import PasteJobForm, SignupForm, ProfileForm
from .models import JobPosting, Profile
from .extraction import extract_job_fields
from .matching import score_job, get_profile_for


def home(request):
    """
    Public landing page. Logged-in users don't need to see a sales
    pitch for a tool they already use, so we send them straight to
    the page they actually want.
    """
    if request.user.is_authenticated:
        return redirect("paste_job")
    return render(request, "jobs/home.html")


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
    """
    Custom profile page - replaces editing Profile through /admin/.
    get_or_create handles the edge case where a Profile somehow
    doesn't exist yet for this user (shouldn't happen via signup,
    but defensive here in case of e.g. an old account from before
    Profile existed).
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        form = ProfileForm(request.POST)
        if form.is_valid():
            profile.skills = form.skills_as_list()
            profile.save()
            messages.success(request, "Profile updated.")
            return redirect("profile_edit")
    else:
        # Pre-fill the textbox with existing skills, converted back to
        # comma-separated text for editing.
        form = ProfileForm(initial={"skills_text": ", ".join(profile.skills)})

    return render(request, "jobs/profile_form.html", {"form": form, "profile": profile})


@login_required
def paste_job(request):
    """
    Phase 2 entry point: user pastes a raw job description.
    We save it immediately (so nothing is lost even if the AI call fails),
    then attempt AI extraction and update the same row with clean fields.
    Now requires login, and scores against the logged-in user's profile
    specifically, not a single global one.
    """
    if request.method == "POST":
        form = PasteJobForm(request.POST)
        if form.is_valid():
            raw_text = form.cleaned_data["raw_text"]

            # Save raw text first - the AI call happens after, never before,
            # so a failed API call never costs us the original data.
            job = JobPosting.objects.create(raw_text=raw_text, source="manual")

            try:
                extracted = extract_job_fields(raw_text)
                job.title = extracted["title"]
                job.company = extracted["company"]
                job.location = extracted["location"]
                job.skills_required = extracted["skills_required"]
                job.salary_range = extracted["salary_range"]
                job.deadline = extracted["deadline"]

                # Score against the logged-in user's profile.
                job.match_score = score_job(job, request.user)

                job.save()
                messages.success(
                    request,
                    f"Extracted: {job.title} at {job.company} (match: {job.match_score}%)",
                )
            except (ValueError, RuntimeError) as e:
                # The job row still exists with raw_text saved - extraction
                # can be retried later without losing anything.
                messages.warning(request, f"Saved, but AI extraction failed: {e}")

            return redirect("paste_job")
    else:
        form = PasteJobForm()

    recent_jobs = JobPosting.objects.order_by("-created_at")[:10]
    profile = get_profile_for(request.user)
    profile_exists = bool(profile and profile.skills)
    return render(
        request,
        "jobs/paste_job.html",
        {"form": form, "recent_jobs": recent_jobs, "profile_exists": profile_exists},
    )
