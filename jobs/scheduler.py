"""
Phase 8: runs scrape_jobs automatically on a schedule, instead of you
having to run it by hand. Uses django_apscheduler's DjangoJobStore so
the schedule survives server restarts (a plain in-memory APScheduler
job store would forget everything the moment the server stopped).
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from django_apscheduler.jobstores import DjangoJobStore
from django.core.management import call_command

logger = logging.getLogger(__name__)


def run_daily_scrape():
    """The actual task the scheduler fires. Wrapped in try/except so one
    bad run (e.g. a network hiccup) doesn't kill the scheduler itself -
    it just tries again at the next scheduled time."""
    try:
        call_command("scrape_jobs", keyword="python", max_results=15)
    except Exception:
        logger.exception("Scheduled scrape_jobs run failed")


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_jobstore(DjangoJobStore(), "default")

    scheduler.add_job(
        run_daily_scrape,
        trigger=CronTrigger(hour=9, minute=0),  # runs once daily at 9:00 AM server time
        id="daily_internshala_scrape",
        max_instances=1,
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started - daily scrape set for 9:00 AM")
    return scheduler
