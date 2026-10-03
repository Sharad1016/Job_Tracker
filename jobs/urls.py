from django.urls import path
from . import views

urlpatterns = [
    path("", views.paste_job, name="paste_job"),
]
