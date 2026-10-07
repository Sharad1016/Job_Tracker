from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from jobs.models import JobPosting
from jobs.scraper import scrape_internshala
from jobs.extraction import extract_job_fields
from jobs.matching import score_job
from jobs.telegram_notify import send_telegram_alert

MATCH_ALERT_THRESHOLD = 70


class Command(BaseCommand):
    help = (
        "Scrapes Internshala for a keyword, adds new postings (skipping "
        "ones already in the database), extracts structured fields with "
        "AI, and scores them against a user's profile."
    )

    def add_arguments(self, parser):
        parser.add_argument("--keyword", type=str, default="python", help="Search keyword.")
        parser.add_argument("--max-results", type=int, default=10)
        parser.add_argument(
            "--username",
            type=str,
            default=None,
            help="Score new jobs against this user's profile. Defaults to the first account.",
        )

    def handle(self, *args, **options):
        username = options["username"]
        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError(f"No user named '{username}'")
        else:
            user = User.objects.first()
            if user is None:
                raise CommandError("No users exist yet - sign up first.")

        self.stdout.write(f"Scraping Internshala for '{options['keyword']}'...")
        scraped = scrape_internshala(
            keyword=options["keyword"], max_results=options["max_results"]
        )
        self.stdout.write(f"Found {len(scraped)} listing(s).")

        added = 0
        skipped_duplicate = 0
        failed_extraction = 0

        for item in scraped:
            # Dedup BEFORE calling the AI - checking title+company directly
            # off the listing card costs nothing, whereas a wasted AI call
            # costs real money and time. Case-insensitive since site
            # formatting can vary run to run.
            already_exists = JobPosting.objects.filter(
                title__iexact=item["title"], company__iexact=item["company"]
            ).exists()
            if already_exists:
                skipped_duplicate += 1
                continue

            job = JobPosting.objects.create(raw_text=item["raw_text"], source="internshala")

            try:
                extracted = extract_job_fields(item["raw_text"])
                job.title = extracted["title"]
                job.company = extracted["company"]
                job.location = extracted["location"]
                job.skills_required = extracted["skills_required"]
                job.requirements = extracted["requirements"]
                job.salary_range = extracted["salary_range"]
                job.deadline = extracted["deadline"]
                job.match_score = score_job(job, user)
                job.save()
                added += 1

                if job.match_score >= MATCH_ALERT_THRESHOLD:
                    sent, alert_msg = send_telegram_alert(job)
                    if sent:
                        self.stdout.write(f"  Telegram alert sent for '{job.title}' ({job.match_score}%)")
                    else:
                        self.stdout.write(self.style.WARNING(f"  Telegram alert skipped: {alert_msg}"))
            except (ValueError, RuntimeError) as e:
                # Same pattern as the paste form: raw_text is already saved,
                # so a failed extraction doesn't lose the listing - it just
                # sits with empty fields until retried.
                failed_extraction += 1
                self.stdout.write(self.style.WARNING(f"Extraction failed for '{item['title']}': {e}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Added {added}, skipped {skipped_duplicate} duplicate(s), "
                f"{failed_extraction} extraction failure(s)."
            )
        )
