from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_QUERY = (
    '"dinosaur jr" OR mascis OR "j mascis" OR "jmascis" OR sebadoh '
    'OR "lou Barlow" OR dinosaurjr OR #dinosaurjr OR #jmascis'
)
DEFAULT_LIMIT = 100
MAX_API_RESULTS = 50
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


@dataclass(frozen=True)
class VideoResult:
    video_id: str
    title: str
    channel_title: str
    published_at: str
    description: str
    thumbnail_url: str

    @property
    def video_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch the latest YouTube search results and build a static page."
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("YOUTUBE_API_KEY"),
        help="YouTube Data API key. Defaults to YOUTUBE_API_KEY.",
    )
    parser.add_argument(
        "--query",
        default=env_or_default("YOUTUBE_SEARCH_QUERY", DEFAULT_QUERY),
        help="Search query. Defaults to YOUTUBE_SEARCH_QUERY or the built-in query.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(env_or_default("YOUTUBE_RESULTS_LIMIT", str(DEFAULT_LIMIT))),
        help="How many recent videos to keep.",
    )
    parser.add_argument(
        "--output-dir",
        default=env_or_default("YOUTUBE_OUTPUT_DIR", "docs"),
        help="Directory for generated output files.",
    )
    return parser.parse_args(argv)


def build_search_url(
    *, api_key: str, query: str, max_results: int, page_token: str | None = None
) -> str:
    params = {
        "part": "snippet",
        "type": "video",
        "order": "date",
        "maxResults": str(max_results),
        "q": query,
        "key": api_key,
    }
    if page_token:
        params["pageToken"] = page_token
    return f"{SEARCH_URL}?{urlencode(params)}"


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "socialcatchup/1.0"})
    with urlopen(request) as response:  # noqa: S310
        return json.load(response)


def extract_videos(payload: dict) -> list[VideoResult]:
    videos: list[VideoResult] = []
    for item in payload.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        if not video_id or not snippet.get("publishedAt"):
            continue

        thumbnail_url = (
            snippet.get("thumbnails", {}).get("medium", {}).get("url")
            or snippet.get("thumbnails", {}).get("default", {}).get("url")
            or ""
        )
        videos.append(
            VideoResult(
                video_id=video_id,
                title=snippet.get("title", ""),
                channel_title=snippet.get("channelTitle", ""),
                published_at=snippet["publishedAt"],
                description=snippet.get("description", ""),
                thumbnail_url=thumbnail_url,
            )
        )
    return videos


def fetch_recent_videos(
    *,
    api_key: str,
    query: str,
    limit: int,
    fetcher: Callable[[str], dict] = fetch_json,
) -> list[VideoResult]:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    page_token: str | None = None
    results: list[VideoResult] = []

    while len(results) < limit:
        payload = fetcher(
            build_search_url(
                api_key=api_key,
                query=query,
                max_results=min(MAX_API_RESULTS, limit - len(results)),
                page_token=page_token,
            )
        )
        results.extend(extract_videos(payload))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return sorted(results, key=lambda video: video.published_at, reverse=True)[:limit]


def render_html(*, query: str, videos: list[VideoResult], generated_at: str) -> str:
    items = []
    for video in videos:
        thumbnail = (
            f'<img src="{escape(video.thumbnail_url)}" alt="" loading="lazy" />'
            if video.thumbnail_url
            else ""
        )
        items.append(
            "<li>"
            f'<a href="{escape(video.video_url)}">{escape(video.title)}</a>'
            f"<p>{escape(video.channel_title)} · {escape(video.published_at)}</p>"
            f"{thumbnail}"
            f"<p>{escape(video.description)}</p>"
            "</li>"
        )

    results_html = "\n".join(items) or "<li>No videos found.</li>"
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>socialcatchup YouTube results</title>
    <style>
      body {{ font-family: Arial, sans-serif; line-height: 1.5; margin: 2rem auto; max-width: 60rem; padding: 0 1rem; }}
      ul {{ list-style: none; padding: 0; }}
      li {{ border-top: 1px solid #ddd; padding: 1rem 0; }}
      img {{ display: block; margin: 0.5rem 0; max-width: 320px; width: 100%; }}
      code {{ background: #f4f4f4; padding: 0.2rem 0.4rem; }}
    </style>
  </head>
  <body>
    <h1>Latest YouTube results</h1>
    <p><strong>Query:</strong> <code>{escape(query)}</code></p>
    <p><strong>Generated:</strong> {escape(generated_at)}</p>
    <ul>
      {results_html}
    </ul>
  </body>
</html>
"""


def write_outputs(
    *,
    output_dir: Path,
    query: str,
    videos: list[VideoResult],
    generated_at: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_payload = {
        "query": query,
        "generated_at": generated_at,
        "count": len(videos),
        "videos": [
            {
                "video_id": video.video_id,
                "title": video.title,
                "channel_title": video.channel_title,
                "published_at": video.published_at,
                "description": video.description,
                "thumbnail_url": video.thumbnail_url,
                "video_url": video.video_url,
            }
            for video in videos
        ],
    }

    (output_dir / "results.json").write_text(
        json.dumps(json_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "index.html").write_text(
        render_html(query=query, videos=videos, generated_at=generated_at),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.api_key:
        raise SystemExit("YOUTUBE_API_KEY is required")

    generated_at = datetime.now(timezone.utc).isoformat()
    videos = fetch_recent_videos(
        api_key=args.api_key,
        query=args.query,
        limit=args.limit,
    )
    write_outputs(
        output_dir=Path(args.output_dir),
        query=args.query,
        videos=videos,
        generated_at=generated_at,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
