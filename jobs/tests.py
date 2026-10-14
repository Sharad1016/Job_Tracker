from django.contrib.auth import get_user_model
from django.test import TestCase
from unittest.mock import patch, MagicMock

from jobs.models import JobPosting, Profile
from jobs.extraction import extract_job_fields
from jobs.scraping_service import run_scrape
from jobs.skills_dictionary import extract_skills_from_text
from jobs.regex_extractor import extract_structured_fields


User = get_user_model()


class DeterministicExtractionTests(TestCase):
    """Test that extraction works without Gemini using deterministic methods."""
    
    def test_extract_job_fields_deterministic_with_structured_format(self):
        """Test extraction from structured job posting format."""
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

        with patch("jobs.extraction._get_client", return_value=None):
            result = extract_job_fields(raw_text)

        self.assertEqual(result["title"], "Python Development")
        self.assertEqual(result["company"], "Qubixo")
        self.assertEqual(result["location"], "Work from home")
        self.assertIn("Python", result["skills_required"])
        self.assertIn("Django", result["skills_required"])

    def test_extract_job_fields_uses_skill_dictionary(self):
        """Test that skill dictionary is used during extraction."""
        raw_text = """
        Role: Backend Developer
        Company: TechCorp
        
        We need Python, Django, PostgreSQL, and Docker experience.
        REST API design is required.
        """

        with patch("jobs.extraction._get_client", return_value=None):
            result = extract_job_fields(raw_text)

        self.assertEqual(result["title"], "Backend Developer")
        self.assertEqual(result["company"], "TechCorp")
        # Skills should be detected via dictionary
        self.assertIn("Python", result["skills_required"])
        self.assertIn("Django", result["skills_required"])
        self.assertIn("PostgreSQL", result["skills_required"])
        self.assertIn("Docker", result["skills_required"])

    def test_extract_job_fields_does_not_call_gemini_when_deterministic_works(self):
        """Test that Gemini is NOT called when deterministic extraction fills critical fields."""
        raw_text = """
        Role: Frontend Engineer
        Company: WebCorp
        
        We need React, TypeScript, and Node.js experts.
        """

        mock_client = MagicMock()
        with patch("jobs.extraction._get_client", return_value=mock_client):
            result = extract_job_fields(raw_text)

        # Gemini should NOT be called because we have title and company
        mock_client.models.generate_content.assert_not_called()
        self.assertEqual(result["title"], "Frontend Engineer")
        self.assertEqual(result["company"], "WebCorp")


class GeminiGracefulDegradationTests(TestCase):
    """Test that Gemini quota errors don't crash the scraper."""
    
    def test_gemini_429_error_disables_future_calls(self):
        """Test that 429 error sets Gemini unavailable flag."""
        raw_text_1 = "Role: Job 1\nCompany: Corp1"
        raw_text_2 = "Role: Job 2\nCompany: Corp2"
        
        from google.genai import errors as genai_errors
        
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = genai_errors.APIError(
            code=429,
            response_json={"error": "RESOURCE_EXHAUSTED: Quota exhausted"}
        )
        
        with patch("jobs.extraction._get_client", return_value=mock_client), \
             patch("jobs.extraction._gemini_available", True):
            # First call with incomplete data that triggers Gemini
            raw_text_incomplete = "Build Python APIs"
            result1 = extract_job_fields(raw_text_incomplete)
            
            # After 429, Gemini should be marked unavailable
            from jobs import extraction
            self.assertFalse(extraction._gemini_available)

    def test_scraping_continues_after_gemini_unavailable(self):
        """Test that scraping doesn't crash when Gemini is unavailable."""
        def fake_scraper(keyword, max_results):
            return [
                {
                    "title": "Job 1",
                    "company": "Corp1",
                    "raw_text": "Role: Job 1\nCompany: Corp1",
                },
                {
                    "title": "Job 2",
                    "company": "Corp2",
                    "raw_text": "Role: Job 2\nCompany: Corp2",
                },
            ]

        user = User.objects.create_user(username="bob", password="pw")
        Profile.objects.create(
            user=user,
            skills=["Python"],
            target_role="Developer",
        )

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}):
            summary = run_scrape(user, sites=["internshala"], max_results_per_site=2)

        # Both jobs should be saved (no Gemini required)
        self.assertEqual(summary["added"], 2)
        self.assertEqual(JobPosting.objects.count(), 2)


