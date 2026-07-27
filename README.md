# socialcatchup

Social Catchup is a Python-powered, static-site project that collects the latest social posts for keyword queries and publishes them to GitHub Pages.

The first platform implemented is YouTube.

## What It Does (YouTube)

- Uses the YouTube Data API v3 instead of the YouTube web UI search.
- Fetches newest uploads for a configurable keyword query.
- Fetches newest playlists for the same keyword query on a separate page.
- Targets up to 100 results (API page size is 50, so it requests multiple pages).
- Sorts newest to oldest.
- Prefers videos that allow external embedding/syndication.
- Renders a static page with embedded videos and descriptions.
- Renders a second static page for playlist embeds and descriptions.
- Shows the latest successful fetch time.
- Runs on a schedule in GitHub Actions and publishes content in the repo's docs site.

## Current Query

The default query is configured in [config/searches.json](config/searches.json):

"dinosaur jr" OR mascis OR "j mascis" OR "jmascis" OR sebadoh OR "lou Barlow" OR "dinosaurjr"

## Repository Structure

- [scripts/update_youtube.py](scripts/update_youtube.py): Fetches YouTube API results and builds static HTML pages.
- [config/searches.json](config/searches.json): Query and max-results configuration.
- [data/youtube_latest.json](data/youtube_latest.json): Most recent normalized fetch output.
- [data/youtube_playlists_latest.json](data/youtube_playlists_latest.json): Most recent playlist fetch output.
- [docs/index.html](docs/index.html): Landing page for platform sections.
- [docs/youtube.html](docs/youtube.html): YouTube results page.
- [docs/playlists.html](docs/playlists.html): YouTube playlist results page.
- [.github/workflows/update-youtube.yml](.github/workflows/update-youtube.yml): Scheduled workflow (every 12 hours) and manual run support.

## Setup

1. Create a YouTube Data API v3 key in Google Cloud.
2. In GitHub repo settings, add this secret:
   - `YOUTUBE_API_KEY`
3. Enable GitHub Pages:
   - Source: `Deploy from a branch`
   - Branch: `main`
   - Folder: `/docs`

After the workflow runs successfully, your site will be available via GitHub Pages with refreshed YouTube results.

## Where To Put Your API Key Safely

Use one of these safe locations:

1. Local development (recommended): create a `.env` file in the repository root.
2. GitHub Actions: set repository secret `YOUTUBE_API_KEY`.

Local `.env` example:

```bash
YOUTUBE_API_KEY="your_real_key_here"
```

This project includes `.env.example` as a template, and `.gitignore` is configured so `.env` and `.env.*` are not committed.

If a real key was ever committed previously, rotate it in Google Cloud and replace it with a new key.

## Local Run

Set your API key and run the Python script.

Option A: use a local `.env` file in the repo root.

Option B: export from the shell.

```bash
export YOUTUBE_API_KEY="your_api_key_here"
python3 scripts/update_youtube.py
```

This updates:

- [data/youtube_latest.json](data/youtube_latest.json)
- [data/youtube_playlists_latest.json](data/youtube_playlists_latest.json)
- [docs/index.html](docs/index.html)
- [docs/youtube.html](docs/youtube.html)
- [docs/playlists.html](docs/playlists.html)

## Playlist Notes

- Playlist search can help surface curated sets that include hard-to-find material.
- YouTube keyword search does not directly discover truly unlisted videos by keyword, but embedded playlists may include items that are only practically discoverable through the playlist itself.

## Scheduling

The workflow is configured to run every 12 hours and on manual dispatch.

To change frequency, edit the cron expression in [update-youtube.yml](.github/workflows/update-youtube.yml#L6).

## Extending to Other Platforms

The site is structured for one page/section per platform.

Next platforms you mentioned (future work):

- Instagram
- Flickr
- Reddit
- Facebook public posts
- X
- Threads
- Bluesky

A typical extension pattern is:

1. Add platform config.
2. Add a platform fetcher script/module.
3. Normalize output to a common JSON shape.
4. Generate a dedicated docs page and add a card on the index page.
