from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("add/", views.paste_job, name="paste_job"),
    path("matches/", views.job_matches, name="job_matches"),
    path("matches/<int:job_id>/", views.job_detail, name="job_detail"),
    path("matches/<int:job_id>/save/", views.toggle_save, name="toggle_save"),
    path("saved/", views.saved_jobs, name="saved_jobs"),
    path("applied/", views.applications_by_status, {"status": "applied"}, name="applications_applied"),
    path("online-assessment/", views.applications_by_status, {"status": "oa"}, name="applications_oa"),
    path("interview/", views.applications_by_status, {"status": "interview"}, name="applications_interview"),
    path("applications/<int:application_id>/status/", views.update_application_status, name="update_application_status"),
    path("signup/", views.signup_view, name="signup"),
    path("profile/", views.profile_edit, name="profile_edit"),
    path("profile/upload-resume/", views.upload_resume, name="upload_resume"),
]
