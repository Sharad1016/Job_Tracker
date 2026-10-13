from django.contrib.auth import get_user_model
from django.test import TestCase
from unittest.mock import patch

from jobs.models import JobPosting, Profile
from jobs.extraction import extract_job_fields
from jobs.scraping_service import run_scrape


User = get_user_model()


class ExtractJobFieldsFallbackTests(TestCase):
    def test_extract_job_fields_uses_raw_text_fallback_when_ai_fails(self):
        raw_text = """
        Role: Python Development
        Company: Qubixo
        Location: Work from home
        Stipend: ₹10,000 - ₹20,000/month

        About the job:
        Build APIs and internal tools.

        Skills:
        Python, Django, Flask, REST API
        """

        with patch("jobs.extraction._get_client", side_effect=RuntimeError("Gemini down")):
            result = extract_job_fields(raw_text)

        self.assertEqual(result["title"], "Python Development")
        self.assertEqual(result["company"], "Qubixo")
        self.assertEqual(result["location"], "Work from home")
        self.assertIn("Python", result["skills_required"])
        self.assertIn("Django", result["skills_required"])


class RunScrapeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.profile = Profile.objects.create(
            user=self.user,
            skills=["Python", "Django"],
            target_role="Python Developer",
            target_role_category="software_engineering",
        )

    def test_run_scrape_does_not_save_a_job_when_extraction_fails(self):
        def fake_scraper(keyword, max_results):
            return [{
                "title": "Python Developer",
                "company": "Qubixo",
                "raw_text": "Role: Python Developer\nCompany: Qubixo\nLocation: Remote",
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}), \
             patch("jobs.scraping_service.extract_job_fields", side_effect=RuntimeError("AI failed")):
            summary = run_scrape(self.user, sites=["internshala"], max_results_per_site=3)

        self.assertEqual(JobPosting.objects.count(), 0)
        self.assertEqual(summary["failed_extraction"], 1)
        self.assertIn("AI failed", "\n".join(summary["errors"]))
