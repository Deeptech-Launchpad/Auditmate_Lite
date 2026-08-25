"""Background extraction worker.

Only needed when JOBS_INLINE=0 in .env. For the demo, inline mode means you
don't have to run this at all.

Production (alongside gunicorn):
    python worker.py

Several copies can run at once — jobs are claimed with SKIP LOCKED, so no two
workers pick up the same document.
"""
import logging
import signal
import time

from app import create_app
from app.services.jobs import claim_next, run_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s worker: %(message)s",
)
log = logging.getLogger(__name__)

POLL_SECONDS = 3
running = True


def _stop(signum, frame):
    global running
    log.info("Shutdown requested — finishing current job then exiting.")
    running = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def main():
    app = create_app()
    log.info("Worker started. Polling every %ss.", POLL_SECONDS)

    with app.app_context():
        while running:
            try:
                job = claim_next()
                if job is None:
                    time.sleep(POLL_SECONDS)
                    continue

                log.info("Running job %s (%s)", job.id, job.job_type)
                run_job(job)
                log.info("Job %s finished: %s", job.id, job.status)

            except Exception:                      # noqa: BLE001
                log.exception("Worker loop error — backing off")
                time.sleep(POLL_SECONDS * 2)

    log.info("Worker stopped.")


if __name__ == "__main__":
    main()
