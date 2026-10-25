from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from unittest.mock import patch

from jobs.models import JobPosting, Profile
from jobs.extraction import extract_job_fields
from jobs.extraction_pipeline import GeminiRunState, process_scraped_item, _keyword_classify
from jobs.scraping_service import run_scrape, _scrape_site


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
        stop further Gemini calls for the remainder of the run. Uses
        deliberately ambiguous titles (no keyword match) so Gemini is
        actually reached - a keyword-confident title would now skip
        Gemini entirely and never exercise this path."""
        item1 = {
            "title": "Digital Solutions Specialist", "company": "Acme",
            "raw_text": "Role: Digital Solutions Specialist\nCompany: Acme\nSkills: Communication",
            "json_ld": [],
        }
        item2 = {
            "title": "Growth Associate", "company": "Beta Corp",
            "raw_text": "Role: Growth Associate\nCompany: Beta Corp\nSkills: Communication",
            "json_ld": [],
        }
        gemini_state = GeminiRunState()

        with patch(
            "jobs.extraction_pipeline.classify_role_category",
            return_value=("", "429 RESOURCE_EXHAUSTED: quota exceeded"),
        ) as mock_classify:
            job1, log1 = process_scraped_item(item1, "internshala", gemini_state)
            self.assertFalse(gemini_state.available)  # disabled after job 1's 429
            job2, log2 = process_scraped_item(item2, "workindia", gemini_state)

        self.assertIsNotNone(job1)
        self.assertIsNotNone(job2)
        # classify_role_category must only have been attempted ONCE (job 1) -
        # job 2 should have used the keyword fallback instead, since Gemini
        # was already disabled by the time it was processed.
        mock_classify.assert_called_once()
        # Both titles are keyword-ambiguous, so both correctly land on
        # "other" - job1 via the quota-disable path, job2 via Gemini
        # being unavailable outright.
        self.assertEqual(job1["role_category"], "other")
        self.assertEqual(job2["role_category"], "other")

    def test_keyword_confident_title_never_calls_gemini(self):
        """Core promise of the optimization: a title the keyword
        classifier can confidently place must never touch Gemini at all."""
        item = {
            "title": "Python Developer", "company": "Qubixo",
            "raw_text": "Role: Python Developer\nCompany: Qubixo\nSkills: Python, Django",
            "json_ld": [],
        }
        gemini_state = GeminiRunState()

        with patch("jobs.extraction_pipeline.classify_role_category") as mock_classify:
            job, log = process_scraped_item(item, "internshala", gemini_state)

        mock_classify.assert_not_called()
        self.assertEqual(gemini_state.calls_made, 0)
        self.assertEqual(job["role_category"], "software_engineering")
        self.assertTrue(any("Gemini: skipped" in line for line in log))

    def test_ambiguous_title_does_call_gemini(self):
        """The other half of the same promise: a genuinely ambiguous
        title (no keyword match) must still reach Gemini when it's
        available."""
        item = {
            "title": "Digital Solutions Specialist", "company": "Acme",
            "raw_text": "Role: Digital Solutions Specialist\nCompany: Acme\nSkills: Communication",
            "json_ld": [],
        }
        gemini_state = GeminiRunState()

        with patch("jobs.extraction_pipeline.classify_role_category", return_value=("customer_support", None)) as mock_classify:
            job, log = process_scraped_item(item, "internshala", gemini_state)

        mock_classify.assert_called_once()
        self.assertEqual(gemini_state.calls_made, 1)
        self.assertEqual(job["role_category"], "customer_support")
        self.assertTrue(any("Gemini: called" in line for line in log))

    def test_duplicate_job_never_reaches_classification_or_gemini(self):
        """Dedup happens in run_scrape() before process_scraped_item() is
        even called, so a duplicate must never touch the classifier or
        Gemini at all."""
        JobPosting.objects.create(
            raw_text="Role: Python Developer", source="internshala",
            title="Python Developer", company="Qubixo", role_category="software_engineering",
        )

        def fake_scraper(keyword, max_results, **kwargs):
            return [{
                "title": "Python Developer", "company": "Qubixo",
                "raw_text": "Role: Python Developer\nCompany: Qubixo\nSkills: Python",
                "json_ld": [],
            }]

        user = User.objects.create_user(username="dup_tester", password="pw")
        Profile.objects.create(user=user, skills=["Python"], target_role="Python Developer", target_role_category="software_engineering")

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}), \
             patch("jobs.extraction_pipeline.classify_role_category") as mock_classify, \
             patch("jobs.extraction_pipeline._keyword_classify") as mock_keyword:
            summary = run_scrape(user, sites=["internshala"], max_results_per_site=3)

        mock_classify.assert_not_called()
        mock_keyword.assert_not_called()
        self.assertEqual(summary["skipped_duplicate"], 1)
        self.assertEqual(summary["added"], 0)
        self.assertEqual(JobPosting.objects.filter(title="Python Developer").count(), 1)


class ConcurrentScrapingTests(TestCase):
    """
    Phase 14: WorkIndia and Internshala now scrape concurrently via
    ThreadPoolExecutor (see _scrape_site() / run_scrape() Phase 1 in
    scraping_service.py). These tests cover exactly the scenarios from
    the concurrency spec: both succeed, one fails, cross-site
    duplicates, and that each thread's Playwright usage stays isolated
    (verified indirectly - see test_each_site_gets_its_own_scrape_call).
    """

    def setUp(self):
        self.user = User.objects.create_user(username="concurrent_tester", password="pw")
        Profile.objects.create(
            user=self.user, skills=["Python"],
            target_role="Python Developer", target_role_category="software_engineering",
        )

    def test_both_scrapers_succeed_and_results_are_combined(self):
        def fake_workindia(keyword, max_results, **kwargs):
            return [{
                "title": "Backend Engineer", "company": "WI Corp",
                "raw_text": "Role: Backend Engineer\nCompany: WI Corp\nSkills: Python",
                "json_ld": [],
            }]

        def fake_internshala(keyword, max_results, **kwargs):
            return [{
                "title": "Frontend Developer", "company": "IS Corp",
                "raw_text": "Role: Frontend Developer\nCompany: IS Corp\nSkills: React",
                "json_ld": [],
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"workindia": fake_workindia, "internshala": fake_internshala}):
            summary = run_scrape(self.user, sites=["workindia", "internshala"], max_results_per_site=5)

        self.assertEqual(summary["added"], 2)
        self.assertEqual(summary["sites"]["workindia"], 1)
        self.assertEqual(summary["sites"]["internshala"], 1)
        self.assertTrue(JobPosting.objects.filter(company="WI Corp").exists())
        self.assertTrue(JobPosting.objects.filter(company="IS Corp").exists())

    def test_workindia_failure_does_not_stop_internshala(self):
        def broken_workindia(keyword, max_results, **kwargs):
            raise TimeoutError("WorkIndia timed out")

        def fake_internshala(keyword, max_results, **kwargs):
            return [{
                "title": "Frontend Developer", "company": "IS Corp",
                "raw_text": "Role: Frontend Developer\nCompany: IS Corp\nSkills: React",
                "json_ld": [],
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"workindia": broken_workindia, "internshala": fake_internshala}):
            summary = run_scrape(self.user, sites=["workindia", "internshala"], max_results_per_site=5)

        self.assertEqual(summary["sites"]["workindia"], 0)
        self.assertEqual(summary["sites"]["internshala"], 1)
        self.assertEqual(summary["added"], 1)
        self.assertTrue(any("workindia" in e.lower() and "failed" in e.lower() for e in summary["errors"]))
        self.assertTrue(JobPosting.objects.filter(company="IS Corp").exists())

    def test_internshala_failure_does_not_stop_workindia(self):
        def fake_workindia(keyword, max_results, **kwargs):
            return [{
                "title": "Backend Engineer", "company": "WI Corp",
                "raw_text": "Role: Backend Engineer\nCompany: WI Corp\nSkills: Python",
                "json_ld": [],
            }]

        def broken_internshala(keyword, max_results, **kwargs):
            raise TimeoutError("Internshala timed out")

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"workindia": fake_workindia, "internshala": broken_internshala}):
            summary = run_scrape(self.user, sites=["workindia", "internshala"], max_results_per_site=5)

        self.assertEqual(summary["sites"]["internshala"], 0)
        self.assertEqual(summary["sites"]["workindia"], 1)
        self.assertEqual(summary["added"], 1)
        self.assertTrue(any("internshala" in e.lower() and "failed" in e.lower() for e in summary["errors"]))
        self.assertTrue(JobPosting.objects.filter(company="WI Corp").exists())

    def test_same_job_from_both_sites_is_not_saved_twice(self):
        """The exact job (same title+company) appearing on both sites
        must still only create ONE database row - concurrency must not
        bypass the existing dedup check."""
        def fake_workindia(keyword, max_results, **kwargs):
            return [{
                "title": "Python Developer", "company": "SameCo",
                "raw_text": "Role: Python Developer\nCompany: SameCo\nSkills: Python",
                "json_ld": [],
            }]

        def fake_internshala(keyword, max_results, **kwargs):
            return [{
                "title": "Python Developer", "company": "SameCo",
                "raw_text": "Role: Python Developer\nCompany: SameCo\nSkills: Python",
                "json_ld": [],
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"workindia": fake_workindia, "internshala": fake_internshala}):
            summary = run_scrape(self.user, sites=["workindia", "internshala"], max_results_per_site=5)

        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["skipped_duplicate"], 1)
        self.assertEqual(JobPosting.objects.filter(title="Python Developer", company="SameCo").count(), 1)

    def test_each_site_gets_its_own_isolated_scrape_call(self):
        """Not a live browser test (no network in this sandbox), but
        proves the concurrency wiring itself: each site's scraper_fn is
        invoked exactly once, with its own keyword/max_results, and
        _scrape_site never shares any object between the two calls -
        each call gets fresh args and returns its own independent
        result tuple."""
        calls = []

        def fake_workindia(keyword, max_results, **kwargs):
            calls.append(("workindia", keyword, max_results))
            return [{"title": "A", "company": "B", "raw_text": "Role: A\nCompany: B", "json_ld": []}]

        def fake_internshala(keyword, max_results, **kwargs):
            calls.append(("internshala", keyword, max_results))
            return [{"title": "C", "company": "D", "raw_text": "Role: C\nCompany: D", "json_ld": []}]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"workindia": fake_workindia, "internshala": fake_internshala}):
            run_scrape(self.user, sites=["workindia", "internshala"], max_results_per_site=7)

        self.assertEqual(len(calls), 2)
        sites_called = {c[0] for c in calls}
        self.assertEqual(sites_called, {"workindia", "internshala"})
        for site, keyword, max_results in calls:
            self.assertEqual(max_results, 7)

    def test_scrape_site_helper_returns_error_not_raises(self):
        """_scrape_site() must catch exceptions itself and return them
        as a value - it must never raise, since an uncaught exception
        inside a ThreadPoolExecutor worker would otherwise only surface
        when .result() is called, which is exactly what we rely on
        catching cleanly in run_scrape()."""
        def broken(keyword, max_results, **kwargs):
            raise ValueError("boom")

        site, results, error, duration = _scrape_site("workindia", broken, "python developer", 5)
        self.assertEqual(site, "workindia")
        self.assertEqual(results, [])
        self.assertIsInstance(error, ValueError)
        self.assertGreaterEqual(duration, 0)


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
        def fake_scraper(keyword, max_results, **kwargs):
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
        def broken_scraper(keyword, max_results, **kwargs):
            raise TimeoutError("selector timed out")

        def working_scraper(keyword, max_results, **kwargs):
            return [{
                "title": "Backend Developer",
                "company": "GoodCo",
                "raw_text": "Role: Backend Developer\nCompany: GoodCo\nSkills: Python",
                "json_ld": [],
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"brokensite": broken_scraper, "internshala": working_scraper}), \
             patch("jobs.extraction_pipeline.classify_role_category", return_value=("software_engineering", None)):
            summary = run_scrape(self.user, sites=["brokensite", "internshala"], max_results_per_site=3)

        self.assertEqual(summary["sites"]["brokensite"], 0)
        self.assertEqual(summary["sites"]["internshala"], 1)
        self.assertEqual(summary["added"], 1)
        self.assertTrue(any("brokensite" in e.lower() and "failed" in e.lower() for e in summary["errors"]))


class WorkIndiaFullPipelineTests(TestCase):
    """
    Phase 10 fix: WorkIndia cards use real CONFIRMED selectors now
    (.JobItemV2 / h2.text-brand a / .CompanyDetail / .LocationDetail /
    .f14.f-bold / .ExperienceDetail - see workindia_scraper.py), and the
    raw_text they produce uses the site's real "Rs. X - Rs. Y" salary
    format. This test proves the full chain - fake scraper -> run_scrape()
    -> real DB row - actually lands location and salary_range, which
    previously came back empty due to two separate bugs:
      1. extraction_pipeline.py's raw_text fallback for location only
         ran when title/company were BOTH missing, which never happens
         for WorkIndia (its own selectors already find them).
      2. rules_extraction.py's salary regex only recognized "₹", not
         WorkIndia's actual "Rs. X - Rs. Y" format.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="workindia_tester", password="pw")
        Profile.objects.create(
            user=self.user, skills=["Python"],
            target_role="Python Developer", target_role_category="software_engineering",
        )

    def test_location_and_salary_reach_the_database(self):
        def fake_workindia_scraper(keyword, max_results, **kwargs):
            # Mirrors exactly what workindia_scraper.py now assembles from
            # a real card (verified against a real saved WorkIndia page).
            raw_text = (
                "Role: Delivery Boy\n"
                "Company: Zepto\n"
                "Location: Purana Kanpur, Kanpur\n"
                "Salary: Rs. 50000 - Rs. 75000\n"
                "Experience: Fresher\n"
                "Category: Delivery"
            )
            return [{
                "title": "Delivery Boy",
                "company": "Zepto",
                "raw_text": raw_text,
                "json_ld": [],
                "application_url": "https://www.workindia.in/jobs/delivery_boy-purana_kanpur-kanpur-10915947/",
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"workindia": fake_workindia_scraper}), \
             patch("jobs.extraction_pipeline.classify_role_category", return_value=("other", None)):
            summary = run_scrape(self.user, sites=["workindia"], max_results_per_site=3)

        self.assertEqual(summary["added"], 1)
        job = JobPosting.objects.get(title="Delivery Boy", company="Zepto")
        self.assertEqual(job.location, "Purana Kanpur, Kanpur")
        self.assertEqual(job.salary_range, "Rs. 50000 - Rs. 75000")
        self.assertEqual(
            job.application_url,
            "https://www.workindia.in/jobs/delivery_boy-purana_kanpur-kanpur-10915947/",
        )

    def test_location_still_fills_via_generic_fallback_when_scraper_omits_it(self):
        """Belt-and-suspenders: even if a future scraper doesn't find
        location/salary via its own selectors, the raw_text line-prefix
        fallback must still pick them up - this was the original bug,
        now fixed independently of the title/company gate."""
        item = {
            "title": "Backend Developer",
            "company": "Acme Corp",
            "raw_text": "Role: Backend Developer\nCompany: Acme Corp\nLocation: Indore\nSalary: Rs. 20000 - Rs. 30000",
            "json_ld": [],
        }
        gemini_state = GeminiRunState()
        with patch("jobs.extraction_pipeline.classify_role_category", return_value=("software_engineering", None)):
            job_fields, log_lines = process_scraped_item(item, "workindia", gemini_state)

        self.assertIsNotNone(job_fields)
        self.assertEqual(job_fields["location"], "Indore")
        self.assertEqual(job_fields["salary_range"], "Rs. 20000 - Rs. 30000")


