from django import forms


class PasteJobForm(forms.Form):
    raw_text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 12, "placeholder": "Paste the full job description here..."}),
        label="Job Description",
    )
