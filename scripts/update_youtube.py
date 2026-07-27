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
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import urlopen

YOUTUBE_SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_PLAYLISTS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlists"
LOGGER = logging.getLogger("socialcatchup.youtube")


def configure_logging() -> None:
    """Configure script logging with environment-controlled verbosity."""

    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


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
    video_count: int | None


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
    LOGGER.debug(
        "youtube.search request type=%s max_results=%s has_page_token=%s",
        result_type,
        max_results,
        bool(page_token),
    )

    try:
        with urlopen(url, timeout=30) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)
    except HTTPError as exc:
        # Preserve API-provided details to make CI failures actionable.
        error_body = exc.read().decode("utf-8", errors="replace")
        message = build_youtube_http_error_message(exc.code, error_body)
        raise RuntimeError(
            f"YouTube search request failed (type={result_type}, has_page_token={bool(page_token)}): {message}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Network error while calling YouTube search API (type={result_type}, has_page_token={bool(page_token)}): {exc}"
        ) from exc


def youtube_playlists_request(api_key: str, playlist_ids: list[str]) -> dict[str, Any]:
    """Fetch contentDetails for a batch of playlist IDs."""

    params: dict[str, str] = {
        "part": "contentDetails",
        "id": ",".join(playlist_ids),
        "maxResults": "50",
        "key": api_key,
    }
    url = f"{YOUTUBE_PLAYLISTS_ENDPOINT}?{urlencode(params)}"
    LOGGER.debug("youtube.playlists request batch_size=%s", len(playlist_ids))

    try:
        with urlopen(url, timeout=30) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        message = build_youtube_http_error_message(exc.code, error_body)
        raise RuntimeError(
            f"YouTube playlists details request failed (batch_size={len(playlist_ids)}): {message}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Network error while calling YouTube playlists details API (batch_size={len(playlist_ids)}): {exc}"
        ) from exc


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


def normalize_playlist_result(item: dict[str, Any], video_count: int | None = None) -> PlaylistResult | None:
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
        video_count=video_count,
    )


def fetch_playlist_video_counts(api_key: str, playlist_ids: list[str]) -> dict[str, int]:
    """Fetch per-playlist video counts using batched playlist details calls."""

    counts: dict[str, int] = {}
    if not playlist_ids:
        return counts

    for start in range(0, len(playlist_ids), 50):
        batch = playlist_ids[start : start + 50]
        LOGGER.info(
            "Fetching playlist video counts batch %s-%s of %s",
            start + 1,
            min(start + len(batch), len(playlist_ids)),
            len(playlist_ids),
        )
        payload = youtube_playlists_request(api_key=api_key, playlist_ids=batch)
        for item in payload.get("items", []):
            playlist_id = item.get("id", "")
            count_value = item.get("contentDetails", {}).get("itemCount")
            if not playlist_id or count_value is None:
                continue
            try:
                counts[playlist_id] = int(count_value)
            except (TypeError, ValueError):
                continue

    return counts


def fetch_latest_videos(api_key: str, query: str, target_count: int) -> list[VideoResult]:
    """Fetch up to target_count videos in newest-first order, deduplicated by video ID."""

    videos: list[VideoResult] = []
    seen_ids: set[str] = set()
    page_token: str | None = None
    page_number = 0

    while len(videos) < target_count:
        page_number += 1
        page_size = min(50, target_count - len(videos))
        LOGGER.info(
            "Fetching video page %s (current=%s target=%s request_size=%s)",
            page_number,
            len(videos),
            target_count,
            page_size,
        )
        payload = youtube_search_request(
            api_key=api_key,
            query=query,
            max_results=page_size,
            page_token=page_token,
            result_type="video",
        )
        items = payload.get("items", [])
        LOGGER.debug("Video page %s returned %s raw items", page_number, len(items))

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
    LOGGER.info("Fetched %s videos total", len(videos))
    return videos


