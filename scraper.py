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

DATA_DIR = Path("data")
DEVELOPERS_FILE = DATA_DIR / "developers.json"
PROGRESS_FILE   = DATA_DIR / "progress.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("scraper")

_gender_detector = gender.Detector(case_sensitive=False)

# ── name titles that strongly imply gender ───────────────────────────────────
_MALE_TITLES   = re.compile(r'\b(mr|sir|he|him|his|bro|dude|guy)\b', re.I)
_FEMALE_TITLES = re.compile(r'\b(ms|mrs|miss|she|her|hers|sis|girl|lady|queen)\b', re.I)

# ── pronoun patterns ─────────────────────────────────────────────────────────
_PRONOUN_SHE  = re.compile(r'\bshe\s*/\s*her\b|\bshe/her/hers\b|\bshe\b', re.I)
_PRONOUN_HE   = re.compile(r'\bhe\s*/\s*him\b|\bhe/him/his\b|\bhe\b', re.I)
_PRONOUN_THEY = re.compile(r'\bthey\s*/\s*them\b|\bthey/them/theirs\b', re.I)
_PRONOUN_ANY  = re.compile(r'\b(any/all|all/any|any pronouns)\b', re.I)

# ── website gender clues ─────────────────────────────────────────────────────
_SITE_MALE_WORDS   = ['man', 'men', 'male', 'guy', 'dude', 'father', 'husband', 'son', 'brother', 'him', 'his']
_SITE_FEMALE_WORDS = ['woman', 'women', 'female', 'girl', 'lady', 'mother', 'wife', 'daughter', 'sister', 'her', 'she']
# Soft color signals — only used when no textual clues are found
_SITE_FEMALE_COLORS = ['pink', '#ff69b4', '#ff1493', '#e75480', '#ffb6c1', 'hotpink', 'deeppink', 'rose']
_SITE_MALE_COLORS   = ['navy', '#003153', '#00008b', 'darkblue', 'steelblue', '#36454f', 'charcoal']


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
    logger.warning(f"Rate limit hit — waiting {wait:.0f}s for reset.")
    time.sleep(wait)


def fetch_website_text(url):
    """Fetch a developer's personal website and return lowercased text + CSS."""
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
    """Return 'male', 'female', or None based on website content."""
    if not site_text:
        return None

    male_score   = sum(site_text.count(w) for w in _SITE_MALE_WORDS)
    female_score = sum(site_text.count(w) for w in _SITE_FEMALE_WORDS)

    # Textual signal is strong enough on its own
    if female_score > male_score + 2:
        return "female"
    if male_score > female_score + 2:
        return "male"

    # Fall back to color scheme when text is ambiguous
    female_color_hits = sum(1 for c in _SITE_FEMALE_COLORS if c in site_text)
    male_color_hits   = sum(1 for c in _SITE_MALE_COLORS   if c in site_text)
    if female_color_hits > male_color_hits:
        return "female"
    if male_color_hits > female_color_hits:
        return "male"

    return None


def infer_gender(display_name, bio, blog_url=None):
    """
    Multi-pass gender inference:
      1. Pronoun block in bio  (highest confidence)
      2. Name-title prefix in display name
      3. gender-guesser on first name
      4. Keyword scan of bio
      5. Personal website scrape (slowest, lowest confidence)
    """
    bio_lower  = (bio or "").lower()
    name_lower = (display_name or "").lower()

    # 1. Explicit pronoun block
    if _PRONOUN_THEY.search(bio_lower):
        return "non-binary"
    if _PRONOUN_ANY.search(bio_lower):
        return "any"
    if _PRONOUN_SHE.search(bio_lower):
        return "female"
    if _PRONOUN_HE.search(bio_lower):
        return "male"

    # 2. Name-title clues (e.g. "Mr. Bongani" or username "MrSnow")
    if _FEMALE_TITLES.search(name_lower):
        return "female"
    if _MALE_TITLES.search(name_lower):
        return "male"

    # 3. gender-guesser on first word of display name
    first_name = (display_name or "").split()[0] if display_name else ""
    result = _gender_detector.get_gender(first_name)
    if result in ("male", "mostly_male"):
        return "male"
    if result in ("female", "mostly_female"):
        return "female"

    # 4. Bio keyword scan (broader)
    if _FEMALE_TITLES.search(bio_lower) or _PRONOUN_SHE.search(bio_lower):
        return "female"
    if _MALE_TITLES.search(bio_lower) or _PRONOUN_HE.search(bio_lower):
        return "male"

    # 5. Website scrape — only if all other signals failed
    if blog_url:
        site_text = fetch_website_text(blog_url)
        website_gender = infer_gender_from_website(site_text)
        if website_gender:
            return website_gender

    return "unknown"


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
        headers=HEADERS, params=params, timeout=15,
    )
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


