# socialcatchup

Simple tools to find the latest things.

## Daily YouTube search page

This repository now includes a small Python script that can query the YouTube Data API, keep the latest videos for a search query sorted from newest to oldest, and write the results to static files that can be hosted from GitHub.

### Default search query

The default query is based on the initial request:

```text
"dinosaur jr" OR mascis OR "j mascis" OR "jmascis" OR sebadoh OR "lou Barlow" OR dinosaurjr OR #dinosaurjr OR #jmascis
```

### Local usage

Set a YouTube API key and run:

```bash
export YOUTUBE_API_KEY=your_api_key
python3 /home/runner/work/socialcatchup/socialcatchup/youtube_daily_update.py --output-dir /home/runner/work/socialcatchup/socialcatchup/docs
```

Useful options:

- `--query` to override the search string
- `--limit` to choose how many videos to keep, up to 100 by default
- `--output-dir` to choose where the generated page and JSON are written

The script writes:

- `docs/index.html` — a simple human-readable page
- `docs/results.json` — the underlying data feed

### GitHub automation

The workflow at `.github/workflows/update-youtube-search.yml` runs twice a day and can also be triggered manually.

Repository configuration:

- add a `YOUTUBE_API_KEY` repository secret
- optionally add `YOUTUBE_SEARCH_QUERY` and `YOUTUBE_RESULTS_LIMIT` repository variables

Once the workflow runs, it updates the files in `docs/`, which can be published with GitHub Pages if desired.
