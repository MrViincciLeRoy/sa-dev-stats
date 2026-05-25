import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("build_stats")

DATA_DIR = Path("data")
DEVELOPERS_FILE = DATA_DIR / "developers.json"
STATS_FILE = DATA_DIR / "stats.json"

PROVINCE_KEYWORDS = {
    "Gauteng": ["gauteng", "johannesburg", "joburg", "jhb", "pretoria", "tshwane", "ekurhuleni", "soweto", "midrand", "sandton", "centurion", "randburg"],
    "Western Cape": ["western cape", "cape town", "ct", "stellenbosch", "bellville", "paarl", "george", "knysna", "worcester"],
    "KwaZulu-Natal": ["kwazulu", "durban", "pietermaritzburg", "pmb", "umhlanga", "ballito", "newcastle", "richards bay"],
    "Eastern Cape": ["eastern cape", "port elizabeth", "gqeberha", "east london", "buffalo city", "mthatha"],
    "Limpopo": ["limpopo", "polokwane", "tzaneen", "phalaborwa"],
    "Mpumalanga": ["mpumalanga", "nelspruit", "mbombela", "witbank"],
    "North West": ["north west", "rustenburg", "mafikeng", "mahikeng"],
    "Free State": ["free state", "bloemfontein", "mangaung"],
    "Northern Cape": ["northern cape", "kimberley", "upington"],
}


def infer_province(location_str):
    if not location_str:
        return "Unknown"
    loc = location_str.lower()
    for province, keywords in PROVINCE_KEYWORDS.items():
        if any(kw in loc for kw in keywords):
            return province
    return "Other / Unresolved"


def account_age_bucket(created_at):
    if not created_at:
        return "Unknown"
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_years = (datetime.now(timezone.utc) - created).days / 365
        if age_years < 1:
            return "< 1 year"
        elif age_years < 3:
            return "1–3 years"
        elif age_years < 6:
            return "3–6 years"
        elif age_years < 10:
            return "6–10 years"
        else:
            return "10+ years"
    except Exception:
        return "Unknown"


def avg(values):
    return round(sum(values) / len(values), 1) if values else 0


def build_gender_detail(devs):
    """
    Build a per-gender stats block. Keys are the raw gender strings found in the data.
    Each entry contains: count, pct, top_languages, province_breakdown,
    account_age_breakdown, averages (followers, public_repos, following).
    """
    total = len(devs)
    if not total:
        return {}

    # Collect all unique gender values present in data
    all_genders = sorted(set(d.get("gender") or "unknown" for d in devs))

    result = {}
    for g in all_genders:
        group = [d for d in devs if (d.get("gender") or "unknown") == g]
        n = len(group)

        lang_counter = Counter()
        for d in group:
            for lang, cnt in (d.get("languages") or {}).items():
                lang_counter[lang] += cnt

        province_counter = Counter(infer_province(d.get("location")) for d in group)
        age_counter = Counter(account_age_bucket(d.get("created_at")) for d in group)

        age_order = ["< 1 year", "1–3 years", "3–6 years", "6–10 years", "10+ years", "Unknown"]

        result[g] = {
            "count": n,
            "pct": round((n / total) * 100, 1),
            "averages": {
                "followers": avg([d.get("followers", 0) for d in group]),
                "public_repos": avg([d.get("public_repos", 0) for d in group]),
                "following": avg([d.get("following", 0) for d in group]),
            },
            "top_languages": [
                {"language": lang, "count": cnt}
                for lang, cnt in lang_counter.most_common(10)
            ],
            "province_breakdown": [
                {"province": p, "count": c}
                for p, c in sorted(province_counter.items(), key=lambda x: -x[1])
            ],
            "account_age_breakdown": [
                {"range": bucket, "count": age_counter.get(bucket, 0)}
                for bucket in age_order
                if age_counter.get(bucket, 0) > 0
            ],
        }

    return result


def build_stats():
    if not DEVELOPERS_FILE.exists():
        logger.warning("developers.json not found — nothing to build from.")
        return {}

    with open(DEVELOPERS_FILE) as f:
        devs = json.load(f)

    if not devs:
        logger.info("No developer data yet.")
        return {}

    total = len(devs)

    gender_counts = Counter(d.get("gender", "unknown") for d in devs)

    def pct(n):
        return round((n / total) * 100, 1) if total else 0

    gender_stats = {
        "male":    {"count": gender_counts.get("male", 0),    "pct": pct(gender_counts.get("male", 0))},
        "female":  {"count": gender_counts.get("female", 0),  "pct": pct(gender_counts.get("female", 0))},
        "unknown": {"count": gender_counts.get("unknown", 0), "pct": pct(gender_counts.get("unknown", 0))},
    }

    all_langs = Counter()
    for d in devs:
        for lang, count in (d.get("languages") or {}).items():
            all_langs[lang] += count
    top_languages = [{"language": lang, "count": cnt} for lang, cnt in all_langs.most_common(15)]

    # Legacy field kept for backward compat — now superseded by gender_detail
    female_langs = Counter()
    for d in devs:
        if d.get("gender") == "female":
            for lang, count in (d.get("languages") or {}).items():
                female_langs[lang] += count
    top_female_languages = [{"language": lang, "count": cnt} for lang, cnt in female_langs.most_common(10)]

    province_counts = Counter(infer_province(d.get("location")) for d in devs)
    province_breakdown = [
        {"province": p, "count": c}
        for p, c in sorted(province_counts.items(), key=lambda x: -x[1])
    ]

    age_counts = Counter(account_age_bucket(d.get("created_at")) for d in devs)
    age_order = ["< 1 year", "1–3 years", "3–6 years", "6–10 years", "10+ years", "Unknown"]
    account_age_breakdown = [
        {"range": bucket, "count": age_counts.get(bucket, 0)}
        for bucket in age_order
        if age_counts.get(bucket, 0) > 0
    ]

    stats = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_developers": total,
        "gender": gender_stats,
        "gender_detail": build_gender_detail(devs),
        "top_languages": top_languages,
        "top_female_languages": top_female_languages,
        "province_breakdown": province_breakdown,
        "account_age_breakdown": account_age_breakdown,
        "averages": {
            "followers": round(sum(d.get("followers", 0) for d in devs) / total, 1),
            "public_repos": round(sum(d.get("public_repos", 0) for d in devs) / total, 1),
        },
    }

    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)

    logger.info(f"Stats rebuilt — {total} devs, saved to {STATS_FILE}")
    return stats


if __name__ == "__main__":
    build_stats()
