#!/usr/bin/env python3
"""Fetch latest YouTube videos/playlists for configured keywords and build static pages.

This script is designed for GitHub Actions + GitHub Pages:
- Reads query settings from config/searches.json.
- Calls the YouTube Data API v3 search endpoint.
- Writes normalized results to data/youtube_latest.json and data/youtube_playlists_latest.json.
- Generates docs/index.html, docs/youtube.html, and docs/playlists.html.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

YOUTUBE_SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs from a local .env file if present.

    The parser intentionally supports a small safe subset:
    - Ignores blank lines and # comments.
    - Accepts KEY=VALUE lines.
    - Removes surrounding single or double quotes from values.
    - Does not override variables already set in the environment.
    """

    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]

        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class VideoResult:
    """A normalized YouTube video result used by the renderer."""

    video_id: str
    title: str
    description: str
    published_at: str
    channel_title: str
    channel_id: str
    thumbnail_url: str


@dataclass(frozen=True)
class PlaylistResult:
    """A normalized YouTube playlist result used by the renderer."""

    playlist_id: str
    title: str
    description: str
    published_at: str
    channel_title: str
    channel_id: str
    thumbnail_url: str


def read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON file into a dictionary."""

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically to prevent partial files on interrupted writes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.write("\n")
    temp_path.replace(path)


def youtube_search_request(
    api_key: str,
    query: str,
    max_results: int,
    page_token: str | None,
    result_type: str,
) -> dict[str, Any]:
    """Send one request to YouTube search and return the decoded JSON payload."""

    params: dict[str, str | int] = {
        "part": "snippet",
        "type": result_type,
        "order": "date",
        "maxResults": max_results,
        "q": query,
        "key": api_key,
    }

    if result_type == "video":
        # Request videos that can be embedded and are allowed off youtube.com.
        params["videoEmbeddable"] = "true"
        params["videoSyndicated"] = "true"

    if page_token:
        params["pageToken"] = page_token

    url = f"{YOUTUBE_SEARCH_ENDPOINT}?{urlencode(params)}"

    try:
        with urlopen(url, timeout=30) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)
    except HTTPError as exc:
        # Preserve API-provided details to make CI failures actionable.
        error_body = exc.read().decode("utf-8", errors="replace")
        message = build_youtube_http_error_message(exc.code, error_body)
        raise RuntimeError(message) from exc
    except URLError as exc:
        raise RuntimeError(f"Network error while calling YouTube API: {exc}") from exc


def build_youtube_http_error_message(status_code: int, response_body: str) -> str:
    """Build a readable failure message from YouTube API HTTP errors."""

    fallback = f"YouTube API request failed with status {status_code}. Raw response: {response_body}"
    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        return fallback

    error = payload.get("error", {})
    error_message = error.get("message", "Unknown YouTube API error")
    reasons: list[str] = []
    for item in error.get("errors", []):
        reason = item.get("reason")
        if reason:
            reasons.append(reason)

    reason_text = ", ".join(sorted(set(reasons))) if reasons else "unknown_reason"

    hints: list[str] = []
    if any(reason in {"forbidden", "accessNotConfigured", "ipRefererBlocked"} for reason in reasons):
        hints.append(
            "Check Google Cloud key restrictions: for GitHub Actions use an API key with API restriction to YouTube Data API v3 and no HTTP referrer restriction."
        )
    if any(reason in {"keyInvalid", "badRequest"} for reason in reasons):
        hints.append("Verify the YOUTUBE_API_KEY secret value is correct and not truncated.")
    if any(reason in {"quotaExceeded", "dailyLimitExceeded"} for reason in reasons):
        hints.append("YouTube API quota is exceeded. Wait for reset or request more quota.")

    hint_text = f" Hints: {' | '.join(hints)}" if hints else ""
    return f"YouTube API error (status {status_code}, reasons: {reason_text}): {error_message}.{hint_text}"


def normalize_video_result(item: dict[str, Any]) -> VideoResult | None:
    """Convert one YouTube API item into VideoResult, or None when incomplete."""

    video_id = item.get("id", {}).get("videoId")
    snippet = item.get("snippet", {})
    if not video_id or not snippet:
        return None

    thumbnails = snippet.get("thumbnails", {})
    thumb = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}

    return VideoResult(
        video_id=video_id,
        title=snippet.get("title", "Untitled"),
        description=snippet.get("description", ""),
        published_at=snippet.get("publishedAt", ""),
        channel_title=snippet.get("channelTitle", ""),
        channel_id=snippet.get("channelId", ""),
        thumbnail_url=thumb.get("url", ""),
    )


def normalize_playlist_result(item: dict[str, Any]) -> PlaylistResult | None:
    """Convert one YouTube API item into PlaylistResult, or None when incomplete."""

    playlist_id = item.get("id", {}).get("playlistId")
    snippet = item.get("snippet", {})
    if not playlist_id or not snippet:
        return None

    thumbnails = snippet.get("thumbnails", {})
    thumb = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}

    return PlaylistResult(
        playlist_id=playlist_id,
        title=snippet.get("title", "Untitled"),
        description=snippet.get("description", ""),
        published_at=snippet.get("publishedAt", ""),
        channel_title=snippet.get("channelTitle", ""),
        channel_id=snippet.get("channelId", ""),
        thumbnail_url=thumb.get("url", ""),
    )


def fetch_latest_videos(api_key: str, query: str, target_count: int) -> list[VideoResult]:
    """Fetch up to target_count videos in newest-first order, deduplicated by video ID."""

    videos: list[VideoResult] = []
    seen_ids: set[str] = set()
    page_token: str | None = None

    while len(videos) < target_count:
        page_size = min(50, target_count - len(videos))
        payload = youtube_search_request(
            api_key=api_key,
            query=query,
            max_results=page_size,
            page_token=page_token,
            result_type="video",
        )
        items = payload.get("items", [])

        for item in items:
            normalized = normalize_video_result(item)
            if not normalized:
                continue
            if normalized.video_id in seen_ids:
                continue

            seen_ids.add(normalized.video_id)
            videos.append(normalized)

            if len(videos) >= target_count:
                break

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    videos.sort(key=lambda video: video.published_at, reverse=True)
    return videos


def fetch_latest_playlists(api_key: str, query: str, target_count: int) -> list[PlaylistResult]:
    """Fetch up to target_count playlists in newest-first order, deduplicated by playlist ID."""

    playlists: list[PlaylistResult] = []
    seen_ids: set[str] = set()
    page_token: str | None = None

    while len(playlists) < target_count:
        page_size = min(50, target_count - len(playlists))
        payload = youtube_search_request(
            api_key=api_key,
            query=query,
            max_results=page_size,
            page_token=page_token,
            result_type="playlist",
        )
        items = payload.get("items", [])

        for item in items:
            normalized = normalize_playlist_result(item)
            if not normalized:
                continue
            if normalized.playlist_id in seen_ids:
                continue

            seen_ids.add(normalized.playlist_id)
            playlists.append(normalized)

            if len(playlists) >= target_count:
                break

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    playlists.sort(key=lambda playlist: playlist.published_at, reverse=True)
    return playlists


def render_index_html(last_fetch_utc: str | None) -> str:
    """Render the landing page for platform-specific result pages."""

    fetch_label = last_fetch_utc or "No successful run yet"

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Social Catchup</title>
  <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
  <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
  <link href=\"https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Fraunces:opsz,wght@9..144,600&display=swap\" rel=\"stylesheet\">
  <link rel=\"stylesheet\" href=\"assets/style.css\">
</head>
<body>
  <main class=\"wrap\">
    <header class=\"hero\">
      <p class=\"eyebrow\">socialcatchup</p>
      <h1>Latest Posts Across Platforms</h1>
      <p class=\"lede\">Start with YouTube now, then expand to Instagram, Reddit, Bluesky, and more.</p>
      <p class=\"meta\">Latest successful fetch: {escape(fetch_label)}</p>
    </header>

    <section class=\"cards\">
      <a class=\"card\" href=\"youtube.html\">
        <h2>YouTube Videos</h2>
        <p>Newest uploads for your configured keyword query, embedded and sorted newest to oldest.</p>
      </a>
      <a class=\"card\" href=\"playlists.html\">
        <h2>YouTube Playlists</h2>
        <p>Newest playlists matching your keyword query, shown with embedded playlist players and descriptions.</p>
      </a>
    </section>
  </main>
</body>
</html>
"""


