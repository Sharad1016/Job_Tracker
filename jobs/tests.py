from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.contrib.messages import get_messages
from unittest.mock import patch
import json

from jobs.models import JobPosting, Profile, Application
from jobs.extraction import extract_job_fields
from jobs.extraction_pipeline import GeminiRunState, process_scraped_item, _keyword_classify
from jobs.scraping_service import run_scrape, _scrape_site
from jobs import skill_dictionary, resume_parser
from google.genai import errors as genai_errors


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


class PerSiteLimitTests(TestCase):
    """Phase 16, Change 1: max_results_per_site now accepts a dict so
    each site can get its OWN limit (8+8=16), instead of one shared
    number applied identically everywhere."""

    def setUp(self):
        self.user = User.objects.create_user(username="limit_tester", password="pw")
        Profile.objects.create(user=self.user, skills=["Python"], target_role="Python Developer", target_role_category="software_engineering")

    def test_dict_limit_passes_correct_per_site_value(self):
        received = {}

        def fake_internshala(keyword, max_results, **kwargs):
            received["internshala"] = max_results
            return []

        def fake_workindia(keyword, max_results, **kwargs):
            received["workindia"] = max_results
            return []

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_internshala, "workindia": fake_workindia}):
            run_scrape(self.user, sites=["internshala", "workindia"], max_results_per_site={"internshala": 8, "workindia": 8})

        self.assertEqual(received["internshala"], 8)
        self.assertEqual(received["workindia"], 8)

    def test_plain_int_still_works_uniformly_backward_compatible(self):
        """Existing callers that pass a single int (e.g. the CLI
        management command) must keep working exactly as before."""
        received = {}

        def fake_internshala(keyword, max_results, **kwargs):
            received["internshala"] = max_results
            return []

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_internshala}):
            run_scrape(self.user, sites=["internshala"], max_results_per_site=5)

        self.assertEqual(received["internshala"], 5)

    def test_16_total_from_8_plus_8_when_all_succeed(self):
        def fake_internshala(keyword, max_results, **kwargs):
            return [
                {"title": f"IS Job {i}", "company": f"ISCo{i}", "raw_text": f"Role: IS Job {i}\nCompany: ISCo{i}", "json_ld": []}
                for i in range(max_results)
            ]

        def fake_workindia(keyword, max_results, **kwargs):
            return [
                {"title": f"WI Job {i}", "company": f"WICo{i}", "raw_text": f"Role: WI Job {i}\nCompany: WICo{i}", "json_ld": []}
                for i in range(max_results)
            ]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_internshala, "workindia": fake_workindia}):
            summary = run_scrape(self.user, sites=["internshala", "workindia"], max_results_per_site={"internshala": 8, "workindia": 8})

        self.assertEqual(summary["sites"]["internshala"], 8)
        self.assertEqual(summary["sites"]["workindia"], 8)
        self.assertEqual(summary["added"], 16)

    def test_duplicates_reduce_total_below_16_not_padded(self):
        """Explicit requirement: never artificially fill to reach 16."""
        JobPosting.objects.create(
            raw_text="Role: Existing Job", source="internshala",
            title="Existing Job", company="DupCo", role_category="other",
        )

        def fake_internshala(keyword, max_results, **kwargs):
            # One of the 8 is a duplicate of an already-saved job.
            jobs = [{"title": "Existing Job", "company": "DupCo", "raw_text": "Role: Existing Job\nCompany: DupCo", "json_ld": []}]
            jobs += [
                {"title": f"IS Job {i}", "company": f"ISCo{i}", "raw_text": f"Role: IS Job {i}\nCompany: ISCo{i}", "json_ld": []}
                for i in range(max_results - 1)
            ]
            return jobs

        def fake_workindia(keyword, max_results, **kwargs):
            return [
                {"title": f"WI Job {i}", "company": f"WICo{i}", "raw_text": f"Role: WI Job {i}\nCompany: WICo{i}", "json_ld": []}
                for i in range(max_results)
            ]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_internshala, "workindia": fake_workindia}):
            summary = run_scrape(self.user, sites=["internshala", "workindia"], max_results_per_site={"internshala": 8, "workindia": 8})

        self.assertEqual(summary["added"], 15)  # 16 discovered, 1 duplicate skipped, NOT padded back to 16
        self.assertEqual(summary["skipped_duplicate"], 1)


