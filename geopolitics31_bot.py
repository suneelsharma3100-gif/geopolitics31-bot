#!/usr/bin/env python3
"""
geopolitics31_bot.py — RSS → Telegram bot for world news & geopolitics alerts.

Configures:
  - RSS feed sources (Reuters, AP, Al Jazeera, BBC World, ...)
  - Keyword filters (case-insensitive, title + description)
  - Poll interval (default 30 minutes)
  - Target Telegram channel

Reads config from geopolitics31_config.json (next to this script) or falls back
to built-in defaults. Persists seen article URLs in seen_urls.json.

Usage:
  1. Edit geopolitics31_config.json (or create it) with your bot token and
     channel ID.
  2. Run:  python geopolitics31_bot.py
  3. Stop with Ctrl+C. The seen-URL store is saved on exit too.

Environment variables (override config file):
  GEOPOLITICS31_BOT_TOKEN  — Telegram bot token from @BotFather
  GEOPOLITICS31_CHANNEL_ID — numeric channel ID (e.g. -1001234567890)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import defusedxml.ElementTree as ET  # type: ignore[import-untyped]
import feedparser  # type: ignore[import-untyped]
import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("geopolitics31")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "bot_token": "",
    "channel_id": "",
    "feeds": [
        {
            "name": "Reuters World News",
            "url": "http://today.reuters.com/rss/worldNews",
        },
        {
            "name": "AP World Stories",
            "url": "http://hosted2.ap.org/atom/APDEFAULT/cae69a7523db45408eeb2b3a98c0c9c5",
        },
        {
            "name": "Al Jazeera",
            "url": "https://feeds.bbci.co.uk/news/world/rss.xml",  # BBC fallback; Al Jazeera RSS is harder to find directly; see README
        },
        {
            "name": "BBC World News",
            "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        },
    ],
    "keywords": [
        "israel",
        "ukraine",
        "election",
        "ceasefire",
        "conflict",
        "war",
        "attack",
        "sanction",
        "treaty",
        "summit",
        "nato",
        "iran",
        "gaza",
        "putin",
        "trump",
        "biden",
    ],
    "skip_keywords": [],
    "poll_interval_seconds": 1800,  # 30 minutes
    "max_articles_per_run": 20,     # cap per feed per run to avoid flooding
    "telegram_timeout_seconds": 30,
    "user_agent": "Geopolitics31Bot/1.0 (RSS-to-Telegram; contact: you@email.com)",
}

# Fallback channel label when config is missing (so the user knows what's wrong)
MISSING_CHANNEL_LABEL = "<fill channel_id in config>"


def load_config() -> dict:
    """Load config from JSON file next to this script, then overlay env vars."""
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "geopolitics31_config.json"

    cfg: dict = {}
    if config_path.exists():
        try:
            raw = config_path.read_text(encoding="utf-8")
            cfg = json.loads(raw)
            log.info("Loaded config from %s", config_path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Config file unreadable (%s), using defaults / env vars", exc)

    # Overlay environment variables (highest priority)
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("GEOPOLITICS31_BOT_TOKEN")
    env_channel = os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("GEOPOLITICS31_CHANNEL_ID")
    if env_token:
        cfg["bot_token"] = env_token
    if env_channel:
        cfg["channel_id"] = env_channel

    # Merge with defaults for any missing keys (shallow merge of known keys)
    for key, default_val in DEFAULT_CONFIG.items():
        if key not in cfg or cfg[key] is None:
            cfg[key] = default_val
        elif isinstance(default_val, list) and isinstance(cfg[key], list):
            # For feeds/keywords, default list is replaced by user list; that's
            # intentional. Keep user's list.
            pass

    return cfg


def validate_config(cfg: dict) -> list[str]:
    """Return a list of human-readable problems (empty list = OK)."""
    problems: list[str] = []
    if not cfg.get("bot_token"):
        problems.append(
            "bot_token is empty. Get one from @BotFather and set it in "
            "geopolitics31_config.json or GEOPOLITICS31_BOT_TOKEN env var."
        )
    if not cfg.get("channel_id"):
        problems.append(
            "channel_id is empty. Create/use a Telegram channel, add the bot "
            "as admin, then get its numeric ID (forward a message to @RawDataBot) "
            "and set it in the config or GEOPOLITICS31_CHANNEL_ID env var."
        )
    if cfg.get("poll_interval_seconds", 0) < 60:
        problems.append(
            "poll_interval_seconds is below 60 — Telegram rate limits may apply. "
            "Set to >= 60."
        )
    return problems


# ---------------------------------------------------------------------------
# Persistent seen-URL store
# ---------------------------------------------------------------------------

SEEN_FILE = Path(__file__).resolve().parent / "seen_urls.json"
# We store a simple set of URLs as a JSON array. Each entry is just the URL
# string. This is fine for a single-machine, single-process bot.


def load_seen_urls() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(data)
        return set()
    except (json.JSONDecodeError, OSError):
        log.warning("Could not parse %s, starting fresh", SEEN_FILE)
        return set()


def save_seen_urls(urls: set[str]) -> None:
    try:
        SEEN_FILE.write_text(
            json.dumps(sorted(urls), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Failed to save seen URLs: %s", exc)


# ---------------------------------------------------------------------------
# Telegram API wrapper
# ---------------------------------------------------------------------------

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def telegram_send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    timeout: int = 30,
    parse_mode: str = "HTML",
) -> dict:
    """Send a text message to a Telegram chat/channel. Returns the API response JSON."""
    url = TELEGRAM_API_BASE.format(token=bot_token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_CONFIG["user_agent"]},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.warning("Telegram API reported error: %s", data.get("description", "unknown"))
        return data
    except requests.RequestException as exc:
        log.error("Telegram API request failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# RSS feed fetching & filtering
# ---------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    """Collapse relative/alternate link forms into one canonical form for dedup."""
    if not url:
        return ""
    url = urllib.parse.urldefrag(url)[0].strip()
    # Unwrap Google News redirect URLs: https://news.google.com/rss/articles/...)
    # or https://news.google.com/news/article?...&url=<real_url>
    # The real article URL is encoded in the 'url' query param or embedded.
    if "news.google.com" in url:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        real = params.get("url", [None])[0]
        if real:
            try:
                real = urllib.parse.unquote(real)
            except Exception:
                pass
            return _normalize_url(real)
        # Some Google News RSS entries encode the URL in the path as an
        # encoded blob. Best-effort: if we can't extract, keep the redirect
        # URL as-is (it still works when clicked).
    return url


def _get_entry_link(entry: feedparser.FeedParserDict) -> str:
    """Pick the best link from a feed entry: link field, then id, then first
    content link, then first link in links list."""
    link = entry.get("link", "")
    if link:
        return _normalize_url(link)
    entry_id = entry.get("id", "")
    if entry_id:
        return _normalize_url(entry_id)
    # feedparser sometimes stores links as a list under 'links'
    links = entry.get("links", [])
    if isinstance(links, list):
        for lnk in links:
            href = lnk.get("href", "") if isinstance(lnk, dict) else ""
            if href:
                return _normalize_url(href)
    # Try content
    content = entry.get("content", [])
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                html = c.get("value", "") or ""
                import re
                m = re.search(r'href=["\']([^"\']+)["\']', html)
                if m:
                    return _normalize_url(m.group(1))
    return ""


def _entry_text(entry: feedparser.FeedParserDict) -> str:
    """Concatenate title + description (and summary if present) for keyword
    matching. Lowercase, simple whitespace join."""
    parts: list[str] = []
    title = entry.get("title", "") or ""
    parts.append(title)
    desc = entry.get("description", "") or ""
    parts.append(desc)
    summary = entry.get("summary", "") or ""
    parts.append(summary)
    # Also try content
    content_list = entry.get("content", [])
    if isinstance(content_list, list):
        for c in content_list:
            if isinstance(c, dict):
                parts.append(c.get("value", "") or "")
    text = " ".join(parts).lower()
    # Strip HTML tags crudely (feedparser usually returns stripped text in
    # description, but not always)
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def _keywords_matched(entry_text: str, keywords: list[str]) -> list[str]:
    """Return the list of keywords (lowercased) that appear in entry_text."""
    matched: list[str] = []
    for kw in keywords:
        if kw.lower() in entry_text:
            matched.append(kw.lower())
    return matched


def fetch_feed(
    feed_cfg: dict,
    keywords: list[str],
    skip_keywords: list[str],
    seen: set[str],
    max_articles: int,
) -> tuple[list[dict], int]:
    """Fetch one RSS feed, return (new_matching_articles, total_fetched_count).

    Each article is a dict:
      - title: str
      - link: str
      - source: str  (feed name)
      - matched_keywords: list[str]
      - published: str (ISO timestamp or empty)
    """
    url = feed_cfg["url"]
    name = feed_cfg.get("name", url)
    articles: list[dict] = []
    total_fetched = 0

    log.info("Fetching feed: %s (%s)", name, url)
    try:
        feed = feedparser.parse(url)
    except Exception as exc:  # feedparser may raise on truly malformed XML; catch broadly
        log.error("Failed to parse feed %s: %s", name, exc)
        return articles, 0

    if not hasattr(feed, "entries"):
        log.warning("Feed %s returned no entries", name)
        return articles, 0

    entries = feed.entries
    new_count = 0
    for entry in entries:
        total_fetched += 1
        if new_count >= max_articles:
            break

        link = _get_entry_link(entry)
        if not link:
            continue
        if link in seen:
            continue

        entry_text = _entry_text(entry)
        matched = _keywords_matched(entry_text, keywords)
        if not matched:
            continue
        # Skip if any skip_keyword appears (noise filter)
        if skip_keywords:
            skip_matched = _keywords_matched(entry_text, skip_keywords)
            if skip_matched:
                _title = entry.get("title", "") or ""
                log.debug("Skipping (noise): %s — matched skip: %s", _title[:50], skip_matched)
                continue

        # Build a clean title (strip HTML crudely if needed)
        title = entry.get("title", "") or "No title"
        import re
        title = re.sub(r"<[^>]+>", "", title).strip()

        # Grab raw description for the brief formatter (may be HTML)
        desc = entry.get("description", "") or ""

        # Published date: try to get something human-readable
        published = ""
        if hasattr(entry, "published"):
            published = entry.published or ""
        elif hasattr(entry, "updated"):
            published = entry.updated or ""
        # If feedparser parsed it, use the raw string
        if not published and entry.get("published_parsed"):
            # Try to format
            import time as _time
            try:
                published = _time.strftime(
                    "%Y-%m-%d %H:%M", entry.published_parsed
                )
            except Exception:
                published = ""

        articles.append(
            {
                "title": title,
                "link": link,
                "source": name,
                "matched_keywords": matched,
                "published": published,
                "description": desc,  # raw description text for the brief formatter
            }
        )
        new_count += 1

    log.info(
        "Feed %s: fetched %d entries, %d new matches after filtering",
        name,
        total_fetched,
        new_count,
    )
    return articles, total_fetched


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_article_message(article: dict) -> str:
    """HTML-formatted Telegram message for one article — brief style.

    Focus on what happened: title + a short description snippet + source/time.
    Keyword tags kept minimal (top 2).
    """
    title = article["title"]
    link = article["link"]
    source = article["source"]
    keywords = article["matched_keywords"]
    published = article["published"]

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # Take a short snippet from the description/summary for the "what happened" part.
    # Prefer description, fall back to summary, then nothing.
    snippet = ""
    desc = article.get("description", "") or ""
    if desc:
        # Strip HTML tags crudely
        import re
        clean = re.sub(r"<[^>]+>", " ", desc).strip()
        # Collapse whitespace
        clean = re.sub(r"\s+", " ", clean)
        # Take first ~280 chars
        snippet = clean[:280]
        if len(clean) > 280:
            snippet += "…"

    # Keywords: show at most the first two most relevant
    kws_display = ""
    if keywords:
        top_kws = keywords[:3]
        kws_display = " • " + ", ".join(f"<b>{esc(k)}</b>" for k in top_kws)

    time_str = f" • <b>{esc(published)}</b>" if published else ""

    # Build brief message
    msg_parts = [
        f"⚠️ <b>{esc(title)}</b>",
    ]
    if snippet:
        msg_parts.append(f"_{esc(snippet)}_")
    msg_parts.append(f"🗞️ {esc(source)}{time_str}{kws_display}")
    msg_parts.append(f"🔗 <a href=\"{esc(link)}\">{esc(link)}</a>")

    return "\n".join(msg_parts)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    problems = validate_config(cfg)
    if problems:
        for p in problems:
            log.error("CONFIG: %s", p)
        log.error("Fix the config and restart. Aborting.")
        sys.exit(1)

    bot_token = cfg["bot_token"]
    channel_id = cfg["channel_id"]
    feeds = cfg["feeds"]
    keywords = cfg["keywords"]
    interval = int(cfg.get("poll_interval_seconds", 1800))
    max_articles = int(cfg.get("max_articles_per_run", 20))
    telegram_timeout = int(cfg.get("telegram_timeout_seconds", 30))

    log.info(
        "Starting geopolitics31 bot — %d feeds, %d keywords, %.1f min interval, "
        "channel=%s",
        len(feeds),
        len(keywords),
        interval / 60,
        channel_id,
    )

    seen = load_seen_urls()
    log.info("Loaded %d previously seen URLs", len(seen))

    run_number = 0
    try:
        while True:
            run_number += 1
            log.info("=== Run #%d ===", run_number)
            all_new: list[dict] = []

            for feed_cfg in feeds:
                new_articles, _total = fetch_feed(
                    feed_cfg, keywords, skip_keywords, seen, max_articles
                )
                all_new.extend(new_articles)
                # Mark these as seen immediately so we don't re-send on a crash
                for art in new_articles:
                    seen.add(art["link"])

            # Deduplicate across feeds (same article from two sources)
            seen_links: set[str] = set()
            unique_new: list[dict] = []
            for art in all_new:
                if art["link"] in seen_links:
                    continue
                seen_links.add(art["link"])
                unique_new.append(art)

            log.info("Run #%d: %d new unique articles to send", run_number, len(unique_new))

            if unique_new:
                # Send in batches of 5 with a short pause to be gentle to the API
                batch_size = 5
                for i in range(0, len(unique_new), batch_size):
                    batch = unique_new[i : i + batch_size]
                    for art in batch:
                        msg = format_article_message(art)
                        log.debug("Sending: %s", msg[:80])
                        resp = telegram_send_message(
                            bot_token, channel_id, msg, timeout=telegram_timeout
                        )
                        if not resp.get("ok"):
                            log.warning(
                                "Failed to send article '%s': %s",
                                art["title"][:50],
                                resp.get("description") or resp.get("error"),
                            )
                        time.sleep(0.5)  # small gap between messages
                    if i + batch_size < len(unique_new):
                        log.info("Sent batch, pausing 3s before next batch...")
                        time.sleep(3)

                # Persist seen URLs after each run
                save_seen_urls(seen)
                log.info("Saved %d seen URLs to %s", len(seen), SEEN_FILE)
            else:
                log.info("Run #%d: no new matching articles", run_number)

            log.info("Sleeping %.1f minutes until next run...", interval / 60)
            # Sleep in 1-minute increments so a SIGINT fires promptly
            slept = 0
            while slept < interval:
                time.sleep(min(60, interval - slept))
                slept += 60
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        save_seen_urls(seen)
        log.info("Saved %d seen URLs on exit. Goodbye.", len(seen))


if __name__ == "__main__":
    main()
