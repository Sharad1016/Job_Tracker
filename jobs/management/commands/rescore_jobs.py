from django.core.management.base import BaseCommand
from jobs.models import JobPosting
from jobs.matching import score_job


class Command(BaseCommand):
    help = (
        "Recalculates match_score for every JobPosting using the current "
        "Profile. Run this after creating your Profile for the first time, "
        "or any time you update your skills - old jobs don't rescore "
        "themselves automatically."
    )

    def handle(self, *args, **options):
        jobs = JobPosting.objects.all()
        updated = 0

        for job in jobs:
            old_score = job.match_score
            job.match_score = score_job(job)
            job.save()
            if job.match_score != old_score:
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Rescored {jobs.count()} job(s), {updated} score(s) changed."
            )
        )
