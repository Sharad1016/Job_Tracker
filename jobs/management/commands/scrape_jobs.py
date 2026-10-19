from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from jobs.scraping_service import run_scrape, SITE_SCRAPERS


class Command(BaseCommand):
    help = (
        "Scrapes one or all of Internshala/WorkIndia, adds new "
        "postings (skipping duplicates and jobs outside the user's target "
        "role), extracts structured fields with AI, and scores them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--site",
            type=str,
            default="all",
            choices=list(SITE_SCRAPERS.keys()) + ["all"],
            help="Which site to scrape. Defaults to all four.",
        )
        parser.add_argument(
            "--max-results", type=int, default=5, help="Max results PER SITE."
        )
        parser.add_argument(
            "--username",
            type=str,
            default=None,
            help="Scrape using this user's target role/profile. Defaults to the first account.",
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

        sites = None if options["site"] == "all" else [options["site"]]

        self.stdout.write(f"Scraping {options['site']} for user '{user.username}'...")
        summary = run_scrape(user, sites=sites, max_results_per_site=options["max_results"])

        self.stdout.write(f"Searched as: '{summary['keyword']}' (category: {summary['target_category']})")
        for site, count in summary["sites"].items():
            self.stdout.write(f"  {site}: {count} added")

        for err in summary["errors"]:
            self.stdout.write(self.style.WARNING(f"  ! {err}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Added {summary['added']}, skipped {summary['skipped_duplicate']} "
                f"duplicate(s), {summary['skipped_wrong_role']} off-role, "
                f"{summary['failed_extraction']} extraction failure(s)."
            )
        )
