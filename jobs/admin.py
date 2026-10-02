from django.contrib import admin
from .models import JobPosting, Application


@admin.register(JobPosting)
class JobPostingAdmin(admin.ModelAdmin):
    list_display = ("title", "company", "match_score", "source", "created_at")
    list_filter = ("source",)
    search_fields = ("title", "company")


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("job", "status", "applied_date", "updated_at")
    list_filter = ("status",)