def fetch_latest_playlists(api_key: str, query: str, target_count: int) -> list[PlaylistResult]:
    """Fetch up to target_count playlists in newest-first order, deduplicated by playlist ID."""

    playlists: list[PlaylistResult] = []
    seen_ids: set[str] = set()
    page_token: str | None = None
    page_number = 0

    while len(playlists) < target_count:
        page_number += 1
        page_size = min(50, target_count - len(playlists))
        LOGGER.info(
            "Fetching playlist page %s (current=%s target=%s request_size=%s)",
            page_number,
            len(playlists),
            target_count,
            page_size,
        )
        payload = youtube_search_request(
            api_key=api_key,
            query=query,
            max_results=page_size,
            page_token=page_token,
            result_type="playlist",
        )
        items = payload.get("items", [])
        LOGGER.debug("Playlist page %s returned %s raw items", page_number, len(items))

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

    counts_by_id = fetch_playlist_video_counts(
        api_key=api_key,
        playlist_ids=[playlist.playlist_id for playlist in playlists],
    )

    playlists_with_counts: list[PlaylistResult] = []
    for playlist in playlists:
        playlists_with_counts.append(
            PlaylistResult(
                playlist_id=playlist.playlist_id,
                title=playlist.title,
                description=playlist.description,
                published_at=playlist.published_at,
                channel_title=playlist.channel_title,
                channel_id=playlist.channel_id,
                thumbnail_url=playlist.thumbnail_url,
                video_count=counts_by_id.get(playlist.playlist_id),
            )
        )

    LOGGER.info("Fetched %s playlists total", len(playlists_with_counts))
    return playlists_with_counts


def render_index_html(
    videos_fetch_utc: str | None,
    videos_fetch_iso: str | None,
    playlists_fetch_utc: str | None,
    playlists_fetch_iso: str | None,
    videos_count: int,
    playlists_count: int,
) -> str:
    """Render the landing page with per-platform freshness metadata."""

    videos_fetch_label = videos_fetch_utc or "No successful run yet"
    playlists_fetch_label = playlists_fetch_utc or "No successful run yet"
    videos_fetch_iso_attr = escape(videos_fetch_iso or "")
    playlists_fetch_iso_attr = escape(playlists_fetch_iso or "")

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
            <p class="meta">Each card shows its own latest successful fetch time.</p>
    </header>

    <section class=\"cards\">
      <a class=\"card\" href=\"youtube.html\">
        <h2>YouTube Videos</h2>
        <p>Newest uploads for your configured keyword query, embedded and sorted newest to oldest.</p>
                <p class="card-meta">Latest fetch: <span class="js-time" data-time-iso="{videos_fetch_iso_attr}">{escape(videos_fetch_label)}</span> | Results: {videos_count}</p>
      </a>
      <a class=\"card\" href=\"playlists.html\">
        <h2>YouTube Playlists</h2>
        <p>Newest playlists matching your keyword query, shown with embedded playlist players and descriptions.</p>
                <p class="card-meta">Latest fetch: <span class="js-time" data-time-iso="{playlists_fetch_iso_attr}">{escape(playlists_fetch_label)}</span> | Results: {playlists_count}</p>
      </a>
    </section>
  </main>
    {render_time_localization_script()}
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


def render_time_localization_script() -> str:
        """Render script that converts UTC timestamps to local time with relative labels."""

        return """<script>
(() => {
    const formatRelative = (targetDate) => {
        const deltaMs = Date.now() - targetDate.getTime();
        const absMs = Math.abs(deltaMs);
        const minute = 60 * 1000;
        const hour = 60 * minute;
        const day = 24 * hour;

        const suffix = deltaMs >= 0 ? "ago" : "from now";
        if (absMs < minute) {
            return deltaMs >= 0 ? "just now" : "in under a minute";
        }
        if (absMs < hour) {
            const value = Math.round(absMs / minute);
            return `${value} minute${value === 1 ? "" : "s"} ${suffix}`;
        }
        if (absMs < day) {
            const value = Math.round(absMs / hour);
            return `${value} hour${value === 1 ? "" : "s"} ${suffix}`;
        }

        const value = Math.round(absMs / day);
        return `${value} day${value === 1 ? "" : "s"} ${suffix}`;
    };

    const localFormatter = new Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "numeric",
        minute: "2-digit",
    });

    for (const element of document.querySelectorAll(".js-time[data-time-iso]")) {
        const isoValue = element.getAttribute("data-time-iso");
        if (!isoValue) {
            continue;
        }

        const parsed = new Date(isoValue);
        if (Number.isNaN(parsed.getTime())) {
            continue;
        }

        element.textContent = `${localFormatter.format(parsed)} (${formatRelative(parsed)})`;
    }
})();
</script>"""


