from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from jobs.models import JobPosting
from jobs.matching import score_job


class Command(BaseCommand):
    help = (
        "Recalculates match_score for every JobPosting using the given "
        "user's Profile. Run this after creating/updating a Profile - "
        "old jobs don't rescore themselves automatically."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            type=str,
            default=None,
            help="Rescore against this user's profile. Defaults to the first account.",
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

        jobs = JobPosting.objects.all()
        updated = 0

        for job in jobs:
            old_score = job.match_score
            job.match_score = score_job(job, user)
            job.save()
            if job.match_score != old_score:
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Rescored {jobs.count()} job(s) against {user.username}'s profile, "
                f"{updated} score(s) changed."
            )
        )
