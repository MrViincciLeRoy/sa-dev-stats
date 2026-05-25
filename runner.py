import logging
import os
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("logs/scraper.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("runner")

INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", 60))
MAX_PAGES_PER_RUN = int(os.getenv("MAX_PAGES_PER_RUN", 5))


def job():
    from scraper import remaining_ok, run_scrape
    from build_stats import build_stats

    logger.info("=== Scheduled scrape job starting ===")

    if not remaining_ok(min_core=100, min_search=10):
        logger.warning("Rate limit too low — skipping this run.")
        return

    try:
        run_scrape(max_pages=MAX_PAGES_PER_RUN)
        build_stats()
        logger.info("=== Job complete — stats rebuilt ===")
    except Exception as e:
        logger.exception(f"Job failed: {e}")


if __name__ == "__main__":
    logger.info(f"Runner started — scraping every {INTERVAL_MINUTES} minutes.")
    job()  # run immediately on start

    scheduler = BlockingScheduler()
    scheduler.add_job(job, "interval", minutes=INTERVAL_MINUTES, id="sa_dev_scrape")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Runner stopped.")
