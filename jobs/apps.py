import os
import sys
from django.apps import AppConfig


class JobsConfig(AppConfig):
    name = 'jobs'

    def ready(self):
        # apps.ready() fires for EVERY manage.py command (migrate, shell,
        # makemigrations, scrape_jobs itself...) - we only want the
        # scheduler running alongside the live dev server, not every time
        # someone runs a one-off command.
        if "runserver" not in sys.argv:
            return

        # Django's autoreloader (on by default) spawns a watcher process
        # AND a worker process; both import this app. RUN_MAIN is only
        # 'true' inside the actual worker, so this stops the scheduler
        # from starting twice and running every job twice. If someone
        # runs with --noreload, RUN_MAIN is never set at all, so we
        # allow that case through too.
        if os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv:
            from jobs.scheduler import start_scheduler
            start_scheduler()
