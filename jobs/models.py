from django.conf import settings
from django.db import models


class Profile(models.Model):
    """
    Represents one signed-up user of this tracker. Linked to Django's
    built-in User model with OneToOneField, so each account gets its
    own skills list - this only became necessary once real login/signup
    existed; before that, a single unowned Profile row was fine.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    skills = models.JSONField(
        default=list,
        blank=True,
        help_text='Your skills, e.g. ["Python", "Django", "Flask", "SQL"]',
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}'s profile"


class JobPosting(models.Model):
    """
    One row = one job posting, whether pasted manually (Phase 2)
    or pulled in later by the scraper (Phase 5).
    """

    # --- Raw input ---
    raw_text = models.TextField(
        help_text="The full job description as pasted or scraped, before AI cleans it up."
    )

    # --- Fields the AI extraction layer (Phase 2) will fill in ---
    title = models.CharField(max_length=200, blank=True)
    company = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=150, blank=True)
    skills_required = models.JSONField(
        default=list,
        blank=True,
        help_text='List of skills, e.g. ["Python", "Flask", "SQL"]',
    )
    requirements = models.JSONField(
        default=list,
        blank=True,
        help_text='List of requirement bullet points, e.g. ["Bachelor\'s degree", "2+ years experience"]',
    )
    salary_range = models.CharField(max_length=100, blank=True)
    deadline = models.DateField(null=True, blank=True)
    application_url = models.URLField(
        blank=True,
        help_text="Optional link to apply. Left blank for pasted jobs unless you provide one.",
    )

    # --- Fields the matching engine (Phase 3) will fill in ---
    match_score = models.FloatField(
        default=0.0,
        help_text="0-100 score of how well this job fits my resume.",
    )

    # --- Bookkeeping ---
    source = models.CharField(
        max_length=50,
        default="manual",
        help_text="'manual' for pasted jobs, 'internshala'/'naukri' once the scraper is added.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title or 'Untitled'} @ {self.company or 'Unknown'}"

    class Meta:
        ordering = ["-match_score", "-created_at"]


class Application(models.Model):
    """
    One row = ONE USER'S application to a specific JobPosting.

    Changed from a plain OneToOneField(JobPosting) to a (user, job) pair -
    JobPosting is shared across everyone (like match_score used to be),
    but tracking status ("I applied", "I got an interview") is inherently
    personal. Two different users need to be able to independently track
    the SAME shared job without colliding.
    """

    STATUS_CHOICES = [
        ("saved", "Saved"),
        ("applied", "Applied"),
        ("oa", "Online Assessment"),
        ("interview", "Interview"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="applications",
    )
    job = models.ForeignKey(
        JobPosting,
        on_delete=models.CASCADE,
        related_name="applications",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="saved",
    )
    applied_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # One user can only have ONE Application per job - re-saving
        # or re-applying should update the existing row, not create
        # a second one for the same (user, job) pair.
        unique_together = [("user", "job")]

    def __str__(self):
        return f"{self.job} - {self.get_status_display()}"