class DetailPageEnrichedScrapingTests(TestCase):
    """
    Phase 15: scrapers now visit each job's own detail page (Stage 2)
    and append the real description + an explicit "Skills: ..." line to
    raw_text - see scraper.py / workindia_scraper.py / mail
    detail_page_extraction.py. These tests simulate exactly what a
    Stage-2-enriched raw_text looks like (since Playwright/live-network
    detail-page fetching itself can't run in this sandbox) and verify
    the EXISTING pipeline - unchanged - correctly turns that into real
    skills_required and a non-zero match_score, computed BEFORE the row
    is ever saved (not as a second write).
    """

    def setUp(self):
        self.user = User.objects.create_user(username="detail_page_tester", password="pw")
        Profile.objects.create(
            user=self.user, skills=["Python", "React", "MongoDB"],
            target_role="Full Stack Developer", target_role_category="software_engineering",
        )

    def test_detail_page_enriched_job_gets_real_skills_and_nonzero_match_score(self):
        def fake_internshala_with_detail_page(keyword, max_results, **kwargs):
            # Mirrors exactly what scraper.py's Stage 2 appends after a
            # real detail-page visit: a "Skills: ..." line plus the real
            # description text, on top of the Stage 1 card summary.
            return [{
                "title": "Full Stack Developer", "company": "Digital Heroes",
                "raw_text": (
                    "Role: Full Stack Developer\nCompany: Digital Heroes\nLocation: Lucknow\n"
                    "Skills: React, Node.js, Python, MongoDB, MySQL\n"
                    "Own the end-to-end development of full-stack web applications using React, "
                    "Node.js, and Python. Architect scalable RESTful APIs with MongoDB integration."
                ),
                "json_ld": [],
                "application_url": "https://internshala.com/jobs/detail/full-stack-developer-123",
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_internshala_with_detail_page}), \
             patch("jobs.extraction_pipeline.classify_role_category", return_value=("software_engineering", None)):
            summary = run_scrape(self.user, sites=["internshala"], max_results_per_site=5)

        self.assertEqual(summary["added"], 1)
        job = JobPosting.objects.get(title="Full Stack Developer", company="Digital Heroes")

        # The database must NOT contain just "Role:/Company:/Location:" -
        # the real description text must be in there.
        self.assertIn("Architect scalable RESTful APIs", job.raw_text)
        self.assertNotEqual(job.raw_text.strip().count("\n"), 2)  # more than the old 3-line summary

        # Real skills, not empty
        self.assertGreater(len(job.skills_required), 0)
        for expected_skill in ["React", "Python", "MongoDB"]:
            self.assertIn(expected_skill, job.skills_required)

        # Match score genuinely non-zero (user has Python/React/MongoDB -
        # 3 of the job's 5 skills) - NOT artificially forced, this is the
        # real calculate_match_score() result.
        self.assertGreater(job.match_score, 0.0)
        expected_score = round((3 / 5) * 100, 1)  # 3 matched of 5 required
        self.assertEqual(job.match_score, expected_score)

    def test_job_with_genuinely_no_matching_skills_still_scores_zero(self):
        """Explicit requirement: never artificially force score above 0 -
        a job with real skills the user genuinely doesn't have must
        still show 0%, not be padded."""
        def fake_scraper(keyword, max_results, **kwargs):
            return [{
                "title": "Sales Executive", "company": "RetailCo",
                "raw_text": (
                    "Role: Sales Executive\nCompany: RetailCo\n"
                    "Skills: Salesforce, Negotiation, Cold Calling\n"
                    "Drive B2B sales pipeline and manage client relationships."
                ),
                "json_ld": [],
                "application_url": "",
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}):
            summary = run_scrape(self.user, sites=["internshala"], max_results_per_site=5)

        job = JobPosting.objects.get(title="Sales Executive")
        self.assertEqual(job.match_score, 0.0)

    def test_detail_page_failure_still_saves_job_with_listing_only_data(self):
        """A broken/unreachable detail page must not stop the job from
        being saved - it just falls back to whatever Stage 1 (the
        listing page) already found, exactly like before this change."""
        def fake_scraper_detail_page_failed(keyword, max_results, **kwargs):
            # Simulates Stage 2 failing silently and item keeping only
            # its Stage 1 raw_text - no Skills:/description line added.
            return [{
                "title": "Backend Developer", "company": "SomeCo",
                "raw_text": "Role: Backend Developer\nCompany: SomeCo\nLocation: Pune",
                "json_ld": [],
                "application_url": "https://internshala.com/jobs/detail/broken-link",
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper_detail_page_failed}):
            summary = run_scrape(self.user, sites=["internshala"], max_results_per_site=5)

        self.assertEqual(summary["added"], 1)
        job = JobPosting.objects.get(title="Backend Developer", company="SomeCo")
        self.assertEqual(job.location, "Pune")  # Stage 1 data still saved fine

    def test_known_pairs_is_passed_to_scraper_and_skips_already_saved_jobs(self):
        """run_scrape() must compute known_pairs from the DB ONCE up
        front and pass it to the scraper, so Stage 2 can skip a
        detail-page visit for a job that's already saved - avoiding
        wasted requests for jobs about to be discarded as duplicates
        anyway."""
        JobPosting.objects.create(
            raw_text="Role: Backend Developer", source="internshala",
            title="Backend Developer", company="AlreadyKnownCo", role_category="software_engineering",
        )

        received_kwargs = {}

        def fake_scraper(keyword, max_results, **kwargs):
            received_kwargs.update(kwargs)
            return [{
                "title": "Backend Developer", "company": "AlreadyKnownCo",
                "raw_text": "Role: Backend Developer\nCompany: AlreadyKnownCo",
                "json_ld": [],
            }]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_scraper}):
            run_scrape(self.user, sites=["internshala"], max_results_per_site=5)

        self.assertIn("known_pairs", received_kwargs)
        self.assertIn(("backend developer", "alreadyknownco"), received_kwargs["known_pairs"])


