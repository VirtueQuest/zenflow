"""
ZenFlow — Phase 2+3: Background Scheduler
──────────────────────────────────────────
Jobs run via APScheduler (embedded in the FastAPI process).
For production with multiple workers, only one worker should run the scheduler
— controlled by the SCHEDULER_ENABLED env var.

Jobs:
  · 00:05 daily  — decrement ads days_left, expire finished ads
  · 09:00 daily  — send 24h-ahead booking reminders
  · 02:00 daily  — run database backup + clean old backups
  · Every 5 min  — health ping (logs DB + Redis status)
"""

import os, asyncio, logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron     import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger("zenflow.scheduler")

SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
BACKUP_KEEP_DAYS  = int(os.getenv("BACKUP_KEEP_DAYS", "30"))

_scheduler: AsyncIOScheduler | None = None

# ─────────────────────────────────────────
#  JOB: Decrement ad days_left + expire
# ─────────────────────────────────────────
async def job_decrement_ads():
    """
    Runs daily at 00:05.
    Decrements days_left for every active ad by 1.
    DB trigger (trg_expire_ad) automatically sets status='expired' when days_left=0.
    """
    from database import get_db as _get_db

    logger.info("scheduler: decrement_ads start")
    try:
        async for db in _get_db():
            # Decrement
            result = await db.execute("""
                UPDATE advertisements
                SET days_left = days_left - 1
                WHERE status = 'active' AND days_left > 0
            """)
            # Force-expire anything at 0 (belt + suspenders in case trigger missed)
            await db.execute("""
                UPDATE advertisements
                SET status = 'expired'
                WHERE days_left <= 0 AND status = 'active'
            """)
            # Count active/expired
            stats = await db.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE status='active')  AS active,
                    COUNT(*) FILTER (WHERE status='expired') AS expired
                FROM advertisements
            """)
            logger.info(f"scheduler: ads — active={stats['active']} expired={stats['expired']}")
    except Exception as e:
        logger.error(f"scheduler: decrement_ads failed: {e}")


# ─────────────────────────────────────────
#  JOB: 24-hour booking reminders
# ─────────────────────────────────────────
async def job_send_reminders():
    """
    Runs daily at 09:00.
    Finds all confirmed bookings scheduled for tomorrow that haven't
    received a reminder yet, and sends WhatsApp/WeChat notifications.
    """
    from database import get_db as _get_db
    from notifications import notify_booking_reminder

    logger.info("scheduler: send_reminders start")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        async for db in _get_db():
            bookings = await db.fetch("""
                SELECT b.booking_ref, b.customer_name, b.contact_type, b.contact_value,
                       b.booking_date, b.booking_time, b.duration_hours, b.total_amount,
                       p.display_name AS professional_name,
                       p.display_name_zh AS professional_name_zh,
                       p.emoji AS professional_emoji,
                       u.lang_pref
                FROM bookings b
                JOIN professionals p ON p.id = b.professional_id
                LEFT JOIN users u ON u.id = b.customer_user_id
                WHERE b.booking_date = $1
                  AND b.status = 'confirmed'
                  AND b.notif_sent_at IS NULL
            """, tomorrow)

            sent = skipped = failed = 0
            for bk in bookings:
                lang = bk.get("lang_pref") or "en"
                result = await notify_booking_reminder(dict(bk), db=db, lang=lang)
                if result["success"]:
                    sent += 1
                else:
                    failed += 1

            logger.info(f"scheduler: reminders — sent={sent} failed={failed} for {tomorrow}")
    except Exception as e:
        logger.error(f"scheduler: send_reminders failed: {e}")


# ─────────────────────────────────────────
#  JOB: Database backup
# ─────────────────────────────────────────
async def job_backup():
    """
    Runs daily at 02:00.
    Creates a timestamped backup and cleans up old ones.
    """
    from database import run_backup, cleanup_old_backups

    logger.info("scheduler: backup start")
    try:
        path = await run_backup()
        deleted = await cleanup_old_backups(BACKUP_KEEP_DAYS)
        logger.info(f"scheduler: backup created={path} old_deleted={deleted}")
    except Exception as e:
        logger.error(f"scheduler: backup failed: {e}")


# ─────────────────────────────────────────
#  JOB: Health ping
# ─────────────────────────────────────────
async def job_health_ping():
    from database import health_check
    try:
        status = await health_check()
        level = "info" if all(status.values()) else "warning"
        getattr(logger, level)(f"scheduler: health_ping {status}")
    except Exception as e:
        logger.error(f"scheduler: health_ping error: {e}")


# ─────────────────────────────────────────
#  SCHEDULER LIFECYCLE
# ─────────────────────────────────────────
def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Singapore")

    scheduler.add_job(
        job_decrement_ads,
        CronTrigger(hour=0, minute=5),
        id="decrement_ads",
        name="Decrement ad days_left",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_send_reminders,
        CronTrigger(hour=9, minute=0),
        id="send_reminders",
        name="Send 24h booking reminders",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_backup,
        CronTrigger(hour=2, minute=0),
        id="db_backup",
        name="Database backup",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        job_health_ping,
        IntervalTrigger(minutes=5),
        id="health_ping",
        name="Health check ping",
        replace_existing=True,
    )
    return scheduler


async def start_scheduler():
    global _scheduler
    if not SCHEDULER_ENABLED:
        logger.info("scheduler: disabled (SCHEDULER_ENABLED=false)")
        return
    _scheduler = create_scheduler()
    _scheduler.start()
    logger.info(f"scheduler: started with {len(_scheduler.get_jobs())} jobs")
    for job in _scheduler.get_jobs():
        logger.info(f"  job: {job.id} — next run: {job.next_run_time}")


async def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler: stopped")


def get_scheduler_status() -> list[dict]:
    if not _scheduler:
        return []
    return [
        {
            "id":            job.id,
            "name":          job.name,
            "next_run":      str(job.next_run_time),
            "trigger":       str(job.trigger),
        }
        for job in _scheduler.get_jobs()
    ]
