from django.shortcuts import render, redirect
from django.contrib import messages

from .forms import PasteJobForm
from .models import JobPosting
from .extraction import extract_job_fields


def paste_job(request):
    """
    Phase 2 entry point: user pastes a raw job description.
    We save it immediately (so nothing is lost even if the AI call fails),
    then attempt AI extraction and update the same row with clean fields.
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
                job.save()
                messages.success(request, f"Extracted: {job.title} at {job.company}")
            except (ValueError, RuntimeError) as e:
                # The job row still exists with raw_text saved - extraction
                # can be retried later without losing anything.
                messages.warning(request, f"Saved, but AI extraction failed: {e}")

            return redirect("paste_job")
    else:
        form = PasteJobForm()

    recent_jobs = JobPosting.objects.all()[:10]
    return render(request, "jobs/paste_job.html", {"form": form, "recent_jobs": recent_jobs})