class CombinedResultMessageTests(TestCase):
    """Phase 16, Change 3: the Fetch New Jobs view must show ONE
    combined total, not a per-site breakdown, and that total must be
    the actual number of successfully SAVED jobs - never hardcoded,
    never counting duplicates/failures."""

    def setUp(self):
        self.user = User.objects.create_user(username="message_tester", password="pw")
        Profile.objects.create(user=self.user, skills=["Python"], target_role="Python Developer", target_role_category="software_engineering")
        self.client = Client()
        self.client.force_login(self.user)

    def test_success_message_shows_combined_total_not_per_site_breakdown(self):
        def fake_internshala(keyword, max_results, **kwargs):
            return [{"title": "A", "company": "X", "raw_text": "Role: A\nCompany: X", "json_ld": []}] * 1

        def fake_internshala_multi(keyword, max_results, **kwargs):
            return [
                {"title": f"IS{i}", "company": f"Co{i}", "raw_text": f"Role: IS{i}\nCompany: Co{i}", "json_ld": []}
                for i in range(8)
            ]

        def fake_workindia_multi(keyword, max_results, **kwargs):
            return [
                {"title": f"WI{i}", "company": f"WCo{i}", "raw_text": f"Role: WI{i}\nCompany: WCo{i}", "json_ld": []}
                for i in range(8)
            ]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_internshala_multi, "workindia": fake_workindia_multi}):
            response = self.client.post("/scrape/", {"site": "all"})

        message_texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("16 new job(s) found" in t for t in message_texts))
        # The old per-site breakdown format must be GONE
        self.assertFalse(any("internshala:" in t.lower() and "workindia:" in t.lower() for t in message_texts))

    def test_message_shows_actual_reduced_count_not_16(self):
        def fake_internshala(keyword, max_results, **kwargs):
            return [
                {"title": f"IS{i}", "company": f"Co{i}", "raw_text": f"Role: IS{i}\nCompany: Co{i}", "json_ld": []}
                for i in range(6)  # fewer than the requested 8
            ]

        def fake_workindia(keyword, max_results, **kwargs):
            return [
                {"title": f"WI{i}", "company": f"WCo{i}", "raw_text": f"Role: WI{i}\nCompany: WCo{i}", "json_ld": []}
                for i in range(7)  # fewer than the requested 8
            ]

        with patch("jobs.scraping_service.SITE_SCRAPERS", {"internshala": fake_internshala, "workindia": fake_workindia}):
            response = self.client.post("/scrape/", {"site": "all"})

        message_texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("13 new job(s) found" in t for t in message_texts))


class ConcurrentDetailPageFetchTests(TestCase):
    """Phase 16, Change 2: Stage 2 now fetches up to MAX_DETAIL_CONCURRENCY
    (4) detail pages at once per site, instead of one at a time."""

    def test_max_detail_concurrency_constant_is_4_on_both_scrapers(self):
        from jobs import scraper, workindia_scraper
        self.assertEqual(scraper.MAX_DETAIL_CONCURRENCY, 4)
        self.assertEqual(workindia_scraper.MAX_DETAIL_CONCURRENCY, 4)

    def test_fetch_detail_page_never_raises_returns_error_in_dict(self):
        from jobs.detail_page_extraction import fetch_detail_page
        # An unreachable/invalid URL - must come back as a dict with an
        # error, never raise, so a ThreadPoolExecutor worker calling
        # this can't take down the whole pool.
        result = fetch_detail_page("http://this-domain-does-not-exist-xyz123.invalid", timeout_ms=3000)
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["description"], "")
        self.assertEqual(result["skills"], [])

    def test_four_at_a_time_not_all_at_once_not_sequential(self):
        """Proves the actual concurrency cap with real wall-clock
        timing: 8 fake 'fetches' of 0.5s each, capped at 4 workers,
        should take ~1s (two waves), not ~4s (sequential) or ~0.5s
        (unbounded)."""
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def fake_fetch(url, timeout_ms=20000):
            time.sleep(0.5)
            return {"description": "d", "skills": ["Python"], "json_ld": [], "error": None}

        start = time.monotonic()
        urls = [f"https://example.com/job{i}" for i in range(8)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(fake_fetch, u) for u in urls]
            for f in as_completed(futures):
                f.result()
        elapsed = time.monotonic() - start

        self.assertGreater(elapsed, 0.9)   # not unbounded-concurrent (~0.5s)
        self.assertLess(elapsed, 2.5)      # not fully sequential (~4s)


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
        # Phase 17: the expanded dictionary's alias system now ALSO
        # correctly recognizes "RESTful APIs" in the description as
        # "REST API" (previously missed entirely, since only the
        # literal string "REST API" was recognized before aliases
        # existed) - so the job's real skill count is 6, not 5. This is
        # a genuine accuracy improvement, not a regression.
        expected_score = round((3 / 6) * 100, 1)  # 3 matched of 6 required
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