class SkillDictionaryTests(TestCase):
    """Test skill dictionary extraction."""
    
    def test_extract_skills_finds_known_skills(self):
        """Test that known skills are extracted."""
        text = "We need Python, Django, PostgreSQL, and Docker."
        skills = extract_skills_from_text(text)
        
        self.assertIn("Python", skills)
        self.assertIn("Django", skills)
        self.assertIn("PostgreSQL", skills)
        self.assertIn("Docker", skills)

    def test_extract_skills_ignores_unknown_skills(self):
        """Test that unknown skills are not returned."""
        text = "We need Python and FakeTechXYZ."
        skills = extract_skills_from_text(text)
        
        self.assertIn("Python", skills)
        self.assertNotIn("FakeTechXYZ", skills)


class RegexExtractionTests(TestCase):
    """Test regex/rule-based extraction."""
    
    def test_extract_salary_range(self):
        """Test salary extraction."""
        text = "₹3-5 LPA"
        fields = extract_structured_fields(text)
        self.assertEqual(fields["salary_range"], "₹3-5 LPA")

    def test_extract_experience(self):
        """Test experience extraction."""
        text = "2-5 years of experience required"
        fields = extract_structured_fields(text)
        self.assertIn("2", fields["experience"])
        self.assertIn("5", fields["experience"])


class RunScrapeTests(TestCase):
    """Test scraping service with new architecture."""
    
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.profile = Profile.objects.create(
            user=self.user,
            skills=["Python", "Django"],
            target_role="Python Developer",
            target_role_category="software_engineering",
        )

    def test_run_scrape_saves_all_jobs_regardless_of_role(self):
        """Test that all extractable jobs are saved (no role filtering)."""
        def fake_scraper(keyword, max_results):
            return [
                {
                    "title": "Python Developer",
                    "company": "TechCorp",
                    "raw_text": "Role: Python Developer\nCompany: TechCorp",
                },
                {
                    "title": "Sales Engineer",
                    "company": "SalesCorp",
                    "raw_text": "Role: Sales Engineer\nCompany: SalesCorp",
                },
            ]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}):
            summary = run_scrape(self.user, sites=["internshala"], max_results_per_site=2)

        # Both jobs should be saved (no role filtering)
        self.assertEqual(summary["added"], 2)
        self.assertEqual(JobPosting.objects.count(), 2)
        # Summary should not have skipped_wrong_role anymore
        self.assertNotIn("skipped_wrong_role", summary)

    def test_run_scrape_skips_duplicates(self):
        """Test that duplicate jobs are skipped."""
        # Pre-create a job
        JobPosting.objects.create(
            raw_text="Original",
            title="Python Developer",
            company="TechCorp",
        )

        def fake_scraper(keyword, max_results):
            return [
                {
                    "title": "Python Developer",
                    "company": "TechCorp",
                    "raw_text": "Role: Python Developer\nCompany: TechCorp",
                },
            ]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}):
            summary = run_scrape(self.user, sites=["internshala"], max_results_per_site=1)

        self.assertEqual(summary["added"], 0)
        self.assertEqual(summary["skipped_duplicate"], 1)

    def test_run_scrape_does_not_save_job_when_critical_fields_missing(self):
        """Test that jobs without title/company are not saved."""
        def fake_scraper(keyword, max_results):
            return [
                {
                    "title": "",
                    "company": "",
                    "raw_text": "No title or company info",
                },
            ]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}):
            summary = run_scrape(self.user, sites=["internshala"], max_results_per_site=1)

        self.assertEqual(summary["added"], 0)
        # Job is skipped at validation (before extraction) so not counted as failed_extraction
        self.assertEqual(summary["failed_extraction"], 0)
        # But there should be an error message about missing title/company
        self.assertTrue(any("Missing title/company" in err for err in summary["errors"]))
        self.assertEqual(JobPosting.objects.count(), 0)

