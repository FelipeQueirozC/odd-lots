from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from odd_lots.app import (
    Config,
    build_email_body,
    build_email_subject,
    fetch_transcript,
    parse_feed,
    parse_recipient_list,
    run,
    send_transcript_email,
)


SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:podcast="https://podcastindex.org/namespace/1.0" version="2.0">
  <channel>
    <title>Odd Lots</title>
    <item>
      <title>Newest Odd Lots Episode</title>
      <description><![CDATA[
        <p>Newest summary with <a href="https://example.com/context">context</a>.</p>
        <p>Read more:<br><a href="https://example.com/article">Article</a></p>
        <p>Subscribe to the Odd Lots Newsletter</p>
        <p>See <a href="https://omnystudio.com/listener">omnystudio.com/listener</a> for privacy information.</p>
      ]]></description>
      <pubDate>Mon, 27 Apr 2026 08:00:00 +0000</pubDate>
      <guid isPermaLink="false">newest-guid</guid>
      <link>https://omny.fm/shows/odd-lots/newest-odd-lots-episode</link>
      <podcast:transcript url="https://example.com/newest.srt" type="application/srt" language="en" />
      <podcast:transcript url="https://example.com/newest.vtt" type="text/vtt" language="en" />
      <podcast:transcript url="https://example.com/newest.txt?format=TextWithTimestamps&amp;t=1" type="text/plain" language="en" />
    </item>
    <item>
      <title>Presenting Another Bloomberg Show</title>
      <description>Promo description.</description>
      <pubDate>Sun, 26 Apr 2026 12:00:00 +0000</pubDate>
      <guid isPermaLink="false">promo-guid</guid>
      <link>https://omny.fm/shows/foundering/promo-episode</link>
      <podcast:transcript url="https://example.com/promo.txt" type="text/plain" language="en" />
    </item>
    <item>
      <title>Older Odd Lots Episode</title>
      <description><![CDATA[
        <p>Older summary paragraph one.</p>
        <p>Older summary paragraph two.</p>
        <p>Only Bloomberg.com subscribers can get the Odd Lots newsletter.</p>
      ]]></description>
      <pubDate>Thu, 23 Apr 2026 08:00:00 +0000</pubDate>
      <guid isPermaLink="false">older-guid</guid>
      <link>https://omny.fm/shows/odd-lots/older-odd-lots-episode</link>
      <podcast:transcript url="https://example.com/older.txt" type="text/plain" language="en-us" />
    </item>
  </channel>
</rss>
"""


MISSING_TRANSCRIPT_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:podcast="https://podcastindex.org/namespace/1.0" version="2.0">
  <channel>
    <item>
      <title>No Plain Text Transcript</title>
      <description>Summary.</description>
      <pubDate>Mon, 27 Apr 2026 08:00:00 +0000</pubDate>
      <guid isPermaLink="false">missing-guid</guid>
      <link>https://omny.fm/shows/odd-lots/no-plain-text-transcript</link>
      <podcast:transcript url="https://example.com/missing.srt" type="application/srt" language="en" />
    </item>
  </channel>
</rss>
"""


