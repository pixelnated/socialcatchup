import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from youtube_daily_update import VideoResult, fetch_recent_videos, write_outputs


class FetchRecentVideosTests(unittest.TestCase):
    def test_fetch_recent_videos_paginates_and_sorts_newest_first(self) -> None:
        payloads = [
            {
                "nextPageToken": "page-2",
                "items": [
                    {
                        "id": {"videoId": "older"},
                        "snippet": {
                            "title": "Older upload",
                            "channelTitle": "Channel A",
                            "publishedAt": "2024-01-01T08:00:00Z",
                            "description": "Old",
                            "thumbnails": {"default": {"url": "https://example.com/older.jpg"}},
                        },
                    }
                ],
            },
            {
                "items": [
                    {
                        "id": {"videoId": "newer"},
                        "snippet": {
                            "title": "Newer upload",
                            "channelTitle": "Channel B",
                            "publishedAt": "2024-01-02T08:00:00Z",
                            "description": "New",
                            "thumbnails": {"medium": {"url": "https://example.com/newer.jpg"}},
                        },
                    }
                ],
            },
        ]
        requested_urls = []

        def fake_fetcher(url: str) -> dict:
            requested_urls.append(url)
            return payloads[len(requested_urls) - 1]

        videos = fetch_recent_videos(
            api_key="api-key",
            query="dinosaur jr",
            limit=2,
            fetcher=fake_fetcher,
        )

        self.assertEqual(["newer", "older"], [video.video_id for video in videos])
        self.assertEqual(2, len(requested_urls))

        first_request = parse_qs(urlparse(requested_urls[0]).query)
        second_request = parse_qs(urlparse(requested_urls[1]).query)
        self.assertEqual(["date"], first_request["order"])
        self.assertEqual(["dinosaur jr"], first_request["q"])
        self.assertEqual(["page-2"], second_request["pageToken"])


class WriteOutputsTests(unittest.TestCase):
    def test_write_outputs_creates_json_and_html(self) -> None:
        videos = [
            VideoResult(
                video_id="abc123",
                title="A title",
                channel_title="A channel",
                published_at="2024-01-02T08:00:00Z",
                description="A description",
                thumbnail_url="https://example.com/thumb.jpg",
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            write_outputs(
                output_dir=output_dir,
                query="dinosaur jr",
                videos=videos,
                generated_at="2024-01-03T00:00:00+00:00",
            )

            payload = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
            html = (output_dir / "index.html").read_text(encoding="utf-8")

        self.assertEqual("dinosaur jr", payload["query"])
        self.assertEqual("abc123", payload["videos"][0]["video_id"])
        self.assertIn("Latest YouTube results", html)
        self.assertIn("https://www.youtube.com/watch?v=abc123", html)


if __name__ == "__main__":
    unittest.main()
