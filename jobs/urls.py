from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("add/", views.paste_job, name="paste_job"),
    path("matches/", views.job_matches, name="job_matches"),
    path("matches/<int:job_id>/", views.job_detail, name="job_detail"),
    path("matches/<int:job_id>/save/", views.toggle_save, name="toggle_save"),
    path("saved/", views.saved_jobs, name="saved_jobs"),
    path("board/", views.kanban_board, name="kanban_board"),
    path("board/<int:application_id>/status/", views.update_application_status, name="update_application_status"),
    path("signup/", views.signup_view, name="signup"),
    path("profile/", views.profile_edit, name="profile_edit"),
    path("profile/upload-resume/", views.upload_resume, name="upload_resume"),
]