class OddLotsTranscriptTests(unittest.TestCase):
    def test_parse_feed_filters_cross_promos_and_selects_text_transcript(self) -> None:
        episodes = parse_feed(SAMPLE_FEED)

        self.assertEqual([episode.guid for episode in episodes], ["newest-guid", "older-guid"])
        self.assertEqual(episodes[0].title, "Newest Odd Lots Episode")
        self.assertEqual(episodes[0].episode_url, "https://omny.fm/shows/odd-lots/newest-odd-lots-episode")
        self.assertEqual(
            episodes[0].transcript_url,
            "https://example.com/newest.txt?format=TextWithTimestamps&t=1",
        )
        self.assertEqual(
            episodes[0].description,
            "Newest summary with context (https://example.com/context).",
        )
        self.assertEqual(
            episodes[1].description,
            "Older summary paragraph one.\nOlder summary paragraph two.",
        )

    def test_parse_feed_keeps_eligible_episode_with_missing_text_transcript(self) -> None:
        episodes = parse_feed(MISSING_TRANSCRIPT_FEED)

        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].guid, "missing-guid")
        self.assertIsNone(episodes[0].transcript_url)

    def test_first_run_sends_latest_and_skips_older_eligible_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sent_episodes.json"
            calls: dict[str, list[str]] = {"fetched": [], "sent": []}

            def fetcher(episode):
                calls["fetched"].append(episode.guid)
                return "00:00:01\nSpeaker 1: Transcript text."

            def sender(episode, transcript, config):
                calls["sent"].append(episode.guid)
                return f"email-{episode.guid}"

            result = run(
                state_path=state_path,
                feed_xml=SAMPLE_FEED,
                config=fake_config(),
                transcript_fetcher=fetcher,
                email_sender=sender,
                now=lambda: datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
            )

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "sent")
            self.assertEqual(calls["fetched"], ["newest-guid"])
            self.assertEqual(calls["sent"], ["newest-guid"])
            self.assertTrue(state["initialized"])
            self.assertEqual(state["episodes"]["newest-guid"]["status"], "sent")
            self.assertNotIn("resend_email_id", state["episodes"]["newest-guid"])
            self.assertEqual(state["episodes"]["older-guid"]["status"], "skipped_initial_backfill")
            self.assertEqual(
                state["episodes"]["newest-guid"]["transcript_url"],
                "https://example.com/newest.txt?format=TextWithTimestamps&t=1",
            )
            self.assertNotIn("promo-guid", state["episodes"])

    def test_noop_when_no_new_eligible_episode_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sent_episodes.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "initialized": True,
                        "episodes": {
                            "newest-guid": {"status": "sent"},
                            "older-guid": {"status": "skipped_initial_backfill"},
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            before = state_path.read_text(encoding="utf-8")

            def fetcher(episode):
                raise AssertionError("Transcript fetcher should not be called")

            def sender(episode, transcript, config):
                raise AssertionError("Email sender should not be called")

            result = run(
                state_path=state_path,
                feed_xml=SAMPLE_FEED,
                config=fake_config(),
                transcript_fetcher=fetcher,
                email_sender=sender,
            )

            self.assertEqual(result.status, "noop")
            self.assertEqual(state_path.read_text(encoding="utf-8"), before)

    def test_initialized_run_sends_all_new_episodes_oldest_to_newest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sent_episodes.json"
            state_path.write_text(
                json.dumps({"version": 1, "initialized": True, "episodes": {}}),
                encoding="utf-8",
            )
            sent: list[str] = []

            def fetcher(episode):
                return f"Transcript for {episode.guid}"

            def sender(episode, transcript, config):
                sent.append(episode.guid)
                return f"email-{episode.guid}"

            result = run(
                state_path=state_path,
                feed_xml=SAMPLE_FEED,
                config=fake_config(),
                transcript_fetcher=fetcher,
                email_sender=sender,
                now=lambda: datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
            )

            self.assertEqual(result.status, "sent")
            self.assertEqual(sent, ["older-guid", "newest-guid"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["episodes"]["older-guid"]["status"], "sent")
            self.assertEqual(state["episodes"]["newest-guid"]["status"], "sent")

    def test_partial_backlog_success_is_persisted_before_later_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sent_episodes.json"
            state_path.write_text(
                json.dumps({"version": 1, "initialized": True, "episodes": {}}),
                encoding="utf-8",
            )

            def fetcher(episode):
                if episode.guid == "newest-guid":
                    raise RuntimeError("Transcript unavailable")
                return "Older transcript."

            def sender(episode, transcript, config):
                return f"email-{episode.guid}"

            with self.assertRaisesRegex(RuntimeError, "Transcript unavailable"):
                run(
                    state_path=state_path,
                    feed_xml=SAMPLE_FEED,
                    config=fake_config(),
                    transcript_fetcher=fetcher,
                    email_sender=sender,
                    now=lambda: datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
                )

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["episodes"]["older-guid"]["status"], "sent")
            self.assertNotIn("newest-guid", state["episodes"])

    def test_transcript_failure_before_send_does_not_update_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sent_episodes.json"

            def fetcher(episode):
                raise RuntimeError("Transcript failed")

            def sender(episode, transcript, config):
                raise AssertionError("Email sender should not be called")

            with self.assertRaisesRegex(RuntimeError, "Transcript failed"):
                run(
                    state_path=state_path,
                    feed_xml=SAMPLE_FEED,
                    config=fake_config(),
                    transcript_fetcher=fetcher,
                    email_sender=sender,
                )

            self.assertFalse(state_path.exists())

    def test_missing_transcript_url_fails_when_episode_would_be_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sent_episodes.json"

            def fetcher(episode):
                raise AssertionError("Transcript fetcher should not be called")

            def sender(episode, transcript, config):
                raise AssertionError("Email sender should not be called")

            with self.assertRaisesRegex(RuntimeError, "missing text/plain transcript URL"):
                run(
                    state_path=state_path,
                    feed_xml=MISSING_TRANSCRIPT_FEED,
                    config=fake_config(),
                    transcript_fetcher=fetcher,
                    email_sender=sender,
                )

            self.assertFalse(state_path.exists())

    def test_dry_run_reports_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "sent_episodes.json"

            def fetcher(episode):
                raise AssertionError("Transcript fetcher should not be called")

            def sender(episode, transcript, config):
                raise AssertionError("Email sender should not be called")

            result = run(
                state_path=state_path,
                feed_xml=SAMPLE_FEED,
                dry_run=True,
                config=fake_config(),
                transcript_fetcher=fetcher,
                email_sender=sender,
            )

            self.assertEqual(result.status, "dry-run")
            self.assertFalse(state_path.exists())

    def test_email_content_and_subject(self) -> None:
        episode = parse_feed(SAMPLE_FEED)[0]
        subject = build_email_subject(episode)
        body = build_email_body(episode, "00:00:01\nSpeaker 1: Line one.\n\n00:00:03\nSpeaker 2: Line two.")

        self.assertEqual(
            subject,
            "2026-04-27 Odd Lots Transcript: Newest Odd Lots Episode",
        )
        self.assertIn("Title: Newest Odd Lots Episode", body)
        self.assertIn("Description:\n\nNewest summary with context", body)
        self.assertIn("Transcript:\n\n00:00:01\nSpeaker 1: Line one.", body)

    def test_resend_payload_has_plain_text_body_and_no_attachments(self) -> None:
        episode = parse_feed(SAMPLE_FEED)[0]
        with patch("odd_lots.app._open_request", return_value='{"id":"email-id"}') as opener:
            email_id = send_transcript_email(episode, "Transcript text.", fake_config())

        request = opener.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(email_id, "email-id")
        self.assertEqual(request.get_header("User-agent"), "odd-lots-transcript/0.1")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertIn("text", payload)
        self.assertNotIn("html", payload)
        self.assertNotIn("attachments", payload)
        self.assertIn("Transcript:\n\nTranscript text.", payload["text"])

    def test_multiple_recipients_still_work(self) -> None:
        self.assertEqual(
            parse_recipient_list("one@example.com, two@example.com"),
            ["one@example.com", "two@example.com"],
        )

    def test_fetch_transcript_cleans_nonempty_response(self) -> None:
        episode = parse_feed(SAMPLE_FEED)[0]
        with patch("odd_lots.app._open_request", return_value="Line one.\n\n\nLine two."):
            transcript = fetch_transcript(episode)

        self.assertEqual(transcript, "Line one.\n\nLine two.")

    def test_fetch_transcript_raises_on_empty_response(self) -> None:
        episode = parse_feed(SAMPLE_FEED)[0]
        with patch("odd_lots.app._open_request", return_value="   "):
            with self.assertRaisesRegex(RuntimeError, "Transcript was empty"):
                fetch_transcript(episode)


def fake_config() -> Config:
    return Config(
        resend_api_key="resend-key",
        resend_from_email="from@example.com",
        resend_to_email="to@example.com",
    )


if __name__ == "__main__":
    unittest.main()
