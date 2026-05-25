import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
import gender_guesser.detector as gender
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("GH_SCRAPER_TOKEN") or os.getenv("GITHUB_TOKEN")
if not TOKEN:
    raise RuntimeError("No GitHub token found. Set GH_SCRAPER_TOKEN or GITHUB_TOKEN.")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

DATA_DIR        = Path("data")
DEVELOPERS_FILE = DATA_DIR / "developers.json"
PROGRESS_FILE   = DATA_DIR / "progress.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("scraper")

_gender_detector = gender.Detector(case_sensitive=False)

# ── Gender signals ────────────────────────────────────────────────────────────
_MALE_TITLES   = re.compile(r'\b(mr|sir|he|him|his|bro|dude|guy)\b', re.I)
_FEMALE_TITLES = re.compile(r'\b(ms|mrs|miss|she|her|hers|sis|girl|lady|queen)\b', re.I)
_PRONOUN_SHE   = re.compile(r'\bshe\s*/\s*her\b|\bshe/her/hers\b|\bshe\b', re.I)
_PRONOUN_HE    = re.compile(r'\bhe\s*/\s*him\b|\bhe/him/his\b|\bhe\b', re.I)
_PRONOUN_THEY  = re.compile(r'\bthey\s*/\s*them\b|\bthey/them/theirs\b', re.I)
_PRONOUN_ANY   = re.compile(r'\b(any/all|all/any|any pronouns)\b', re.I)

_SITE_MALE_WORDS   = ['man', 'men', 'male', 'guy', 'dude', 'father', 'husband', 'son', 'brother', 'him', 'his']
_SITE_FEMALE_WORDS = ['woman', 'women', 'female', 'girl', 'lady', 'mother', 'wife', 'daughter', 'sister', 'her', 'she']
_SITE_FEMALE_COLORS = ['pink', '#ff69b4', '#ff1493', '#e75480', '#ffb6c1', 'hotpink', 'deeppink', 'rose']
_SITE_MALE_COLORS   = ['navy', '#003153', '#00008b', 'darkblue', 'steelblue', '#36454f', 'charcoal']

# ── Date-range windows ────────────────────────────────────────────────────────
# GitHub search caps at 1 000 results (page 1–33 × 30) per query.
# Splitting by created: range keeps each window well under that limit.
# Windows cover GitHub's existence (2008) through present; adjust as needed.
DATE_WINDOWS = [
    ("2008-01-01", "2012-12-31"),
    ("2013-01-01", "2015-12-31"),
    ("2016-01-01", "2017-12-31"),
    ("2018-01-01", "2019-06-30"),
    ("2019-07-01", "2020-06-30"),
    ("2020-07-01", "2021-03-31"),
    ("2021-04-01", "2021-12-31"),
    ("2022-01-01", "2022-06-30"),
    ("2022-07-01", "2022-12-31"),
    ("2023-01-01", "2023-06-30"),
    ("2023-07-01", "2023-12-31"),
    ("2024-01-01", "2024-06-30"),
    ("2024-07-01", "2024-12-31"),
    ("2025-01-01", "2025-06-30"),
    ("2025-07-01", "2026-12-31"),
]