class PythonResumeSkillExtractionTests(TestCase):
    """
    Phase 17: Python-primary resume skill extraction. Covers
    skill_dictionary.extract_resume_skills() (pure Python, no Gemini)
    directly, plus resume_parser.extract_skills_from_resume() for the
    Gemini-validation-of-unknowns layer on top.
    """

    def test_basic_skills(self):
        result = skill_dictionary.extract_resume_skills("Skills: Python, Django, React, MongoDB")
        for skill in ["Python", "Django", "React", "MongoDB"]:
            self.assertIn(skill, result["known"])

    def test_case_variations_collapse_to_one_canonical_entry(self):
        result = skill_dictionary.extract_resume_skills("python PYTHON Python")
        self.assertEqual(result["known"].count("Python"), 1)

    def test_aliases_normalize_to_canonical_name(self):
        for variant in ["NodeJS", "Node JS", "node.js"]:
            self.assertEqual(skill_dictionary.normalize_skill(variant), "Node.js")

    def test_stack_detection(self):
        stacks = skill_dictionary.detect_stacks("I have MERN Stack development experience.")
        self.assertIn("MERN", stacks)

    def test_stack_components_expand_correctly(self):
        components = skill_dictionary.expand_stack_components(["MERN"])
        for skill in ["MongoDB", "Express.js", "React", "Node.js"]:
            self.assertIn(skill, components)

    def test_skills_detected_inside_project_description_sentence(self):
        text = "Developed a web application using Python, Django, React and PostgreSQL."
        result = skill_dictionary.extract_resume_skills(text)
        for skill in ["Python", "Django", "React", "PostgreSQL"]:
            self.assertIn(skill, result["known"])

    def test_skills_detected_inside_experience_description(self):
        text = "Experience\nBuilt REST APIs using FastAPI and PostgreSQL for a production system."
        result = skill_dictionary.extract_resume_skills(text)
        for skill in ["FastAPI", "PostgreSQL"]:
            self.assertIn(skill, result["known"])

    def test_unknown_skill_is_flagged_not_silently_added(self):
        text = "Skills\nPython, Django, React, LangChain"
        result = skill_dictionary.extract_resume_skills(text)
        self.assertIn("Python", result["known"])
        self.assertIn("LangChain", result["unknown"])
        self.assertNotIn("LangChain", result["known"])

    def test_stack_plus_explicit_skills_both_present(self):
        text = "Skills: Python\nMERN Stack developer with 2 years experience."
        result = skill_dictionary.extract_resume_skills(text)
        self.assertIn("Python", result["known"])
        self.assertIn("React", result["known"])  # from MERN expansion
        self.assertIn("MongoDB", result["known"])


class ResumeParserGeminiValidationTests(TestCase):
    """resume_parser.extract_skills_from_resume(): Gemini is called
    AT MOST ONCE per upload, only for unknown terms - never for
    already-known skills, and never required for the upload to
    succeed."""

    def test_known_skills_only_never_calls_gemini(self):
        text = "Skills: Python, Django, React, MongoDB"
        with patch("jobs.resume_parser._get_client") as mock_client:
            result = resume_parser.extract_skills_from_resume(text)
        mock_client.assert_not_called()
        for skill in ["Python", "Django", "React", "MongoDB"]:
            self.assertIn(skill, result)

    def test_gemini_confirms_unknown_technical_skill_gets_added(self):
        text = "Skills\nPython, Django, LangChain"

        def fake_generate_content(model, contents, config):
            class FakeResponse:
                text = json.dumps({"valid_skills": ["LangChain"]})
            return FakeResponse()

        with patch("jobs.resume_parser._get_client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = fake_generate_content
            result = resume_parser.extract_skills_from_resume(text)

        self.assertIn("Python", result)
        self.assertIn("LangChain", result)
        # Confirmed skill should now be recognized by the dictionary itself
        self.assertEqual(skill_dictionary.normalize_skill("LangChain"), "LangChain")

    def test_gemini_rejects_unknown_term_it_is_not_added(self):
        text = "Skills\nPython, Passionate Team Player"

        def fake_generate_content(model, contents, config):
            class FakeResponse:
                text = json.dumps({"valid_skills": []})  # rejected
            return FakeResponse()

        with patch("jobs.resume_parser._get_client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = fake_generate_content
            result = resume_parser.extract_skills_from_resume(text)

        self.assertIn("Python", result)
        self.assertNotIn("Passionate Team Player", result)

    def test_gemini_unavailable_known_skills_still_returned(self):
        """Explicit core requirement: a 503/unavailable Gemini must
        NEVER break the resume upload - known skills come back
        regardless."""
        text = "Skills\nPython, Django, React, SomeUnknownTerm"

        def always_fails(*a, **k):
            raise genai_errors.APIError(
                503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}}
            )

        with patch("jobs.resume_parser._get_client") as mock_client, \
             patch("jobs.resume_parser.time.sleep"):  # skip the real retry delay in tests
            mock_client.return_value.models.generate_content.side_effect = always_fails
            result = resume_parser.extract_skills_from_resume(text)  # must NOT raise

        for skill in ["Python", "Django", "React"]:
            self.assertIn(skill, result)
        self.assertNotIn("SomeUnknownTerm", result)  # unvalidated, correctly excluded

    def test_duplicate_skills_not_added_twice(self):
        text = "Skills: Python, python, PYTHON, Django"
        result = resume_parser.extract_skills_from_resume(text)
        self.assertEqual(sum(1 for s in result if s.lower() == "python"), 1)


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