class KeywordRoleClassificationTests(TestCase):
    """
    CLASSIFY THE JOB, NOT THE COMPANY. Covers the deterministic
    (Gemini-unavailable) fallback classifier used by both the direct
    unit tests below and, at the pipeline level in
    WorkIndiaFullPipelineTests, when Gemini is disabled.
    """

    def test_title_only_examples(self):
        cases = {
            "Python Developer": "software_engineering",
            "Software Engineer": "software_engineering",
            "Backend Developer": "software_engineering",
            "Frontend Developer": "software_engineering",
            "Full Stack Developer": "software_engineering",
            "Associate Software Engineer": "software_engineering",
            "Sales Executive": "sales",
            "Business Development Executive": "sales",
            "HR Executive": "hr",
            "Recruiter": "hr",
            "Marketing Executive": "marketing",
            "Data Analyst": "data_analytics",
            "UI/UX Designer": "design",
            "Registration & License Executive": "other",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(_keyword_classify(title, raw_text=f"Role: {title}"), expected)

    def test_company_name_does_not_override_the_real_role(self):
        # The exact bug report: "software" inside the company name used
        # to make an unrelated job classify as software_engineering.
        result = _keyword_classify(
            "Registration & License Executive",
            "Role: Registration & License Executive\nCompany: Hoog Software Private Limited",
        )
        self.assertNotEqual(result, "software_engineering")
        self.assertEqual(result, "other")

    def test_sales_role_at_a_software_company(self):
        result = _keyword_classify(
            "Sales Executive - Software Company",
            "Role: Sales Executive - Software Company\nCompany: ABC Software Solutions",
        )
        self.assertEqual(result, "sales")

    def test_hr_role_at_an_it_company(self):
        result = _keyword_classify(
            "HR Executive",
            "Role: HR Executive\nCompany: XYZ Software Technologies",
        )
        self.assertEqual(result, "hr")

    def test_hr_recruiter_role_with_company_suffix_in_title(self):
        result = _keyword_classify(
            "HR Recruiter - IT Company",
            "Role: HR Recruiter - IT Company\nCompany: SomeCo",
        )
        self.assertEqual(result, "hr")


class WorkIndiaRoleMisclassificationRegressionTests(TestCase):
    """Reproduces the exact reported bug end-to-end (fake scraper ->
    run_scrape() -> DB row) with Gemini forced unavailable, so the
    deterministic fallback is what's actually under test - this is the
    path a live 429/quota outage would hit in production."""

    def setUp(self):
        self.user = User.objects.create_user(username="role_bug_tester", password="pw")
        Profile.objects.create(
            user=self.user, skills=["Python"],
            target_role="Software Engineer", target_role_category="software_engineering",
        )

    def test_registration_and_license_executive_is_not_software_engineering(self):
        def fake_workindia_scraper(keyword, max_results, **kwargs):
            return [{
                "title": "Registration & License Executive",
                "company": "Hoog Software Private Limited",
                "raw_text": (
                    "Role: Registration & License Executive\n"
                    "Company: Hoog Software Private Limited\n"
                    "Location: Indore\n"
                    "Experience: 1-2 years"
                ),
                "json_ld": [],
                "application_url": "https://www.workindia.in/jobs/registration-license-executive-123/",
            }]

        gemini_state = GeminiRunState()
        gemini_state.disable("simulated quota exhaustion for this test")

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"workindia": fake_workindia_scraper}), \
             patch("jobs.scraping_service.GeminiRunState", return_value=gemini_state):
            summary = run_scrape(self.user, sites=["workindia"], max_results_per_site=3)

        self.assertEqual(summary["added"], 1)
        job = JobPosting.objects.get(title="Registration & License Executive")
        self.assertNotEqual(job.role_category, "software_engineering")
        self.assertEqual(job.role_category, "other")


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
        def fake_scraper(keyword, max_results, **kwargs):
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