def render_pagination_script() -> str:
    """Render script that paginates video cards client-side for static pages."""

    return """<script>
(() => {
    for (const grid of document.querySelectorAll(".video-grid[data-page-size]")) {
        const pageSize = Number.parseInt(grid.getAttribute("data-page-size") || "25", 10);
        const cards = [...grid.querySelectorAll(".video-card")];
        if (!Number.isFinite(pageSize) || pageSize < 1 || cards.length <= pageSize) {
            continue;
        }

        const pageParam = grid.getAttribute("data-page-param") || "page";
        const pageCount = Math.ceil(cards.length / pageSize);
        let page = 1;

        const controls = document.createElement("nav");
        controls.className = "pagination-controls";
        controls.setAttribute("aria-label", "Pagination");

        const prev = document.createElement("button");
        prev.type = "button";
        prev.className = "pagination-button";
        prev.textContent = "Previous";

        const summary = document.createElement("p");
        summary.className = "pagination-summary";

        const next = document.createElement("button");
        next.type = "button";
        next.className = "pagination-button";
        next.textContent = "Next";

        const pages = document.createElement("div");
        pages.className = "pagination-pages";

        const pageButtons = [];
        for (let index = 1; index <= pageCount; index += 1) {
            const pageButton = document.createElement("button");
            pageButton.type = "button";
            pageButton.className = "pagination-page-button";
            pageButton.textContent = String(index);
            pageButton.setAttribute("aria-label", `Go to page ${index}`);
            pageButton.addEventListener("click", () => {
                setPage(index, { scroll: true, updateUrl: true });
            });
            pages.append(pageButton);
            pageButtons.push(pageButton);
        }

        controls.append(prev, summary, next, pages);
        grid.insertAdjacentElement("afterend", controls);

        const readPageFromUrl = () => {
            const raw = new URLSearchParams(window.location.search).get(pageParam);
            const parsed = Number.parseInt(raw || "1", 10);
            if (!Number.isFinite(parsed)) {
                return 1;
            }
            return Math.min(Math.max(parsed, 1), pageCount);
        };

        const writePageToUrl = () => {
            const url = new URL(window.location.href);
            if (page <= 1) {
                url.searchParams.delete(pageParam);
            } else {
                url.searchParams.set(pageParam, String(page));
            }
            window.history.replaceState(null, "", url.toString());
        };

        const renderPage = () => {
            const start = (page - 1) * pageSize;
            const end = start + pageSize;

            cards.forEach((card, index) => {
                card.hidden = index < start || index >= end;
            });

            const first = start + 1;
            const last = Math.min(end, cards.length);
            summary.textContent = `Page ${page} of ${pageCount} • Showing ${first}-${last} of ${cards.length}`;

            prev.disabled = page <= 1;
            next.disabled = page >= pageCount;

            pageButtons.forEach((button, index) => {
                const isCurrent = index + 1 === page;
                button.disabled = isCurrent;
                if (isCurrent) {
                    button.setAttribute("aria-current", "page");
                } else {
                    button.removeAttribute("aria-current");
                }
            });
        };

        const setPage = (nextPage, options = { scroll: false, updateUrl: false }) => {
            const bounded = Math.min(Math.max(nextPage, 1), pageCount);
            if (bounded === page) {
                return;
            }
            page = bounded;
            renderPage();
            if (options.updateUrl) {
                writePageToUrl();
            }
            if (options.scroll) {
                grid.scrollIntoView({ behavior: "smooth", block: "start" });
            }
        };

        prev.addEventListener("click", () => {
            setPage(page - 1, { scroll: true, updateUrl: true });
        });

        next.addEventListener("click", () => {
            setPage(page + 1, { scroll: true, updateUrl: true });
        });

        window.addEventListener("popstate", () => {
            setPage(readPageFromUrl(), { scroll: false, updateUrl: false });
        });

        page = readPageFromUrl();
        renderPage();
    }
})();
</script>"""


def extract_search_tokens(text: str) -> set[str]:
    """Extract lowercase word tokens used for simple relevance scoring."""

    return {token for token in re.findall(r"[A-Za-z0-9]+", text.lower()) if len(token) > 2}


def score_playlist_relevance(video: VideoResult, playlist: PlaylistResult) -> tuple[int, int]:
    """Return relevance score and timestamp rank for deterministic playlist ordering."""

    channel_bonus = 100 if video.channel_id and video.channel_id == playlist.channel_id else 0
    video_tokens = extract_search_tokens(f"{video.title} {video.description}")
    playlist_tokens = extract_search_tokens(f"{playlist.title} {playlist.description}")
    token_overlap = len(video_tokens & playlist_tokens)

    try:
        published_rank = int(
            datetime.fromisoformat(playlist.published_at.replace("Z", "+00:00")).timestamp()
        )
    except ValueError:
        published_rank = 0

    return channel_bonus + token_overlap, published_rank


