# SA Dev Stats

Scrapes public GitHub profiles of South African developers via GitHub Actions, commits the data back to the repo, and serves a live dashboard via GitHub Pages.

## How it works

1. GitHub Actions runs on a cron (every 6 hours) — calls `scraper.py` then `build_stats.py`
2. Updated JSON files are committed back to the repo automatically
3. GitHub Pages serves `index.html` which reads `data/stats.json` directly

## Setup

```bash
git clone <your-repo>
cd sa-dev-stats
```

**1. Add your GitHub PAT as a repo secret**

Go to **Settings → Secrets and variables → Actions → New repository secret**

- Name: `GH_SCRAPER_TOKEN`
- Value: your GitHub Personal Access Token (needs `read:user` and `public_repo` scopes)

Generate one at https://github.com/settings/tokens

**2. Enable GitHub Pages**

Go to **Settings → Pages**
- Source: `Deploy from a branch`
- Branch: `main`, folder: `/ (root)`

**3. Trigger the first run manually**

Go to **Actions → Scrape SA Dev Stats → Run workflow**

After it completes, `data/stats.json` will be committed and the dashboard will be live at:
`https://<your-username>.github.io/<repo-name>/`

## Cron schedule

Runs every 6 hours by default. Change in `.github/workflows/scrape.yml`:

```yaml
- cron: '0 */6 * * *'
```

## Optional repo variable

Set `MAX_PAGES_PER_RUN` under **Settings → Variables → Actions** to control how many search result pages are fetched per run (default: 5, i.e. ~150 users per run).

## Local usage

```bash
pip install -r requirements.txt
export GH_SCRAPER_TOKEN=your_token
python scraper.py
python build_stats.py
python -m http.server 8080
# Open http://localhost:8080
```

## Notes

- Scraper resumes from `data/progress.json` — safe to re-run, no duplicates
- Rate limit is checked before every API call; pauses automatically if quota is low
- All data is public GitHub info only
- Gender is inferred from display name via `gender-guesser` + pronoun detection in bio