def format_timestamp(iso_value: str) -> str:
    """Convert an RFC3339 UTC timestamp to a readable UTC display string."""

    if not iso_value:
        return "Unknown publish date"
    try:
        parsed = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_value


def render_video_card(video: VideoResult) -> str:
    """Render one embedded YouTube video card with metadata and description."""

    safe_title = escape(video.title)
    safe_description = escape(video.description).replace("\n", "<br>")
    safe_channel = escape(video.channel_title)
    published_label = escape(format_timestamp(video.published_at))
    watch_url = f"https://www.youtube.com/watch?v={video.video_id}"
    channel_url = f"https://www.youtube.com/channel/{video.channel_id}" if video.channel_id else "https://www.youtube.com"

    return f"""
<article class=\"video-card\">
  <div class=\"embed-wrap\">
    <iframe
      src=\"https://www.youtube.com/embed/{video.video_id}\"
      title={json.dumps(video.title)}
      loading=\"lazy\"
      allow=\"accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share\"
      referrerpolicy=\"strict-origin-when-cross-origin\"
      allowfullscreen>
    </iframe>
  </div>

  <div class=\"video-content\">
    <h2><a href=\"{watch_url}\" target=\"_blank\" rel=\"noopener noreferrer\">{safe_title}</a></h2>
    <p class=\"video-meta\">{published_label} | <a href=\"{channel_url}\" target=\"_blank\" rel=\"noopener noreferrer\">{safe_channel}</a></p>
    <p>{safe_description}</p>
  </div>
</article>
"""


