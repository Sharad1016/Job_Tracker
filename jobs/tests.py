from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from unittest.mock import patch

from jobs.models import JobPosting, Profile
from jobs.extraction import extract_job_fields
from jobs.extraction_pipeline import GeminiRunState, process_scraped_item
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


class ExtractionPipelineTests(TestCase):
    """
    Phase 12 (deterministic-first extraction refactor): verifies the
    core promise of the refactor - a structurally complete scraped
    item never needs to call Gemini for FIELD extraction at all, only
    (optionally) for role classification.
    """

    def test_complete_item_never_calls_gemini_field_extraction(self):
        item = {
            "title": "Python Developer",
            "company": "Qubixo",
            "raw_text": (
                "Role: Python Developer\nCompany: Qubixo\nLocation: Remote\n"
                "Stipend: ₹4-6 LPA\n\nSkills:\nPython, Django, REST API"
            ),
            "json_ld": [],
        }
        gemini_state = GeminiRunState()

        with patch("jobs.extraction_pipeline.extract_job_fields") as mock_extract, \
             patch("jobs.extraction_pipeline.classify_role_category", return_value=("software_engineering", None)):
            job, log = process_scraped_item(item, "internshala", gemini_state)

        mock_extract.assert_not_called()  # the full-field Gemini fallback must NEVER be called here
        self.assertIsNotNone(job)
        self.assertEqual(job["title"], "Python Developer")
        self.assertEqual(job["company"], "Qubixo")
        self.assertIn("Python", job["skills_required"])
        self.assertIn("Django", job["skills_required"])
        self.assertTrue(any("Gemini not required" in line for line in log))

    def test_quota_error_disables_gemini_for_rest_of_run_and_scraping_continues(self):
        """A 429 on job 1's classification must not crash job 2, and must
        stop further Gemini calls for the remainder of the run."""
        item1 = {
            "title": "Backend Engineer", "company": "Acme",
            "raw_text": "Role: Backend Engineer\nCompany: Acme\nSkills: Python, Django",
            "json_ld": [],
        }
        item2 = {
            "title": "Software Developer", "company": "Beta Corp",
            "raw_text": "Role: Software Developer\nCompany: Beta Corp\nSkills: Java",
            "json_ld": [],
        }
        gemini_state = GeminiRunState()

        with patch(
            "jobs.extraction_pipeline.classify_role_category",
            return_value=("", "429 RESOURCE_EXHAUSTED: quota exceeded"),
        ) as mock_classify:
            job1, log1 = process_scraped_item(item1, "internshala", gemini_state)
            self.assertFalse(gemini_state.available)  # disabled after job 1's 429
            job2, log2 = process_scraped_item(item2, "naukri", gemini_state)

        self.assertIsNotNone(job1)
        self.assertIsNotNone(job2)
        # classify_role_category must only have been attempted ONCE (job 1) -
        # job 2 should have used the keyword fallback instead, since Gemini
        # was already disabled by the time it was processed.
        mock_classify.assert_called_once()
        self.assertEqual(job1["role_category"], "software_engineering")
        self.assertEqual(job2["role_category"], "software_engineering")


class RunScrapeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.profile = Profile.objects.create(
            user=self.user,
            skills=["Python", "Django"],
            target_role="Python Developer",
            target_role_category="software_engineering",
        )

    def test_run_scrape_saves_a_structurally_complete_job_without_gemini_field_extraction(self):
        def fake_scraper(keyword, max_results):
            return [{
                "title": "Python Developer",
                "company": "Qubixo",
                "raw_text": "Role: Python Developer\nCompany: Qubixo\nLocation: Remote\nSkills: Python, Django",
                "json_ld": [],
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}), \
             patch("jobs.extraction_pipeline.extract_job_fields") as mock_extract, \
             patch("jobs.extraction_pipeline.classify_role_category", return_value=("software_engineering", None)):
            summary = run_scrape(self.user, sites=["internshala"], max_results_per_site=3)

        mock_extract.assert_not_called()
        self.assertEqual(JobPosting.objects.count(), 1)
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["failed_extraction"], 0)

    def test_run_scrape_continues_when_one_site_scraper_raises(self):
        """A single site failing outright (timeout, selector break, etc.)
        must not stop other sites from being scraped."""
        def broken_scraper(keyword, max_results):
            raise TimeoutError("Naukri selector timed out")

        def working_scraper(keyword, max_results):
            return [{
                "title": "Backend Developer",
                "company": "GoodCo",
                "raw_text": "Role: Backend Developer\nCompany: GoodCo\nSkills: Python",
                "json_ld": [],
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"naukri": broken_scraper, "internshala": working_scraper}), \
             patch("jobs.extraction_pipeline.classify_role_category", return_value=("software_engineering", None)):
            summary = run_scrape(self.user, sites=["naukri", "internshala"], max_results_per_site=3)

        self.assertEqual(summary["sites"]["naukri"], 0)
        self.assertEqual(summary["sites"]["internshala"], 1)
        self.assertEqual(summary["added"], 1)
        self.assertTrue(any("naukri" in e.lower() and "failed" in e.lower() for e in summary["errors"]))


