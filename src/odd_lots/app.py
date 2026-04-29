from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable


FEED_URL = (
    "https://omnycontent.com/d/playlist/"
    "e73c998e-6e60-432f-8610-ae210140c5b1/"
    "8A94442E-5A74-4FA2-8B8D-AE27003A8D6B/"
    "982F5071-765C-403D-969D-AE27003A8D83/podcast.rss"
)
PODCAST_NAME = "Odd Lots"
USER_AGENT = "odd-lots-transcript/0.1"
RESEND_EMAIL_URL = "https://api.resend.com/emails"
DEFAULT_STATE_PATH = Path("sent_episodes.json")
PODCAST_NAMESPACE = "https://podcastindex.org/namespace/1.0"
REQUIRED_ENV_VARS = (
    "RESEND_API_KEY",
    "RESEND_FROM_EMAIL",
    "RESEND_TO_EMAIL",
)
DESCRIPTION_STOP_MARKERS = (
    "Read more:",
    "More:",
    "Only Bloomberg",
    "Only Bloomberg.com",
    "Subscribe",
    "Join the conversation",
    "See omnystudio.com/listener",
)


@dataclass(frozen=True)
class Config:
    resend_api_key: str
    resend_from_email: str
    resend_to_email: str

    @classmethod
    def from_env(cls) -> "Config":
        missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required environment variables: {joined}")

        return cls(
            resend_api_key=os.environ["RESEND_API_KEY"],
            resend_from_email=os.environ["RESEND_FROM_EMAIL"],
            resend_to_email=os.environ["RESEND_TO_EMAIL"],
        )


@dataclass(frozen=True)
class Episode:
    guid: str
    title: str
    publication_datetime: datetime
    description: str
    episode_url: str
    transcript_url: str | None

    @property
    def publication_date(self) -> date:
        return self.publication_datetime.date()


@dataclass(frozen=True)
class RunResult:
    status: str
    episode_guid: str | None = None
    email_ids: tuple[str, ...] = ()
    state_changed: bool = False


TranscriptFetcher = Callable[[Episode], str]
EmailSender = Callable[[Episode, str, Config], str]


class _HTMLToTextParser(HTMLParser):
    block_tags = {"address", "article", "br", "div", "li", "p", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._link_stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in self.block_tags:
            self._parts.append("\n")
        if normalized == "a":
            href = ""
            for name, value in attrs:
                if name.lower() == "href" and value:
                    href = value.strip()
                    break
            self._link_stack.append(href or None)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized == "a":
            href = self._link_stack.pop() if self._link_stack else None
            if href:
                self._parts.append(f" ({href})")
        if normalized in self.block_tags:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def fetch_feed_xml() -> str:
    request = urllib.request.Request(
        FEED_URL,
        headers={"User-Agent": USER_AGENT},
    )
    return _open_request(request, timeout=30)


def parse_feed(xml_text: str) -> list[Episode]:
    root = ET.fromstring(xml_text)
    items = root.findall("./channel/item")
    episodes: list[Episode] = []

    for item in items:
        link = clean_inline_text(item.findtext("link") or "")
        if "/shows/odd-lots/" not in link:
            continue

        title = _required_text(item, "title")
        transcript_url = _text_transcript_url(item)
        pub_date = _parse_pub_date(_required_text(item, "pubDate"))
        guid = _guid_text(item)
        description = description_to_plain_text(item.findtext("description") or "")

        episodes.append(
            Episode(
                guid=guid,
                title=clean_inline_text(title),
                publication_datetime=pub_date,
                description=description,
                episode_url=link,
                transcript_url=transcript_url,
            )
        )

    return sorted(episodes, key=lambda episode: episode.publication_datetime, reverse=True)


def _required_text(item: ET.Element, tag: str) -> str:
    value = item.findtext(tag)
    if value is None or not value.strip():
        raise ValueError(f"RSS item is missing required tag: {tag}")
    return value.strip()


def _guid_text(item: ET.Element) -> str:
    guid = item.findtext("guid")
    if guid and guid.strip():
        return guid.strip()
    raise ValueError("RSS item is missing guid")


def _text_transcript_url(item: ET.Element) -> str | None:
    transcript_tag = f"{{{PODCAST_NAMESPACE}}}transcript"
    for transcript in item.findall(transcript_tag):
        transcript_type = transcript.attrib.get("type", "").strip().lower()
        if transcript_type != "text/plain":
            continue
        url = transcript.attrib.get("url", "").strip()
        if url:
            return html.unescape(url)

    return None


def _parse_pub_date(value: str) -> datetime:
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def description_to_plain_text(description: str) -> str:
    parser = _HTMLToTextParser()
    parser.feed(html.unescape(description))
    parser.close()
    text = clean_multiline_text(parser.get_text(), preserve_blank_lines=False)
    return summary_only_description(text)


def summary_only_description(value: str) -> str:
    for marker in DESCRIPTION_STOP_MARKERS:
        index = value.find(marker)
        if index != -1:
            value = value[:index]
    return clean_multiline_text(value, preserve_blank_lines=False)


def clean_inline_text(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def clean_multiline_text(value: str, *, preserve_blank_lines: bool = True) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    lines = [line.strip() for line in value.split("\n")]
    if not preserve_blank_lines:
        lines = [line for line in lines if line]
    value = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict:
    if not path.exists():
        return {"version": 1, "initialized": False, "episodes": {}}

    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state.get("episodes"), dict):
        raise ValueError("State file must contain an episodes object")
    state.setdefault("version", 1)
    state.setdefault("initialized", False)
    return state


def save_state(state: dict, path: Path = DEFAULT_STATE_PATH) -> None:
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fetch_transcript(episode: Episode) -> str:
    if not episode.transcript_url:
        raise RuntimeError(f"Episode is missing text/plain transcript URL: {episode.title}")

    request = urllib.request.Request(
        episode.transcript_url,
        headers={"User-Agent": USER_AGENT},
    )
    transcript = _open_request(request, timeout=60)
    transcript = clean_multiline_text(transcript)
    if not transcript:
        raise RuntimeError(f"Transcript was empty for: {episode.title}")
    return transcript


def send_transcript_email(episode: Episode, transcript: str, config: Config) -> str:
    payload = {
        "from": config.resend_from_email,
        "to": parse_recipient_list(config.resend_to_email),
        "subject": build_email_subject(episode),
        "text": build_email_body(episode, transcript),
    }
    request = urllib.request.Request(
        RESEND_EMAIL_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Idempotency-Key": f"odd-lots-transcript/{episode.guid}",
        },
        method="POST",
    )
    response_text = _open_request(request, timeout=60)
    response = json.loads(response_text)
    email_id = response.get("id")
    if not email_id:
        raise RuntimeError(f"Resend response did not include an email id: {response_text}")
    return email_id


