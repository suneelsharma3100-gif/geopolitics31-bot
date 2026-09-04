#!/usr/bin/env python3
"""
geopolitics31_oneshot.py — Single fetch-and-post cycle for the geopolitics31 bot.

Intended to be called every 30 minutes by a cron job (or manually for a test).
Does exactly one poll of all configured feeds, sends matching new articles to the
configured Telegram chat, persists seen URLs, and exits.

Usage:
  python geopolitics31_oneshot.py            # one cycle
  python geopolitics31_oneshot.py --verbose  # one cycle, more logging
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Import the shared bot logic from the same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
import geopolitics31_bot as bot

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("geopolitics31_oneshot")


def main() -> None:
    parser = argparse.ArgumentParser(description="One cycle of the geopolitics31 RSS→Telegram bot")
    parser.add_argument(
        "--verbose", "-v",
        action="store_const",
        const=logging.DEBUG,
        default=logging.INFO,
        help="More verbose logging (DEBUG level)",
    )
    args = parser.parse_args()
    log.setLevel(args.verbose)

    cfg = bot.load_config()
    problems = bot.validate_config(cfg)
    if problems:
        for p in problems:
            log.error("CONFIG: %s", p)
        log.error("Aborting one-shot.")
        sys.exit(1)

    bot_token = cfg["bot_token"]
    channel_id = cfg["channel_id"]
    feeds = cfg["feeds"]
    keywords = cfg["keywords"]
    skip_keywords = cfg.get("skip_keywords", [])
    max_articles = int(cfg.get("max_articles_per_run", 20))
    telegram_timeout = int(cfg.get("telegram_timeout_seconds", 30))

    log.info("One-shot cycle — %d feeds, %d keywords, %d skip keywords",
             len(feeds), len(keywords), len(skip_keywords))

    seen = bot.load_seen_urls()
    log.debug("Loaded %d previously seen URLs", len(seen))

    all_new: list[dict] = []
    for feed_cfg in feeds:
        new_articles, total = bot.fetch_feed(feed_cfg, keywords, skip_keywords, seen, max_articles)
        log.info(
            "Feed %s: fetched %d, %d new matches",
            feed_cfg["name"],
            total,
            len(new_articles),
        )
        for art in new_articles:
            seen.add(art["link"])
        all_new.extend(new_articles)

    # Dedup across feeds
    seen_links: set[str] = set()
    unique_new: list[dict] = []
    for art in all_new:
        if art["link"] in seen_links:
            continue
        seen_links.add(art["link"])
        unique_new.append(art)

    log.info("Unique new articles to send: %d", len(unique_new))

    if not unique_new:
        log.info("Nothing new to send. Exiting.")
        return

    # Send each article
    sent = 0
    failed = 0
    for art in unique_new:
        msg = bot.format_article_message(art)
        resp = bot.telegram_send_message(
            bot_token, channel_id, msg, timeout=telegram_timeout
        )
        ok = resp.get("ok", False)
        if ok:
            sent += 1
            log.info("Sent (%d/%d): %s", sent, len(unique_new), art["title"][:60])
        else:
            failed += 1
            log.warning(
                "Failed (%d/%d): %s — %s",
                failed,
                len(unique_new),
                art["title"][:60],
                resp.get("description") or resp.get("error"),
            )
        # Small pause between messages to be gentle to the API
        import time
        time.sleep(0.6)

    # Persist
    bot.save_seen_urls(seen)
    log.info(
        "Cycle done: %d sent, %d failed, %d total seen URLs saved",
        sent,
        failed,
        len(seen),
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