def get_related_playlists(video: VideoResult, playlists: list[PlaylistResult], limit: int = 3) -> list[PlaylistResult]:
    """Select a short set of playlists most related to a video."""

    ranked = sorted(
        playlists,
        key=lambda playlist: score_playlist_relevance(video, playlist),
        reverse=True,
    )

    related: list[PlaylistResult] = []
    for playlist in ranked:
        score, _ = score_playlist_relevance(video, playlist)
        if score <= 0:
            continue
        related.append(playlist)
        if len(related) >= limit:
            return related

    # Keep the section populated even when no token/channel matches are found.
    return ranked[:limit]


def render_related_playlists(playlists: list[PlaylistResult]) -> str:
    """Render related playlist links shown within a video card."""

    if not playlists:
        return '<div class="related-playlists"><p class="related-playlists-label">Related playlists: none</p></div>'

    items = ""
    for playlist in playlists:
        count_suffix = f" ({playlist.video_count} videos)" if playlist.video_count is not None else ""
        items += (
            f'<li><a href="https://www.youtube.com/playlist?list={playlist.playlist_id}" target="_blank" rel="noopener noreferrer">'
            f"{escape(playlist.title)}{escape(count_suffix)}</a></li>"
        )
    return (
        '<div class="related-playlists">'
        '<p class="related-playlists-label">Related playlists</p>'
        f'<ul class="related-playlists-list">{items}</ul>'
        "</div>"
    )


def render_video_card(video: VideoResult, related_playlists: list[PlaylistResult]) -> str:
    """Render one embedded YouTube video card with metadata and description."""

    safe_title = escape(video.title)
    safe_description = escape(video.description).replace("\n", "<br>")
    safe_channel = escape(video.channel_title)
    published_label = escape(format_timestamp(video.published_at))
    published_iso_attr = escape(video.published_at)
    watch_url = f"https://www.youtube.com/watch?v={video.video_id}"
    channel_url = f"https://www.youtube.com/channel/{video.channel_id}" if video.channel_id else "https://www.youtube.com"
    related_html = render_related_playlists(related_playlists)

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
        <p class=\"video-meta\"><span class=\"js-time\" data-time-iso=\"{published_iso_attr}\">{published_label}</span> | <a href=\"{channel_url}\" target=\"_blank\" rel=\"noopener noreferrer\">{safe_channel}</a></p>
        {related_html}
        <p>{safe_description}</p>
  </div>
</article>
"""


def render_youtube_html(
        query: str,
        fetched_at_utc: str,
    fetched_at_iso: str,
        videos: list[VideoResult],
        playlists: list[PlaylistResult],
) -> str:
    """Render the YouTube results page with embeds and descriptions."""

    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    cards = "\n".join(
        render_video_card(video, get_related_playlists(video, playlists)) for video in videos
    )

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
            <p class="lede">Query: <a href="{search_url}" target="_blank" rel="noopener noreferrer">{escape(query)}</a></p>
            <p class=\"meta\">Latest successful fetch: <span class=\"js-time\" data-time-iso=\"{escape(fetched_at_iso)}\">{escape(fetched_at_utc)}</span> | Total videos: {len(videos)}</p>
    </header>

        <section class=\"video-grid\" data-page-size=\"25\">
      {cards}
    </section>
  </main>
    {render_time_localization_script()}
        {render_pagination_script()}
</body>
</html>
"""


def render_playlist_card(playlist: PlaylistResult) -> str:
    """Render one embedded YouTube playlist card with metadata and description."""

    safe_title = escape(playlist.title)
    safe_description = escape(playlist.description).replace("\n", "<br>")
    safe_channel = escape(playlist.channel_title)
    published_label = escape(format_timestamp(playlist.published_at))
    published_iso_attr = escape(playlist.published_at)
    watch_url = f"https://www.youtube.com/playlist?list={playlist.playlist_id}"
    channel_url = f"https://www.youtube.com/channel/{playlist.channel_id}" if playlist.channel_id else "https://www.youtube.com"
    count_label = (
        f"{playlist.video_count} videos"
        if playlist.video_count is not None
        else "Video count unavailable"
    )

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
                <p class="video-meta"><span class="js-time" data-time-iso="{published_iso_attr}">{published_label}</span> | <a href="{channel_url}" target="_blank" rel="noopener noreferrer">{safe_channel}</a> | {escape(count_label)}</p>
    <p>{safe_description}</p>
  </div>
