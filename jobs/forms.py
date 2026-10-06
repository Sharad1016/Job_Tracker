from django import forms
from django.contrib.auth.forms import UserCreationForm


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

    def skills_as_list(self):
        """Converts the submitted comma-separated text into a clean list."""
        raw = self.cleaned_data.get("skills_text", "")
        return [s.strip() for s in raw.split(",") if s.strip()]
