#!/usr/bin/env python3
"""Build a subscribable RKLB calendar from official Rocket Lab IR events.

The script:
1. Fetches the official Rocket Lab investor events page.
2. Reads only the "Upcoming Events" section.
3. Keeps the last successful result if the page is temporarily unavailable.
4. Merges optional manually confirmed events.
5. Generates docs/RKLB_official_events.ics and docs/index.html.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from icalendar import Alarm, Calendar, Event

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

IR_EVENTS_URL = "https://investors.rocketlabcorp.com/events-presentations/events"
CALENDAR_URL = (
    "https://infiniteseymour.github.io/"
    "rklb-calendar/RKLB_official_events.ics"
)
WEBCAL_URL = (
    "webcal://infiniteseymour.github.io/"
    "rklb-calendar/RKLB_official_events.ics"
)

CACHE_FILE = DATA_DIR / "cache_ir_events.json"
MANUAL_FILE = DATA_DIR / "manual_events.json"
ICS_FILE = DOCS_DIR / "RKLB_official_events.ics"
INDEX_FILE = DOCS_DIR / "index.html"

USER_AGENT = (
    "RKLB-Official-Events-Calendar/1.0 "
    "(public GitHub project: infiniteseymour/rklb-calendar)"
)

MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
DATE_RE = re.compile(
    rf"\b(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}"
    r"(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)"
    r"(?:\s+(?:EST|EDT|CST|CDT|MST|MDT|PST|PDT|UTC))?)?",
    re.IGNORECASE,
)
TZINFOS = {
    "EST": -5 * 3600,
    "EDT": -4 * 3600,
    "CST": -6 * 3600,
    "CDT": -5 * 3600,
    "MST": -7 * 3600,
    "MDT": -6 * 3600,
    "PST": -8 * 3600,
    "PDT": -7 * 3600,
    "UTC": 0,
}
REJECT_TITLE_PARTS = {
    "upcoming events",
    "past events",
    "add to outlook",
    "add to google calendar",
    "google calendar",
    "outlook",
    "click here for webcast",
    "webcast",
    "presentation",
    "download",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read {path}: {exc}", file=sys.stderr)
        return default


def atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(content, bytes):
        tmp.write_bytes(content)
    else:
        tmp.write_text(content, encoding="utf-8", newline="\n")
    tmp.replace(path)


def fetch_ir_html() -> str:
    response = requests.get(
        IR_EVENTS_URL,
        timeout=30,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    response.raise_for_status()
    return response.text


def title_from_block(block: Tag, date_text: str) -> str | None:
    candidates = block.find_all(["h2", "h3", "h4", "h5", "h6", "strong", "a"])
    for candidate in candidates:
        text = clean_text(candidate.get_text(" ", strip=True))
        lowered = text.lower()

        if not text or text == date_text or DATE_RE.fullmatch(text):
            continue
        if any(part in lowered for part in REJECT_TITLE_PARTS):
            continue
        if len(text) < 8 or len(text) > 220:
            continue
        return text

    # Fallback: remove the date and utility labels from the block text.
    text = clean_text(block.get_text(" ", strip=True))
    text = clean_text(text.replace(date_text, ""))
    for phrase in REJECT_TITLE_PARTS:
        text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
    text = clean_text(text)
    return text[:220] if len(text) >= 8 else None


def best_event_block(date_tag: Tag, date_text: str) -> Tag:
    current: Tag = date_tag
    best = date_tag

    for _ in range(7):
        if not isinstance(current, Tag):
            break

        text = clean_text(current.get_text(" ", strip=True))
        if len(text) > 1200:
            break

        if title_from_block(current, date_text):
            best = current
            break

        parent = current.parent
        if not isinstance(parent, Tag):
            break
        current = parent

    return best


def parse_datetime(date_text: str) -> tuple[datetime | date, bool]:
    has_time = bool(re.search(r"\d{1,2}:\d{2}\s*(?:AM|PM)", date_text, re.I))
    parsed = date_parser.parse(date_text, tzinfos=TZINFOS, fuzzy=False)

    if not has_time:
        return parsed.date(), True

    if parsed.tzinfo is None:
        # The official page normally includes a timezone. UTC is used only as
        # a defensive fallback so the generated calendar remains valid.
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc), False


def source_link_from_block(block: Tag, title: str) -> str:
    links = block.find_all("a", href=True)

    for link in links:
        if clean_text(link.get_text(" ", strip=True)) == title:
            return urljoin(IR_EVENTS_URL, link["href"])

    for link in links:
        label = clean_text(link.get_text(" ", strip=True)).lower()
        if "webcast" in label or "event" in label:
            return urljoin(IR_EVENTS_URL, link["href"])

    return IR_EVENTS_URL


def parse_upcoming_ir_events(page_html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page_html, "html.parser")
    tags = list(soup.find_all(True))

    upcoming = next(
        (
            tag
            for tag in tags
            if tag.name in {"h1", "h2", "h3", "h4", "h5"}
            and "upcoming events" in clean_text(tag.get_text(" ", strip=True)).lower()
        ),
        None,
    )
    if upcoming is None:
        raise ValueError("Could not locate the Upcoming Events heading.")

    start_index = tags.index(upcoming)
    end_index = len(tags)

    for index in range(start_index + 1, len(tags)):
        tag = tags[index]
        if (
            tag.name in {"h1", "h2", "h3", "h4", "h5"}
            and "past events" in clean_text(tag.get_text(" ", strip=True)).lower()
        ):
            end_index = index
            break

    section_tags = tags[start_index + 1 : end_index]
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for tag in section_tags:
        text = clean_text(tag.get_text(" ", strip=True))
        matches = list(DATE_RE.finditer(text))
        if not matches:
            continue

        # Prefer the smallest element containing the date to avoid repeatedly
        # parsing the same event through each ancestor container.
        direct_child_has_date = any(
            isinstance(child, Tag)
            and DATE_RE.search(clean_text(child.get_text(" ", strip=True)))
            for child in tag.find_all(recursive=False)
        )
        if direct_child_has_date:
            continue

        for match in matches:
            date_text = clean_text(match.group(0))
            block = best_event_block(tag, date_text)
            title = title_from_block(block, date_text)
            if not title:
                continue

            start, all_day = parse_datetime(date_text)
            if all_day:
                end: datetime | date = start + timedelta(days=1)
                start_key = start.isoformat()
                end_value = end.isoformat()
            else:
                end = start + timedelta(hours=1)
                start_key = start.isoformat().replace("+00:00", "Z")
                end_value = end.isoformat().replace("+00:00", "Z")

            key = (title.lower(), start_key)
            if key in seen:
                continue
            seen.add(key)

            events.append(
                {
                    "title": title,
                    "start": start_key,
                    "end": end_value,
                    "all_day": all_day,
                    "source_url": source_link_from_block(block, title),
                    "category": "Rocket Lab Investor Relations",
                    "status": "CONFIRMED",
                }
            )

    return sorted(events, key=lambda item: item["start"])


def refresh_ir_cache() -> tuple[list[dict[str, Any]], str]:
    cached = load_json(CACHE_FILE, {"events": []})
    cached_events = cached.get("events", [])

    try:
        page_html = fetch_ir_html()
        fetched_events = parse_upcoming_ir_events(page_html)

        if not fetched_events:
            raise ValueError(
                "The official page returned no upcoming events; preserving cache."
            )

        payload = {
            "source": IR_EVENTS_URL,
            "last_successful_fetch_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "events": fetched_events,
        }
        atomic_write(
            CACHE_FILE,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        return fetched_events, "Official page fetched successfully"
    except Exception as exc:  # Network/HTML changes should not erase the feed.
        print(f"Warning: {exc}", file=sys.stderr)
        return cached_events, f"Using last successful cache: {exc}"


def parse_iso_value(value: str, all_day: bool) -> datetime | date:
    if all_day:
        return date.fromisoformat(value[:10])

    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_future_event(item: dict[str, Any]) -> bool:
    all_day = bool(item.get("all_day", False))
    start = parse_iso_value(item["start"], all_day)

    if all_day:
        return start >= datetime.now(timezone.utc).date()
    return start >= datetime.now(timezone.utc) - timedelta(hours=2)


def event_identity(item: dict[str, Any]) -> tuple[str, str]:
    return clean_text(item["title"]).lower(), item["start"]


def merge_events(*event_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for group in event_groups:
        for item in group:
            if not is_future_event(item):
                continue
            merged[event_identity(item)] = item
    return sorted(merged.values(), key=lambda item: item["start"])


def make_uid(item: dict[str, Any]) -> str:
    raw = f'{item["title"]}|{item["start"]}|{item.get("source_url", "")}'
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{digest}@rklb-calendar.github.io"


def build_ics(events: list[dict[str, Any]]) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//infiniteseymour//RKLB Official Events//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", "RKLB Official Events")
    calendar.add(
        "x-wr-caldesc",
        "Officially confirmed future Rocket Lab investor events.",
    )
    calendar.add("refresh-interval", timedelta(hours=12))
    calendar.add("x-published-ttl", timedelta(hours=12))

    stamp = datetime.now(timezone.utc).replace(microsecond=0)

    for item in events:
        event = Event()
        all_day = bool(item.get("all_day", False))
        start = parse_iso_value(item["start"], all_day)
        end = parse_iso_value(item["end"], all_day)

        event.add("uid", make_uid(item))
        event.add("dtstamp", stamp)
        event.add("summary", f'RKLB | {item["title"]}')
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("status", item.get("status", "CONFIRMED"))
        event.add("transp", "TRANSPARENT")
        event.add("categories", item.get("category", "Rocket Lab"))
        event.add("url", item.get("source_url", IR_EVENTS_URL))
        event.add(
            "description",
            "Official source: " + item.get("source_url", IR_EVENTS_URL),
        )

        one_day = Alarm()
        one_day.add("action", "DISPLAY")
        one_day.add("description", f'RKLB event tomorrow: {item["title"]}')
        one_day.add("trigger", timedelta(days=-1))
        event.add_component(one_day)

        if not all_day:
            thirty_minutes = Alarm()
            thirty_minutes.add("action", "DISPLAY")
            thirty_minutes.add(
                "description",
                f'RKLB event in 30 minutes: {item["title"]}',
            )
            thirty_minutes.add("trigger", timedelta(minutes=-30))
            event.add_component(thirty_minutes)

        calendar.add_component(event)

    return calendar.to_ical()


def format_event_time(item: dict[str, Any]) -> str:
    all_day = bool(item.get("all_day", False))
    start = parse_iso_value(item["start"], all_day)

    if all_day:
        return start.strftime("%B %-d, %Y")

    # Display in both U.S. Eastern and Pacific time without extra dependencies.
    from zoneinfo import ZoneInfo

    eastern = start.astimezone(ZoneInfo("America/New_York"))
    pacific = start.astimezone(ZoneInfo("America/Los_Angeles"))
    return (
        eastern.strftime("%B %-d, %Y · %-I:%M %p %Z")
        + " / "
        + pacific.strftime("%-I:%M %p %Z")
    )


def build_index(events: list[dict[str, Any]], status_message: str) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if events:
        event_cards = "\n".join(
            f"""
            <article class="event">
              <div class="date">{html.escape(format_event_time(item))}</div>
              <h2>{html.escape(item["title"])}</h2>
              <p>{html.escape(item.get("category", "Rocket Lab"))}</p>
              <a href="{html.escape(item.get("source_url", IR_EVENTS_URL))}">
                Official source
              </a>
            </article>
            """
            for item in events
        )
    else:
        event_cards = """
        <article class="event">
          <h2>No officially confirmed future events found.</h2>
          <p>The feed will update when Rocket Lab publishes a confirmed date.</p>
        </article>
        """

    return dedent(
        f"""\
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <meta name="description"
                content="Automatically updated official Rocket Lab event calendar">
          <title>RKLB Official Events Calendar</title>
          <style>
            :root {{
              color-scheme: light dark;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system,
                           BlinkMacSystemFont, "Segoe UI", sans-serif;
            }}
            body {{
              max-width: 820px;
              margin: 0 auto;
              padding: 48px 20px 80px;
              line-height: 1.55;
            }}
            header {{ margin-bottom: 32px; }}
            h1 {{ font-size: clamp(2rem, 6vw, 4rem); line-height: 1.02; }}
            .subtitle {{ font-size: 1.1rem; opacity: .78; max-width: 650px; }}
            .actions {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 28px 0; }}
            .button {{
              display: inline-block;
              padding: 12px 18px;
              border-radius: 999px;
              border: 1px solid currentColor;
              text-decoration: none;
              font-weight: 650;
            }}
            .primary {{
              background: CanvasText;
              color: Canvas;
            }}
            .event {{
              border: 1px solid color-mix(in srgb, CanvasText 22%, transparent);
              border-radius: 18px;
              padding: 22px;
              margin: 16px 0;
            }}
            .event h2 {{ margin: 7px 0 4px; font-size: 1.25rem; }}
            .date {{ font-weight: 700; }}
            .meta {{
              margin-top: 36px;
              font-size: .9rem;
              opacity: .7;
            }}
            code {{ overflow-wrap: anywhere; }}
          </style>
        </head>
        <body>
          <header>
            <p>ROCKET LAB · NASDAQ: RKLB</p>
            <h1>Official Events Calendar</h1>
            <p class="subtitle">
              A subscribable calendar containing only future dates confirmed
              by official Rocket Lab sources.
            </p>
            <div class="actions">
              <a class="button primary" href="{WEBCAL_URL}">
                Subscribe in Apple Calendar
              </a>
              <a class="button" href="RKLB_official_events.ics">
                Download .ics
              </a>
            </div>
          </header>

          <main>
            <h2>Upcoming confirmed events</h2>
            {event_cards}
          </main>

          <footer class="meta">
            <p>Generated: {html.escape(generated)}</p>
            <p>Update status: {html.escape(status_message)}</p>
            <p>
              Subscription URL:<br>
              <code>{WEBCAL_URL}</code>
            </p>
            <p>
              Unofficial community utility. Verify time-sensitive details
              against the linked official source.
            </p>
          </footer>
        </body>
        </html>
        """
    )


def main() -> int:
    ir_events, status_message = refresh_ir_cache()
    manual_payload = load_json(MANUAL_FILE, {"events": []})
    manual_events = manual_payload.get("events", [])
    events = merge_events(ir_events, manual_events)

    atomic_write(ICS_FILE, build_ics(events))
    atomic_write(INDEX_FILE, build_index(events, status_message))
    atomic_write(DOCS_DIR / ".nojekyll", "")

    print(f"Generated {ICS_FILE} with {len(events)} future event(s).")
    print(f"Generated {INDEX_FILE}.")
    print(status_message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