</article>
"""


def render_playlists_html(
    query: str,
    fetched_at_utc: str,
    fetched_at_iso: str,
    playlists: list[PlaylistResult],
) -> str:
    """Render the YouTube playlists page with playlist embeds and descriptions."""

    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
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
            <p class="lede">Query: <a href="{search_url}" target="_blank" rel="noopener noreferrer">{escape(query)}</a></p>
            <p class=\"meta\">Latest successful fetch: <span class=\"js-time\" data-time-iso=\"{escape(fetched_at_iso)}\">{escape(fetched_at_utc)}</span> | Total playlists: {len(playlists)}</p>
    </header>

        <section class=\"video-grid\" data-page-size=\"25\">
      {cards}
    </section>
  </main>
    {render_time_localization_script()}
        {render_pagination_script()}
</body>
</html>
"""


def ensure_static_files(docs_dir: Path) -> None:
    """Create docs output directory and static asset directory if needed."""

    (docs_dir / "assets").mkdir(parents=True, exist_ok=True)


def main() -> int:
    """Run fetch + render workflow for YouTube video and playlist results."""

    configure_logging()

    root = Path(__file__).resolve().parents[1]
    load_env_file(root / ".env")
    config_path = root / "config" / "searches.json"
    videos_data_path = root / "data" / "youtube_latest.json"
    playlists_data_path = root / "data" / "youtube_playlists_latest.json"
    docs_dir = root / "docs"

    LOGGER.info("Starting YouTube sync")
    LOGGER.info("Using config file: %s", config_path)
    LOGGER.info("Output targets: %s, %s, %s", videos_data_path, playlists_data_path, docs_dir)

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

    fetched_at_now = datetime.now(UTC)
    fetched_at_utc = fetched_at_now.strftime("%Y-%m-%d %H:%M UTC")
    fetched_at_iso = fetched_at_now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    LOGGER.info("Requested video query: %s", query)
    LOGGER.info("Requested playlist query: %s", playlists_query)
    LOGGER.info("Target counts -> videos=%s playlists=%s", max_results, playlists_max_results)
    videos = fetch_latest_videos(api_key=api_key, query=query, target_count=max_results)
    playlists = fetch_latest_playlists(
        api_key=api_key,
        query=playlists_query,
        target_count=playlists_max_results,
    )

    videos_payload = {
        "platform": "youtube",
        "query": query,
        "max_results": max_results,
        "fetched_at_utc": fetched_at_utc,
        "fetched_at_iso": fetched_at_iso,
        "result_count": len(videos),
        "videos": [video.__dict__ for video in videos],
    }
    playlists_payload = {
        "platform": "youtube_playlists",
        "query": playlists_query,
        "max_results": playlists_max_results,
        "fetched_at_utc": fetched_at_utc,
        "fetched_at_iso": fetched_at_iso,
        "result_count": len(playlists),
        "playlists": [playlist.__dict__ for playlist in playlists],
    }

    write_json(videos_data_path, videos_payload)
    write_json(playlists_data_path, playlists_payload)
    ensure_static_files(docs_dir)
    LOGGER.info("Wrote JSON payloads")

    index_html = render_index_html(
        videos_fetch_utc=videos_payload.get("fetched_at_utc"),
        videos_fetch_iso=videos_payload.get("fetched_at_iso"),
        playlists_fetch_utc=playlists_payload.get("fetched_at_utc"),
        playlists_fetch_iso=playlists_payload.get("fetched_at_iso"),
        videos_count=len(videos),
        playlists_count=len(playlists),
    )
    youtube_html = render_youtube_html(
        query=query,
        fetched_at_utc=fetched_at_utc,
        fetched_at_iso=fetched_at_iso,
        videos=videos,
        playlists=playlists,
    )
    playlists_html = render_playlists_html(
        query=playlists_query,
        fetched_at_utc=fetched_at_utc,
        fetched_at_iso=fetched_at_iso,
        playlists=playlists,
    )

    (docs_dir / "index.html").write_text(index_html, encoding="utf-8")
    (docs_dir / "youtube.html").write_text(youtube_html, encoding="utf-8")
    (docs_dir / "playlists.html").write_text(playlists_html, encoding="utf-8")

    LOGGER.info("Wrote docs pages: index.html, youtube.html, playlists.html")
    print(f"Fetched {len(videos)} videos and {len(playlists)} playlists and generated docs pages.")
    return 0


if __name__ == "__main__":
    configure_logging()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        LOGGER.exception("Unhandled failure during update_youtube.py run")
        raise
