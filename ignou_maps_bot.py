#!/usr/bin/env python3
"""IGNOU MAPS updates Telegram bot.

Watches public IGNOU MAPS / M.A. Political Science pages and sends Telegram
messages when relevant links/text change. Uses SQLite to avoid duplicate alerts.

Required environment variables:
  BOT_TOKEN  Telegram bot token from @BotFather
  CHAT_ID    Your Telegram user/group/channel chat ID
Optional:
  CHECK_INTERVAL  Seconds between website scans (default 1800)
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from html import unescape
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
try:
    CHECK_INTERVAL = max(300, int(os.getenv("CHECK_INTERVAL", "1800")))
except ValueError:
    CHECK_INTERVAL = 1800

DB_PATH = os.getenv("DB_PATH", "ignou_maps_bot.sqlite3")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
    "Chrome/126.0 Mobile Safari/537.36 IGNOU-MAPS-UpdateBot/1.0"
)

SOURCES = [
    {
        "key": "announcements",
        "name": "IGNOU Announcements",
        "url": "https://www.ignou.ac.in/announcements/0?nav=1",
        "kind": "announcements",
    },
    {
        "key": "current_assignments",
        "name": "IGNOU Current Assignments",
        "url": "https://www.ignou.ac.in/studentService/download/assignments",
        "kind": "current_assignments",
    },
    {
        "key": "maps_assignments",
        "name": "MAPS / M.A. Political Science Assignments",
        "url": "https://webservices.ignou.ac.in/assignments/Master-Degree/MPS/ma_polsci.htm",
        "kind": "maps_assignments",
    },
    {
        "key": "maps_programme",
        "name": "IGNOU MAPS Programme Page",
        "url": "https://www.ignou.ac.in/schools/programme/MAPS",
        "kind": "maps_programme",
    },
    {
        "key": "egyankosh_maps",
        "name": "eGyanKosh MAPS Resources",
        "url": "https://www.egyankosh.ac.in/handle/123456789/126837",
        "kind": "egyankosh",
    },
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"})

CODE_RE = re.compile(
    r"\b(?:MAPS|MPS\s*[-–]?\s*\d{1,3}|MPSE\s*[-–]?\s*\d{1,3}|"
    r"MGP\s*[-–]?\s*\d{1,3}|MGPE\s*[-–]?\s*\d{1,3})\b",
    re.IGNORECASE,
)
RELEVANT_RE = re.compile(
    r"political\s*science|assignment|term[ -]?end|\bTEE\b|date\s*sheet|"
    r"hall\s*ticket|result|grade\s*card|re[ -]?registration|last\s*date|"
    r"deadline|examination|exam\s*schedule|notification|notice|semester|"
    r"course\s*material|study\s*material|june\s*2027|december\s*2026",
    re.IGNORECASE,
)


@dataclass
class Item:
    source_key: str
    source_name: str
    title: str
    url: str
    context: str
    identity: str
    fingerprint: str


def compact(text: str, limit: int = 350) -> str:
    text = unescape(re.sub(r"\s+", " ", text or "")).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS items (
                identity TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        con.commit()


def db_has_baseline() -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT value FROM meta WHERE key='baseline_done'").fetchone()
        return bool(row and row[0] == "1")


def mark_baseline_done() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO meta(key, value) VALUES('baseline_done','1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        con.commit()


def telegram_send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured; message would have been:\n" + text)
        return False
    try:
        response = SESSION.post(
            f"{TELEGRAM_API}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            print("Telegram API error:", payload)
            return False
        return True
    except (requests.RequestException, ValueError) as exc:
        print(f"Telegram send failed: {exc}")
        return False


def telegram_updates(offset: int | None) -> list[dict]:
    if not BOT_TOKEN or not CHAT_ID:
        return []
    params: dict[str, object] = {"timeout": 1}
    if offset is not None:
        params["offset"] = offset
    try:
        response = SESSION.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=8)
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok"):
            return payload.get("result", [])
        print("Telegram getUpdates error:", payload)
    except (requests.RequestException, ValueError) as exc:
        print(f"Telegram command polling failed: {exc}")
    return []


def get_context(anchor: Tag) -> str:
    # Prefer the table row or notice/card container so assignment code and
    # session/due-date details are included even if the link says only English/PDF.
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"tr", "li", "article"}:
            text = compact(parent.get_text(" ", strip=True), 700)
            if text:
                return text
        classes = " ".join(parent.get("class", [])).lower()
        if any(k in classes for k in ("notice", "announcement", "card", "result", "listing")):
            text = compact(parent.get_text(" ", strip=True), 700)
            if text:
                return text
        # Use a small nearby container only if it holds useful text.
        if parent.name in {"div", "section", "td"}:
            text = compact(parent.get_text(" ", strip=True), 700)
            if 18 <= len(text) <= 700:
                return text
        if parent.name in {"body", "html"}:
            break
    return compact(anchor.get_text(" ", strip=True), 350)


def source_relevant(kind: str, title: str, context: str, url: str) -> bool:
    blob = f"{title} {context} {url}"
    if kind == "maps_assignments":
        # This page is dedicated to M.A. Political Science assignment sets.
        return True
    if kind == "current_assignments":
        return bool(CODE_RE.search(blob) or re.search(r"M\.A\.?\s*\(?Pol\.?\s*Science|Political Science", blob, re.I))
    if kind == "maps_programme":
        return bool(CODE_RE.search(blob) or re.search(r"political science|MAPS|course structure", blob, re.I))
    if kind == "egyankosh":
        return bool(CODE_RE.search(blob) or re.search(r"political science|semester[- ]?[i1v]+", blob, re.I))
    if kind == "announcements":
        return bool(CODE_RE.search(blob) or RELEVANT_RE.search(blob))
    return bool(CODE_RE.search(blob) or RELEVANT_RE.search(blob))


def extract_items(source: dict) -> list[Item]:
    response = SESSION.get(source["url"], timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    # Remove layout-only content to reduce navigation/footer noise.
    for node in soup.find_all(["script", "style", "noscript", "svg"]):
        node.decompose()

    items: list[Item] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        raw_href = str(anchor.get("href", "")).strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full_url = urljoin(source["url"], raw_href)
        parsed = urlparse(full_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if full_url.rstrip("/") == source["url"].rstrip("/"):
            continue

        title = compact(anchor.get_text(" ", strip=True), 220)
        if not title:
            path_name = urlparse(full_url).path.rsplit("/", 1)[-1]
            title = compact(path_name.replace("%20", " ").replace("_", " "), 180)
        if len(title) < 2:
            continue

        context = get_context(anchor)
        kind = source["kind"]
        if not source_relevant(kind, title, context, full_url):
            continue

        # Ignore repeated links within one page.
        dedupe_key = full_url + "|" + title.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        identity = hashlib.sha256(f"{source['key']}|{full_url}|{title.lower()}".encode()).hexdigest()
        fingerprint = hashlib.sha256(f"{title}|{context}|{full_url}".encode()).hexdigest()
        items.append(
            Item(
                source_key=source["key"],
                source_name=source["name"],
                title=title,
                url=full_url,
                context=context,
                identity=identity,
                fingerprint=fingerprint,
            )
        )
    return items


def upsert_item(item: Item, baseline: bool) -> str:
    """Return 'new', 'changed', or 'same'. First scan only establishes baseline."""
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT fingerprint FROM items WHERE identity=?", (item.identity,)
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO items(identity, source_key, title, url, fingerprint, first_seen, last_seen) "
                "VALUES(?,?,?,?,?,?,?)",
                (item.identity, item.source_key, item.title, item.url, item.fingerprint, now, now),
            )
            con.commit()
            return "same" if baseline else "new"
        if row[0] != item.fingerprint:
            con.execute(
                "UPDATE items SET title=?, url=?, fingerprint=?, last_seen=? WHERE identity=?",
                (item.title, item.url, item.fingerprint, now, item.identity),
            )
            con.commit()
            return "same" if baseline else "changed"
        con.execute("UPDATE items SET last_seen=? WHERE identity=?", (now, item.identity))
        con.commit()
    return "same"


def format_item(item: Item, prefix: str = "NEW") -> str:
    context = item.context
    if context.lower() == item.title.lower():
        context = ""
    parts = [f"[{prefix}] {item.source_name}", item.title]
    if context:
        parts.append(context)
    parts.append(item.url)
    return "\n".join(parts)


def scan_websites(force_summary: bool = False) -> tuple[int, int, list[Item]]:
    baseline = not db_has_baseline()
    all_items: list[Item] = []
    new_count = 0
    errors: list[str] = []

    for source in SOURCES:
        try:
            found = extract_items(source)
            all_items.extend(found)
            for item in found:
                status = upsert_item(item, baseline=baseline)
                if status in {"new", "changed"}:
                    new_count += 1
                    telegram_send(format_item(item, "NEW" if status == "new" else "UPDATED"))
            print(f"[{time.strftime('%H:%M:%S')}] {source['name']}: {len(found)} matching links")
        except Exception as exc:  # keep other sources working when one site is down
            message = f"{source['name']}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print("Scrape error:", message)

    if baseline and not errors:
        mark_baseline_done()
        telegram_send(
            "IGNOU MAPS Update Bot started.\n"
            "Initial website scan complete; existing links were saved as baseline, so old notices will not be spammed.\n"
            "Use /check to get a current snapshot, /status to see bot status.\n"
            f"Check interval: {CHECK_INTERVAL // 60} minutes."
        )
    elif baseline and errors:
        # Keep baseline mode active until every source can be fetched; this prevents
        # an unavailable site from flooding the chat with old links on the next run.
        print("Initial scan incomplete; will retry baseline without alerting old links.")
    elif force_summary:
        # Send a manageable snapshot from the official pages; avoid flooding Telegram.
        summary_items: list[Item] = []
        used: set[str] = set()
        for item in all_items:
            if item.url not in used:
                used.add(item.url)
                summary_items.append(item)
            if len(summary_items) >= 10:
                break
        if summary_items:
            header = "IGNOU MAPS — current matching links\n\n"
            body = "\n\n────────────\n\n".join(format_item(item, "CURRENT") for item in summary_items)
            # Keep each Telegram message comfortably below Telegram's message limit.
            message = header + body
            if len(message) <= 3800:
                telegram_send(message)
            else:
                telegram_send(header + "\n\n".join(format_item(item, "CURRENT") for item in summary_items[:4]))
                telegram_send("More current links are available on the official source pages listed in /status.")
        else:
            telegram_send("No matching links were found in this scan. The source sites may be temporarily unavailable or their page layout may have changed.")

    if errors:
        print(f"Scan finished with {len(errors)} source error(s).")
    return len(all_items), new_count, all_items


def handle_commands(offset: int | None) -> int | None:
    updates = telegram_updates(offset)
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            offset = max(offset or 0, update_id + 1)
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if chat_id != CHAT_ID:
            continue
        text = (message.get("text") or "").strip().split()[0:1]
        command = text[0].split("@")[0].lower() if text else ""
        if command in {"/start", "/help"}:
            telegram_send(
                "IGNOU MAPS Update Bot\n\n"
                "/check — current MAPS-related links का snapshot\n"
                "/status — sources और interval\n"
                "/help — commands\n\n"
                "Bot IGNOU की public pages पर नए relevant links/changes check करता है।"
            )
        elif command == "/check":
            telegram_send("IGNOU MAPS pages scan कर रहा हूँ…")
            scan_websites(force_summary=True)
        elif command == "/status":
            source_lines = "\n".join(f"• {s['name']}: {s['url']}" for s in SOURCES)
            telegram_send(
                "IGNOU MAPS Update Bot: RUNNING\n"
                f"Scan interval: {CHECK_INTERVAL // 60} minutes\n"
                f"Database: {DB_PATH}\n\nMonitoring sources:\n{source_lines}"
            )
    return offset


def main() -> int:
    if not BOT_TOKEN or not CHAT_ID:
        print(
            "ERROR: BOT_TOKEN and CHAT_ID are required.\n"
            "Set them as environment variables before running; do not share your bot token.\n"
            "Example:\n"
            "  export BOT_TOKEN='123456:your_token'\n"
            "  export CHAT_ID='123456789'\n"
            "  python ignou_maps_bot.py"
        )
        return 2

    init_db()
    telegram_send("IGNOU MAPS Update Bot process is online. Preparing first scan…")
    offset: int | None = None
    next_scan = 0.0
    while True:
        now = time.monotonic()
        if now >= next_scan:
            scan_websites()
            next_scan = time.monotonic() + CHECK_INTERVAL
        offset = handle_commands(offset)
        time.sleep(2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