class LivePerViewerRoleFilterTests(TestCase):
    """
    Covers the fix that moved role filtering from scrape-time
    (permanently discarding jobs) to display-time (per-viewer,
    reversible, no re-scrape needed).
    """

    def setUp(self):
        self.client = Client()

        self.user_a = User.objects.create_user(username="usera", password="pw")
        Profile.objects.create(
            user=self.user_a, skills=["Python"],
            target_role="Python Developer", target_role_category="software_engineering",
        )

        self.user_b = User.objects.create_user(username="userb", password="pw")
        Profile.objects.create(
            user=self.user_b, skills=["Sales"],
            target_role="Sales Executive", target_role_category="sales",
        )

        self.se_job = JobPosting.objects.create(
            raw_text="Role: Python Developer", source="internshala",
            title="Python Developer", company="Acme", role_category="software_engineering",
        )
        self.sales_job = JobPosting.objects.create(
            raw_text="Role: Sales Executive", source="internshala",
            title="Sales Executive", company="Beta Corp", role_category="sales",
        )

    def test_run_scrape_no_longer_discards_off_role_jobs(self):
        """Requirement: jobs must NOT be permanently excluded from the
        database just because they don't match the scraping user's role."""
        def fake_scraper(keyword, max_results):
            return [{
                "title": "Sales Executive", "company": "Gamma Inc",
                "raw_text": "Role: Sales Executive\nCompany: Gamma Inc\nSkills: Communication",
                "json_ld": [],
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}), \
             patch("jobs.extraction_pipeline.classify_role_category", return_value=("sales", None)):
            # user_a targets software_engineering, but the scraped job is "sales"
            summary = run_scrape(self.user_a, sites=["internshala"], max_results_per_site=3)

        self.assertEqual(summary["added"], 1)  # saved despite not matching user_a's role
        self.assertEqual(summary["skipped_wrong_role"], 0)  # no longer used to discard
        self.assertTrue(JobPosting.objects.filter(company="Gamma Inc").exists())

    def test_different_users_see_different_jobs_from_same_shared_pool(self):
        self.client.login(username="usera", password="pw")
        response_a = self.client.get("/matches/")
        titles_a = [j["job"].title for j in response_a.context["jobs_with_display"]]
        self.assertIn("Python Developer", titles_a)
        self.assertNotIn("Sales Executive", titles_a)

        self.client.login(username="userb", password="pw")
        response_b = self.client.get("/matches/")
        titles_b = [j["job"].title for j in response_b.context["jobs_with_display"]]
        self.assertIn("Sales Executive", titles_b)
        self.assertNotIn("Python Developer", titles_b)

    def test_changing_target_role_changes_displayed_jobs_without_rescrape(self):
        self.client.login(username="usera", password="pw")

        response_before = self.client.get("/matches/")
        titles_before = [j["job"].title for j in response_before.context["jobs_with_display"]]
        self.assertNotIn("Sales Executive", titles_before)

        # User A changes their target role - no new scrape happens, same DB rows
        profile_a = Profile.objects.get(user=self.user_a)
        profile_a.target_role_category = "sales"
        profile_a.save()

        response_after = self.client.get("/matches/")
        titles_after = [j["job"].title for j in response_after.context["jobs_with_display"]]
        self.assertIn("Sales Executive", titles_after)
        self.assertNotIn("Python Developer", titles_after)

    def test_scoring_and_role_category_storage_are_unaffected(self):
        """role_category must still be stored on save, and match scoring
        must still work exactly as before - only the display filter is new."""
        self.assertEqual(self.se_job.role_category, "software_engineering")
        self.assertEqual(self.sales_job.role_category, "sales")

        self.client.login(username="usera", password="pw")
        response = self.client.get("/matches/")
        entry = next(j for j in response.context["jobs_with_display"] if j["job"].id == self.se_job.id)
        self.assertIn("live_score", entry)
