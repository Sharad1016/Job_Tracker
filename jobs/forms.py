from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import ROLE_CATEGORY_CHOICES


class PasteJobForm(forms.Form):
    raw_text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 12, "placeholder": "Paste the full job description here..."}),
        label="Job Description",
    )


class SignupForm(UserCreationForm):
    """
    Django's UserCreationForm already handles username + two-password
    confirmation + validation correctly - we're not reinventing that,
    just using it as-is so signup gets that behavior for free.
    """
    pass


class ProfileForm(forms.Form):
    """
    A plain Form, not a ModelForm, on purpose: Profile.skills is a
    JSONField storing a Python list, but asking a user to type raw
    JSON like ["Python", "SQL"] in a browser is bad UX and error-prone.
    This form shows/accepts a simple comma-separated string instead,
    and the view converts between the two representations.
    """
    skills_text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Python, Django, Flask, SQL, Git"}),
        label="Your skills (comma-separated)",
        required=False,
    )
    target_role = forms.CharField(
        max_length=150,
        required=False,
        label="Target role (used as the search term when fetching jobs)",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Backend Developer"}),
    )
    target_role_category = forms.ChoiceField(
        choices=ROLE_CATEGORY_CHOICES,
        label="Target field (jobs outside this field are filtered out automatically)",
    )

    def skills_as_list(self):
        """Converts the submitted comma-separated text into a clean list."""
        raw = self.cleaned_data.get("skills_text", "")
        return [s.strip() for s in raw.split(",") if s.strip()]


class ResumeUploadForm(forms.Form):
    resume_file = forms.FileField(
        label="Upload resume (PDF or DOCX)",
        help_text="AI will pull out your skills and add them to the list below - review before saving.",
    )