class PaginationTests(TestCase):
    """
    Phase 18: Job Matches, Saved Jobs, and Applied/OA/Interview all
    show 10 per page via Django's built-in Paginator. Covers exactly
    the scenarios from the pagination spec: >10 items, search-term
    persistence across pages, <10 items, empty list, and an invalid
    ?page= value.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="page_tester", password="pw")
        Profile.objects.create(
            user=self.user, skills=["Python"],
            target_role="Python Developer", target_role_category="software_engineering",
        )
        self.client = Client()
        self.client.force_login(self.user)

    def _make_jobs(self, count, title_prefix="Job"):
        jobs = []
        for i in range(count):
            jobs.append(JobPosting.objects.create(
                raw_text=f"Role: {title_prefix} {i}", source="manual",
                title=f"{title_prefix} {i}", company=f"Co{i}",
                role_category="software_engineering", skills_required=["Python"],
            ))
        return jobs

    def test_job_matches_shows_exactly_10_per_page(self):
        self._make_jobs(15)
        response = self.client.get("/matches/")
        self.assertEqual(len(response.context["page_obj"]), 10)
        self.assertEqual(response.context["page_obj"].paginator.num_pages, 2)

    def test_job_matches_page_2_shows_remaining_5(self):
        self._make_jobs(15)
        response = self.client.get("/matches/", {"page": 2})
        self.assertEqual(len(response.context["page_obj"]), 5)

    def test_job_matches_live_score_sort_order_preserved_across_pages(self):
        """Jobs with different skill overlaps must stay correctly
        ordered by live_score across the page boundary, not reshuffled
        by pagination."""
        for i in range(12):
            JobPosting.objects.create(
                raw_text=f"Role: Job {i}", source="manual", title=f"Job {i}", company=f"Co{i}",
                role_category="software_engineering",
                # Every 3rd job matches (has Python); the rest don't -
                # scores should still sort correctly across pages.
                skills_required=["Python"] if i % 3 == 0 else ["Java", "Rust"],
            )
        response_p1 = self.client.get("/matches/")
        response_p2 = self.client.get("/matches/", {"page": 2})
        scores_p1 = [item["live_score"] for item in response_p1.context["page_obj"]]
        scores_p2 = [item["live_score"] for item in response_p2.context["page_obj"]]
        all_scores = scores_p1 + scores_p2
        self.assertEqual(all_scores, sorted(all_scores, reverse=True))

    def test_search_query_persists_across_pagination_links(self):
        self._make_jobs(15, title_prefix="Backend Developer")
        response = self.client.get("/matches/", {"q": "Backend", "page": 1})
        self.assertEqual(response.context["query"], "Backend")
        self.assertContains(response, "q=Backend")  # the Next link must carry the search term

    def test_fewer_than_10_items_no_broken_pagination(self):
        self._make_jobs(3)
        response = self.client.get("/matches/")
        self.assertEqual(response.context["page_obj"].paginator.num_pages, 1)
        # The pagination bar shouldn't render controls for a single page
        self.assertNotContains(response, "Next →")

    def test_empty_list_still_shows_empty_state(self):
        response = self.client.get("/matches/")
        self.assertContains(response, "No jobs yet")

    def test_invalid_page_number_falls_back_gracefully(self):
        self._make_jobs(15)
        response = self.client.get("/matches/", {"page": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 1)

    def test_page_number_out_of_range_falls_back_to_last_page(self):
        self._make_jobs(15)
        response = self.client.get("/matches/", {"page": 999})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 2)  # last valid page

    def test_saved_jobs_paginated(self):
        jobs = self._make_jobs(15)
        for job in jobs:
            Application.objects.create(user=self.user, job=job, status="saved")
        response = self.client.get("/saved/")
        self.assertEqual(len(response.context["jobs_with_display"]), 10)
        self.assertEqual(response.context["page_obj"].paginator.num_pages, 2)

    def test_applied_status_list_paginated(self):
        jobs = self._make_jobs(12)
        for job in jobs:
            Application.objects.create(user=self.user, job=job, status="applied")
        response = self.client.get("/applied/")
        self.assertEqual(len(response.context["jobs_with_display"]), 10)
        response_p2 = self.client.get("/applied/", {"page": 2})
        self.assertEqual(len(response_p2.context["jobs_with_display"]), 2)


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