# ── UPDATE MODE ──────────────────────────────────────────────────────────────
# Once all devs are scraped (progress["scan_complete"] == True), switch to
# refresh mode: re-fetch each known dev's profile to update followers,
# public_repos, bio, and re-infer gender.

UPDATE_BATCH = int(os.getenv("UPDATE_BATCH_SIZE", 50))   # devs refreshed per run in update mode
UPDATE_STALE_DAYS = int(os.getenv("UPDATE_STALE_DAYS", 7))  # refresh devs older than N days


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
    """Refresh stale developer records."""
    stale = [d for d in developers if needs_refresh(d)]
    if not stale:
        logger.info("All developers are up to date — nothing to refresh.")
        return developers

    logger.info(f"Update mode: {len(stale)} stale devs found, refreshing up to {UPDATE_BATCH}.")
    dev_map = {d["login"]: i for i, d in enumerate(developers)}
    refreshed = 0

    for dev in stale[:UPDATE_BATCH]:
        if time.time() > deadline:
            logger.info("Time limit reached during update — stopping.")
            break
        if not remaining_ok():
            logger.warning("Rate limit low — stopping update early.")
            break

        login = dev["login"]
        detail = fetch_user_detail(login)
        if not detail:
            continue

        languages = get_user_languages(login)
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

    logger.info(f"Update run complete — {refreshed} devs refreshed.")
    return developers


# ── MAIN SCRAPE ──────────────────────────────────────────────────────────────

def run_scrape(max_pages=None):
    if max_pages is None:
        max_pages = int(os.getenv("MAX_PAGES_PER_RUN", 25))

    deadline = time.time() + 3540  # 59-minute hard stop

    progress   = load_json(PROGRESS_FILE, {"last_page": 0, "seen_logins": [], "total_count": 0, "scan_complete": False})
    developers = load_json(DEVELOPERS_FILE, [])

    # ── Switch to update mode once full scan is done ──────────────────────────
    if progress.get("scan_complete"):
        logger.info("Full scan already complete — running in UPDATE mode.")
        developers = run_update(developers, deadline)
        save_json(DEVELOPERS_FILE, developers)
        save_json(PROGRESS_FILE, progress)
        return developers

    seen       = set(progress.get("seen_logins", []))
    start_page = progress.get("last_page", 0) + 1
    total_count = progress.get("total_count", 0)

    logger.info(f"Scrape mode — starting page {start_page}. {len(developers)} devs stored so far.")

    for page in range(start_page, start_page + max_pages):
        if time.time() > deadline:
            logger.info("59-minute limit reached — saving and stopping.")
            break
        if not remaining_ok():
            logger.warning("Rate limit too low — stopping early.")
            break

        logger.info(f"Fetching search page {page}…")
        items, hit_limit, tc = search_sa_developers(page=page)

        if tc and not progress.get("total_count"):
            progress["total_count"] = tc
            logger.info(f"GitHub reports {tc} total SA developers.")

        if hit_limit:
            logger.warning("Rate limit hit during search — saving and stopping.")
            break
        if not items:
            logger.info("No more results — scan complete!")
            progress["scan_complete"] = True
            progress["last_page"] = page
            save_json(PROGRESS_FILE, progress)
            save_json(DEVELOPERS_FILE, developers)
            return developers

        for item in items:
            if time.time() > deadline:
                save_json(PROGRESS_FILE, progress)
                save_json(DEVELOPERS_FILE, developers)
                return developers

            login = item["login"]
            if login in seen:
                continue
            if not remaining_ok():
                save_json(PROGRESS_FILE, progress)
                save_json(DEVELOPERS_FILE, developers)
                return developers

            detail = fetch_user_detail(login)
            if not detail:
                continue

            languages  = get_user_languages(login)
            gender_val = infer_gender(detail.get("name"), detail.get("bio"), detail.get("blog"))

            dev = {
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
            }

            developers.append(dev)
            seen.add(login)
            pct = f"{(len(developers) / progress['total_count'] * 100):.1f}%" if progress.get("total_count") else ""
            logger.info(f"  + {login} ({gender_val}) {pct}")
            time.sleep(0.5)

        progress["last_page"]     = page
        progress["seen_logins"]   = list(seen)
        save_json(PROGRESS_FILE, progress)
        save_json(DEVELOPERS_FILE, developers)
        logger.info(f"Page {page} done. Total devs: {len(developers)}")
        time.sleep(1)

    save_json(DEVELOPERS_FILE, developers)
    save_json(PROGRESS_FILE, progress)
    logger.info(f"Scrape run finished. {len(developers)} total developers saved.")
    return developers


if __name__ == "__main__":
    run_scrape()