def parse_recipient_list(value: str) -> str | list[str]:
    recipients = [part.strip() for part in value.split(",") if part.strip()]
    if not recipients:
        raise RuntimeError("RESEND_TO_EMAIL must contain at least one recipient")
    if len(recipients) == 1:
        return recipients[0]
    return recipients


def build_email_subject(episode: Episode) -> str:
    return f"{episode.publication_date:%Y-%m-%d} {PODCAST_NAME} Transcript: {episode.title}"


def build_email_body(episode: Episode, transcript: str) -> str:
    return "\n\n".join(
        [
            f"Title: {episode.title}",
            "Description:",
            episode.description,
            "Transcript:",
            clean_multiline_text(transcript),
        ]
    ).rstrip() + "\n"


def _open_request(request: urllib.request.Request, timeout: int) -> str:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {request.full_url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed for {request.full_url}: {exc.reason}") from exc


def run(
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    dry_run: bool = False,
    feed_xml: str | None = None,
    config: Config | None = None,
    transcript_fetcher: TranscriptFetcher = fetch_transcript,
    email_sender: EmailSender = send_transcript_email,
    now: Callable[[], datetime] | None = None,
) -> RunResult:
    now = now or (lambda: datetime.now(UTC))
    state = load_state(state_path)
    episodes = parse_feed(feed_xml if feed_xml is not None else fetch_feed_xml())
    if not episodes:
        raise RuntimeError("No eligible Odd Lots episodes found in RSS feed")

    unsent = [episode for episode in episodes if episode.guid not in state["episodes"]]
    if not unsent:
        print(f"No new episode. Latest eligible already recorded: {episodes[0].title}")
        return RunResult(status="noop", episode_guid=episodes[0].guid, state_changed=False)

    was_initialized = bool(state.get("initialized"))
    episodes_to_send = [unsent[0]] if not was_initialized else list(reversed(unsent))

    if dry_run:
        print("Dry run: would process eligible Odd Lots episodes:")
        for episode in episodes_to_send:
            transcript_detail = episode.transcript_url or "missing text/plain transcript URL"
            print(
                f"- {episode.publication_date:%Y-%m-%d} {episode.title} "
                f"({transcript_detail})"
            )
        return RunResult(
            status="dry-run",
            episode_guid=episodes_to_send[-1].guid,
            state_changed=False,
        )

    load_dotenv()
    config = config or Config.from_env()
    updated_state = copy.deepcopy(state)
    updated_state["version"] = 1
    updated_state["initialized"] = True

    if not was_initialized:
        for episode in episodes[1:]:
            updated_state["episodes"].setdefault(
                episode.guid,
                _episode_state_entry(
                    episode,
                    status="skipped_initial_backfill",
                    timestamp_key="skipped_at",
                    timestamp=now(),
                ),
            )

    email_ids: list[str] = []
    state_changed = False
    for episode in episodes_to_send:
        if not episode.transcript_url:
            raise RuntimeError(f"Episode is missing text/plain transcript URL: {episode.title}")
        transcript = transcript_fetcher(episode)
        email_id = email_sender(episode, transcript, config)
        email_ids.append(email_id)
        updated_state["episodes"][episode.guid] = _episode_state_entry(
            episode,
            status="sent",
            timestamp_key="sent_at",
            timestamp=now(),
        )
        save_state(updated_state, state_path)
        state_changed = True
        print(f"Sent transcript email for: {episode.title}")

    if not state_changed and updated_state != state:
        save_state(updated_state, state_path)
        state_changed = True

    return RunResult(
        status="sent",
        episode_guid=episodes_to_send[-1].guid,
        email_ids=tuple(email_ids),
        state_changed=state_changed,
    )


def _episode_state_entry(
    episode: Episode,
    *,
    status: str,
    timestamp_key: str,
    timestamp: datetime,
) -> dict[str, str]:
    return {
        "status": status,
        "title": episode.title,
        "publication_date": f"{episode.publication_date:%Y-%m-%d}",
        "guid": episode.guid,
        "episode_url": episode.episode_url,
        "transcript_url": episode.transcript_url or "",
        timestamp_key: _utc_isoformat(timestamp),
    }


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Odd Lots transcript emailer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Check the feed and send unsent transcripts")
    run_parser.add_argument("--dry-run", action="store_true", help="Do not fetch transcripts, email, or update state")
    run_parser.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help="Path to sent episode state JSON",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "run":
        try:
            result = run(state_path=Path(args.state_path), dry_run=args.dry_run)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0 if result.status in {"sent", "noop", "dry-run"} else 1

    parser.error(f"Unknown command: {args.command}")
    return 2
