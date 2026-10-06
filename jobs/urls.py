from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("paste/", views.paste_job, name="paste_job"),
    path("signup/", views.signup_view, name="signup"),
    path("profile/", views.profile_edit, name="profile_edit"),
]