UPDATE_BATCH      = int(os.getenv("UPDATE_BATCH_SIZE", 50))
UPDATE_STALE_DAYS = int(os.getenv("UPDATE_STALE_DAYS", 7))


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    r = requests.get("https://api.github.com/rate_limit", headers=HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()["resources"]
    return data["core"], data["search"]


def remaining_ok(min_core=50, min_search=5):
    try:
        core, search = get_rate_limit()
        logger.info(f"Rate limit — core: {core['remaining']}, search: {search['remaining']}")
        return core["remaining"] >= min_core and search["remaining"] >= min_search
    except Exception as e:
        logger.warning(f"Could not check rate limit: {e}")
        return False


def wait_for_reset(reset_epoch):
    wait = max(0, reset_epoch - time.time()) + 5
    logger.warning(f"Rate limit hit — waiting {wait:.0f}s.")
    time.sleep(wait)


# ── Gender inference ──────────────────────────────────────────────────────────

def fetch_website_text(url):
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        if r.status_code == 200 and "text" in r.headers.get("Content-Type", ""):
            return r.text[:20000].lower()
    except Exception:
        pass
    return ""


def infer_gender_from_website(site_text):
    if not site_text:
        return None
    male_score   = sum(site_text.count(w) for w in _SITE_MALE_WORDS)
    female_score = sum(site_text.count(w) for w in _SITE_FEMALE_WORDS)
    if female_score > male_score + 2:
        return "female"
    if male_score > female_score + 2:
        return "male"
    female_color_hits = sum(1 for c in _SITE_FEMALE_COLORS if c in site_text)
    male_color_hits   = sum(1 for c in _SITE_MALE_COLORS   if c in site_text)
    if female_color_hits > male_color_hits:
        return "female"
    if male_color_hits > female_color_hits:
        return "male"
    return None


def infer_gender(display_name, bio, blog_url=None):
    bio_lower  = (bio or "").lower()
    name_lower = (display_name or "").lower()
    # 1. Pronouns in bio
    if _PRONOUN_THEY.search(bio_lower): return "non-binary"
    if _PRONOUN_ANY.search(bio_lower):  return "any"
    if _PRONOUN_SHE.search(bio_lower):  return "female"
    if _PRONOUN_HE.search(bio_lower):   return "male"
    # 2. Title prefixes in display name
    if _FEMALE_TITLES.search(name_lower): return "female"
    if _MALE_TITLES.search(name_lower):   return "male"
    # 3. gender-guesser on first name token
    first_name = (display_name or "").split()[0] if display_name else ""
    result = _gender_detector.get_gender(first_name)
    if result in ("male", "mostly_male"):   return "male"
    if result in ("female", "mostly_female"): return "female"
    # 4. Bio keyword scan
    if _FEMALE_TITLES.search(bio_lower) or _PRONOUN_SHE.search(bio_lower): return "female"
    if _MALE_TITLES.search(bio_lower)   or _PRONOUN_HE.search(bio_lower):  return "male"
    # 5. Website scrape (slowest — only when everything else fails)
    if blog_url:
        site_gender = infer_gender_from_website(fetch_website_text(blog_url))
        if site_gender:
            return site_gender
    return "unknown"


# ── GitHub API calls ──────────────────────────────────────────────────────────

def search_sa_developers(date_from, date_to, page=1, per_page=30):
    """
    Search SA developers filtered to a created: date window.
    Returns (items, hit_limit, total_count).
    """
    q = f"location:\"South Africa\" type:user created:{date_from}..{date_to}"
    params = {"q": q, "per_page": per_page, "page": page, "sort": "joined", "order": "asc"}
    r = requests.get(
        "https://api.github.com/search/users",
        headers=HEADERS, params=params, timeout=15,
    )
    if r.status_code == 422:
        # Past page 33 for this window — shouldn't happen with narrow windows, but handle it
        logger.warning(f"422 on window {date_from}..{date_to} page {page} — window exhausted.")
        return [], False, 0
    if r.status_code in (403, 429):
        wait_for_reset(int(r.headers.get("X-RateLimit-Reset", time.time() + 60)))
        return None, True, 0
    r.raise_for_status()
    data = r.json()
    return data.get("items", []), False, data.get("total_count", 0)


def fetch_user_detail(login):
    r = requests.get(f"https://api.github.com/users/{login}", headers=HEADERS, timeout=10)
    if r.status_code in (403, 429):
        wait_for_reset(int(r.headers.get("X-RateLimit-Reset", time.time() + 60)))
        return None
    if r.status_code != 200:
        return None
    return r.json()


def get_user_languages(username):
    try:
        r = requests.get(
            f"https://api.github.com/users/{username}/repos",
            headers=HEADERS,
            params={"per_page": 30, "sort": "updated"},
            timeout=10,
        )
        if r.status_code == 403:
            wait_for_reset(int(r.headers.get("X-RateLimit-Reset", time.time() + 60)))
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


# ── Update mode ───────────────────────────────────────────────────────────────

def needs_refresh(dev):
    scraped = dev.get("scraped_at")
    if not scraped:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(scraped)).days
        return age >= UPDATE_STALE_DAYS
    except Exception:
        return True


def run_update(developers, deadline):
    stale = [d for d in developers if needs_refresh(d)]
    if not stale:
        logger.info("All developers up to date — nothing to refresh.")
        return developers
    logger.info(f"Update mode: {len(stale)} stale, refreshing up to {UPDATE_BATCH}.")
    dev_map = {d["login"]: i for i, d in enumerate(developers)}
    refreshed = 0
    for dev in stale[:UPDATE_BATCH]:
        if time.time() > deadline or not remaining_ok():
            break
        login  = dev["login"]
        detail = fetch_user_detail(login)
        if not detail:
            continue
        languages  = get_user_languages(login)
        gender_val = infer_gender(detail.get("name"), detail.get("bio"), detail.get("blog"))
        updated = {
            **dev,
            "name":         detail.get("name"),
            "location":     detail.get("location"),
            "bio":          detail.get("bio"),
            "public_repos": detail.get("public_repos", 0),
            "followers":    detail.get("followers", 0),
            "following":    detail.get("following", 0),
            "languages":    languages,
            "gender":       gender_val,
            "scraped_at":   datetime.now(timezone.utc).isoformat(),
        }
        idx = dev_map.get(login)
        if idx is not None:
            developers[idx] = updated
        refreshed += 1
        logger.info(f"  ↻ {login} refreshed ({gender_val})")
        time.sleep(0.5)
    logger.info(f"Update run complete — {refreshed} refreshed.")
    return developers


# ── Process one user entry ────────────────────────────────────────────────────