def render_youtube_html(query: str, fetched_at_utc: str, videos: list[VideoResult]) -> str:
    """Render the YouTube results page with embeds and descriptions."""

    cards = "\n".join(render_video_card(video) for video in videos)

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>YouTube Results | Social Catchup</title>
  <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
  <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
  <link href=\"https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Fraunces:opsz,wght@9..144,600&display=swap\" rel=\"stylesheet\">
  <link rel=\"stylesheet\" href=\"assets/style.css\">
</head>
<body>
  <main class=\"wrap\">
    <p><a href=\"index.html\">Back to all platforms</a></p>
    <header class=\"hero\">
      <p class=\"eyebrow\">YouTube</p>
      <h1>Latest Uploads</h1>
      <p class=\"lede\">Query: {escape(query)}</p>
      <p class=\"meta\">Latest successful fetch: {escape(fetched_at_utc)} | Total videos: {len(videos)}</p>
    </header>

    <section class=\"video-grid\">
      {cards}
    </section>
  </main>
</body>
</html>
"""


def render_playlist_card(playlist: PlaylistResult) -> str:
    """Render one embedded YouTube playlist card with metadata and description."""

    safe_title = escape(playlist.title)
    safe_description = escape(playlist.description).replace("\n", "<br>")
    safe_channel = escape(playlist.channel_title)
    published_label = escape(format_timestamp(playlist.published_at))
    watch_url = f"https://www.youtube.com/playlist?list={playlist.playlist_id}"
    channel_url = f"https://www.youtube.com/channel/{playlist.channel_id}" if playlist.channel_id else "https://www.youtube.com"

    return f"""
<article class=\"video-card\">
  <div class=\"embed-wrap\">
    <iframe
      src=\"https://www.youtube.com/embed/videoseries?list={playlist.playlist_id}\"
      title={json.dumps(playlist.title)}
      loading=\"lazy\"
      allow=\"accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share\"
      referrerpolicy=\"strict-origin-when-cross-origin\"
      allowfullscreen>
    </iframe>
  </div>

  <div class=\"video-content\">
    <h2><a href=\"{watch_url}\" target=\"_blank\" rel=\"noopener noreferrer\">{safe_title}</a></h2>
    <p class=\"video-meta\">{published_label} | <a href=\"{channel_url}\" target=\"_blank\" rel=\"noopener noreferrer\">{safe_channel}</a></p>
    <p>{safe_description}</p>
  </div>
