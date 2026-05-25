import os
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
import gender_guesser.detector as gender
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

DATA_DIR = Path("data")
DEVELOPERS_FILE = DATA_DIR / "developers.json"
PROGRESS_FILE = DATA_DIR / "progress.json"

logger = logging.getLogger("scraper")

_gender_detector = gender.Detector(case_sensitive=False)


def load_json(path, default):
    if path.exists() and path.stat().st_size > 2:
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_rate_limit():
    r = requests.get("https://api.github.com/rate_limit", headers=HEADERS)
    r.raise_for_status()
    core = r.json()["resources"]["core"]
    search = r.json()["resources"]["search"]
    return core, search


def remaining_ok(min_core=50, min_search=5):
    try:
        core, search = get_rate_limit()
        logger.debug(f"Rate limit — core: {core['remaining']}, search: {search['remaining']}")
        return core["remaining"] >= min_core and search["remaining"] >= min_search
    except Exception as e:
        logger.warning(f"Could not check rate limit: {e}")
        return False


def wait_for_reset(reset_epoch):
    wait = max(0, reset_epoch - time.time()) + 5
    logger.warning(f"Rate limit hit — waiting {wait:.0f}s for reset.")
    time.sleep(wait)


def get_user_languages(username):
    try:
        r = requests.get(
            f"https://api.github.com/users/{username}/repos",
            headers=HEADERS,
            params={"per_page": 30, "sort": "updated"},
        )
        if r.status_code == 403:
            _, search = get_rate_limit()
            wait_for_reset(search["reset"])
            return {}
        r.raise_for_status()
        langs = {}
        for repo in r.json():
            lang = repo.get("language")
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
        return langs
    except Exception as e:
        logger.warning(f"Language fetch failed for {username}: {e}")
        return {}


def infer_gender(display_name, bio):
    first_name = (display_name or "").split()[0] if display_name else ""
    result = _gender_detector.get_gender(first_name)
    if result in ("male", "mostly_male"):
        return "male"
    if result in ("female", "mostly_female"):
        return "female"
    # fallback: scan bio for pronouns
    bio_lower = (bio or "").lower()
    if "she/her" in bio_lower or " she " in bio_lower:
        return "female"
    if "he/him" in bio_lower or " he " in bio_lower:
        return "male"
    if "they/them" in bio_lower:
        return "unknown"
    return "unknown"


def search_sa_developers(page=1, per_page=30):
    params = {
        "q": "location:South Africa type:user",
        "per_page": per_page,
        "page": page,
        "sort": "joined",
        "order": "asc",
    }
    r = requests.get(
        "https://api.github.com/search/users",
        headers=HEADERS,
        params=params,
    )
    if r.status_code == 403 or r.status_code == 429:
        reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
        wait_for_reset(reset)
        return None, True  # signal: hit limit
    r.raise_for_status()
    data = r.json()
    return data.get("items", []), False


def fetch_user_detail(login):
    r = requests.get(f"https://api.github.com/users/{login}", headers=HEADERS)
    if r.status_code == 403 or r.status_code == 429:
        reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
        wait_for_reset(reset)
        return None
    if r.status_code != 200:
        return None
    return r.json()


def run_scrape(max_pages=10):
    progress = load_json(PROGRESS_FILE, {"last_page": 0, "seen_logins": []})
    developers = load_json(DEVELOPERS_FILE, [])

    seen = set(progress.get("seen_logins", []))
    start_page = progress.get("last_page", 0) + 1

    logger.info(f"Starting scrape from page {start_page}. {len(developers)} devs already stored.")

    for page in range(start_page, start_page + max_pages):
        if not remaining_ok():
            logger.warning("Rate limit too low — stopping early and saving progress.")
            break

        logger.info(f"Fetching search page {page}...")
        items, hit_limit = search_sa_developers(page=page)

        if hit_limit:
            logger.warning("Hit rate limit during search — saving and stopping.")
            break

        if not items:
            logger.info("No more results — scrape complete.")
            progress["last_page"] = page
            break

        for item in items:
            login = item["login"]
            if login in seen:
                continue

            if not remaining_ok():
                logger.warning("Core rate limit too low — stopping mid-page.")
                save_json(PROGRESS_FILE, progress)
                save_json(DEVELOPERS_FILE, developers)
                return developers

            detail = fetch_user_detail(login)
            if not detail:
                continue

            languages = get_user_languages(login)

            gender_val = infer_gender(detail.get("name"), detail.get("bio"))

            dev = {
                "login": login,
                "name": detail.get("name"),
                "location": detail.get("location"),
                "bio": detail.get("bio"),
                "public_repos": detail.get("public_repos", 0),
                "followers": detail.get("followers", 0),
                "following": detail.get("following", 0),
                "created_at": detail.get("created_at"),
                "languages": languages,
                "gender": gender_val,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }

            developers.append(dev)
            seen.add(login)
            logger.debug(f"  + {login} ({gender_val}) — {list(languages.keys())[:3]}")

            time.sleep(0.5)  # gentle throttle

        progress["last_page"] = page
        progress["seen_logins"] = list(seen)
        save_json(PROGRESS_FILE, progress)
        save_json(DEVELOPERS_FILE, developers)
        logger.info(f"Page {page} done. Total devs: {len(developers)}")

        time.sleep(1)

    save_json(DEVELOPERS_FILE, developers)
    save_json(PROGRESS_FILE, progress)
    logger.info(f"Scrape finished. {len(developers)} total developers saved.")
    return developers
