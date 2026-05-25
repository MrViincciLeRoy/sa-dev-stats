# SA Dev Stats

Scrapes public GitHub profiles of South African developers, builds stats, and visualises them in a local dashboard.

## Setup

```bash
git clone <repo>
cd sa-dev-stats
pip install -r requirements.txt

cp .env.example .env
# Edit .env — add your GitHub Personal Access Token
```

Generate a token at https://github.com/settings/tokens — **read:user** and **public_repo** scopes are enough.

## Usage

**One-shot scrape + stats build:**
```bash
python scraper.py      # or import and call run_scrape() directly
python build_stats.py
```

**Background scheduler (runs every 60 min by default):**
```bash
python runner.py
```

**View dashboard:**
```bash
python -m http.server 8080
# Open http://localhost:8080
```

## Config (`.env`)

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | Required. GitHub PAT |
| `SCRAPE_INTERVAL_MINUTES` | `60` | How often runner.py scrapes |
| `MAX_PAGES_PER_RUN` | `5` | Search result pages per run (30 users/page) |

## Notes

- Scraper resumes from `data/progress.json` — safe to stop and restart
- Rate limit is checked before every run; pauses automatically if quota is low
- All data is public GitHub info — no private data collected
- Gender is inferred from display name via `gender-guesser` + pronoun detection in bio