</article>
"""


def render_playlists_html(query: str, fetched_at_utc: str, playlists: list[PlaylistResult]) -> str:
    """Render the YouTube playlists page with playlist embeds and descriptions."""

    cards = "\n".join(render_playlist_card(playlist) for playlist in playlists)

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>YouTube Playlists | Social Catchup</title>
  <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
  <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
  <link href=\"https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Fraunces:opsz,wght@9..144,600&display=swap\" rel=\"stylesheet\">
  <link rel=\"stylesheet\" href=\"assets/style.css\">
</head>
<body>
  <main class=\"wrap\">
    <p><a href=\"index.html\">Back to all platforms</a></p>
    <header class=\"hero\">
      <p class=\"eyebrow\">YouTube Playlists</p>
      <h1>Latest Playlists</h1>
      <p class=\"lede\">Query: {escape(query)}</p>
      <p class=\"meta\">Latest successful fetch: {escape(fetched_at_utc)} | Total playlists: {len(playlists)}</p>
    </header>

    <section class=\"video-grid\">
      {cards}
    </section>
  </main>
</body>
</html>
"""


def ensure_static_files(docs_dir: Path) -> None:
    """Create docs output directory and static asset directory if needed."""

    (docs_dir / "assets").mkdir(parents=True, exist_ok=True)


def main() -> int:
    """Run fetch + render workflow for YouTube video and playlist results."""

    root = Path(__file__).resolve().parents[1]
    load_env_file(root / ".env")
    config_path = root / "config" / "searches.json"
    videos_data_path = root / "data" / "youtube_latest.json"
    playlists_data_path = root / "data" / "youtube_playlists_latest.json"
    docs_dir = root / "docs"

    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("YOUTUBE_API_KEY is not set. Add it to your environment or GitHub Actions secrets.")

    config = read_json(config_path)
    youtube_cfg = config.get("youtube", {})
    playlists_cfg = config.get("youtube_playlists", {})

    query = str(youtube_cfg.get("query", "")).strip()
    max_results = int(youtube_cfg.get("max_results", 100))
    playlists_query = str(playlists_cfg.get("query", query)).strip()
    playlists_max_results = int(playlists_cfg.get("max_results", 50))

    if not query:
        raise SystemExit("YouTube query is empty in config/searches.json.")
    if max_results < 1:
        raise SystemExit("youtube.max_results must be at least 1.")
    if not playlists_query:
        raise SystemExit("YouTube playlists query is empty in config/searches.json.")
    if playlists_max_results < 1:
        raise SystemExit("youtube_playlists.max_results must be at least 1.")

    fetched_at_utc = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    try:
        videos = fetch_latest_videos(api_key=api_key, query=query, target_count=max_results)
        playlists = fetch_latest_playlists(
            api_key=api_key,
            query=playlists_query,
            target_count=playlists_max_results,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    videos_payload = {
        "platform": "youtube",
        "query": query,
        "max_results": max_results,
        "fetched_at_utc": fetched_at_utc,
        "result_count": len(videos),
        "videos": [video.__dict__ for video in videos],
    }
    playlists_payload = {
        "platform": "youtube_playlists",
        "query": playlists_query,
        "max_results": playlists_max_results,
        "fetched_at_utc": fetched_at_utc,
        "result_count": len(playlists),
        "playlists": [playlist.__dict__ for playlist in playlists],
    }

    write_json(videos_data_path, videos_payload)
    write_json(playlists_data_path, playlists_payload)
    ensure_static_files(docs_dir)

    index_html = render_index_html(fetched_at_utc)
    youtube_html = render_youtube_html(query=query, fetched_at_utc=fetched_at_utc, videos=videos)
    playlists_html = render_playlists_html(
        query=playlists_query,
        fetched_at_utc=fetched_at_utc,
        playlists=playlists,
    )

    (docs_dir / "index.html").write_text(index_html, encoding="utf-8")
    (docs_dir / "youtube.html").write_text(youtube_html, encoding="utf-8")
    (docs_dir / "playlists.html").write_text(playlists_html, encoding="utf-8")

    print(f"Fetched {len(videos)} videos and {len(playlists)} playlists and generated docs pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