def process_user(item, seen, developers, progress, deadline):
    """
    Fetch detail + languages + gender for one search result item.
    Returns True if added, False if skipped/failed.
    Mutates developers and seen in place.
    """
    login = item["login"]
    if login in seen:
        return False
    if time.time() > deadline or not remaining_ok():
        return None  # signal caller to stop

    detail = fetch_user_detail(login)
    if not detail:
        seen.add(login)  # don't retry failed lookups
        return False

    languages  = get_user_languages(login)
    gender_val = infer_gender(detail.get("name"), detail.get("bio"), detail.get("blog"))

    developers.append({
        "login":        login,
        "name":         detail.get("name"),
        "location":     detail.get("location"),
        "bio":          detail.get("bio"),
        "public_repos": detail.get("public_repos", 0),
        "followers":    detail.get("followers", 0),
        "following":    detail.get("following", 0),
        "created_at":   detail.get("created_at"),
        "languages":    languages,
        "gender":       gender_val,
        "scraped_at":   datetime.now(timezone.utc).isoformat(),
    })
    seen.add(login)

    total = progress.get("total_count", 0)
    pct   = f"{len(developers) / total * 100:.1f}%" if total else ""
    logger.info(f"  + {login} ({gender_val}) {pct}")
    time.sleep(0.5)
    return True


# ── Main scrape ───────────────────────────────────────────────────────────────

def run_scrape(max_pages=None):
    if max_pages is None:
        max_pages = int(os.getenv("MAX_PAGES_PER_RUN", 25))

    deadline   = time.time() + 3540
    progress   = load_json(PROGRESS_FILE, {
        "window_idx": 0,
        "last_page":  0,
        "seen_logins": [],
        "total_count": 0,
        "scan_complete": False,
    })
    developers = load_json(DEVELOPERS_FILE, [])

    # ── Update mode once full scan done ──────────────────────────────────────
    if progress.get("scan_complete"):
        logger.info("Full scan complete — running UPDATE mode.")
        developers = run_update(developers, deadline)
        save_json(DEVELOPERS_FILE, developers)
        save_json(PROGRESS_FILE, progress)
        return developers

    seen        = set(progress.get("seen_logins", []))
    window_idx  = progress.get("window_idx", 0)
    start_page  = progress.get("last_page", 0) + 1
    pages_this_run = 0

    logger.info(
        f"Scrape mode — window {window_idx}/{len(DATE_WINDOWS)-1}, "
        f"page {start_page}, {len(developers)} devs stored."
    )

    while window_idx < len(DATE_WINDOWS):
        date_from, date_to = DATE_WINDOWS[window_idx]

        for page in range(start_page, 31):  # cap at page 30 (900 results per window)
            if time.time() > deadline:
                logger.info("59-min limit reached — saving.")
                _save_progress(progress, developers, seen, window_idx, page - 1)
                return developers
            if pages_this_run >= max_pages:
                logger.info(f"Reached max_pages ({max_pages}) for this run — saving.")
                _save_progress(progress, developers, seen, window_idx, page - 1)
                return developers
            if not remaining_ok():
                logger.warning("Rate limit low — stopping early.")
                _save_progress(progress, developers, seen, window_idx, page - 1)
                return developers

            logger.info(f"Window {date_from}..{date_to} — page {page}…")
            items, hit_limit, tc = search_sa_developers(date_from, date_to, page=page)
            pages_this_run += 1

            if tc and not progress.get("total_count"):
                progress["total_count"] = tc
                logger.info(f"GitHub: {tc} total SA devs reported for this window.")

            if hit_limit:
                logger.warning("Rate limit during search — saving.")
                _save_progress(progress, developers, seen, window_idx, page)
                return developers

            if not items:
                logger.info(f"Window {date_from}..{date_to} exhausted at page {page}.")
                break  # move to next window

            for item in items:
                result = process_user(item, seen, developers, progress, deadline)
                if result is None:
                    # Hard stop signal
                    _save_progress(progress, developers, seen, window_idx, page)
                    return developers

            _save_progress(progress, developers, seen, window_idx, page)
            logger.info(f"Page {page} done. Total devs: {len(developers)}")
            time.sleep(1)

        # Advance to next window
        window_idx += 1
        start_page  = 1
        progress["window_idx"] = window_idx
        progress["last_page"]  = 0
        save_json(PROGRESS_FILE, progress)
        logger.info(f"Moving to window {window_idx}.")

    # All windows exhausted
    progress["scan_complete"] = True
    save_json(PROGRESS_FILE, progress)
    save_json(DEVELOPERS_FILE, developers)
    logger.info(f"Full scan complete — {len(developers)} total developers.")
    return developers


def _save_progress(progress, developers, seen, window_idx, last_page):
    progress["window_idx"]  = window_idx
    progress["last_page"]   = last_page
    progress["seen_logins"] = list(seen)
    save_json(PROGRESS_FILE, progress)
    save_json(DEVELOPERS_FILE, developers)


if __name__ == "__main__":
    run_scrape()
