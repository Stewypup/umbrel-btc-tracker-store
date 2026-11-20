"""
scheduler.py — APScheduler configuration for the daily sync job.

The job runs at 02:00 UTC every day and:
  1. Scans all wallets via Fulcrum to pick up new transactions.
  2. Rebuilds daily balance history for all wallets.
  3. Fetches the latest BTC prices from CoinGecko.

It goes through sync.py, so it never overlaps a manual Sync Now.
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

_scheduler = None


def _daily_job():
    # Import here to avoid circular imports at module load time
    import sync

    log.info("Daily sync starting…")
    sync.run_blocking(source="scheduled")
    log.info("Daily sync finished.")


def start():
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _daily_job,
        trigger=CronTrigger(hour=2, minute=0),
        id="daily_sync",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("Scheduler started. Daily sync at 02:00 UTC.")


def shutdown():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


def run_now():
    """Run the sync job immediately in the calling thread."""
    _daily_job()
