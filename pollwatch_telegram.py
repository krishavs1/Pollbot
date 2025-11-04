#!/usr/bin/env python3
# pollwatch_telegram.py
#
# Watch a single Poll Everywhere link and send a Telegram message when
# a poll goes live (new activity) or flips to "accepting responses".
#
# Setup:
#   1) pip install -r requirements_telegram.txt
#   2) Copy .env.telegram.example to .env and fill in values
#   3) python pollwatch_telegram.py
#
# Get a Telegram bot token:
#   - Talk to @BotFather, create a bot, copy the token.
# Find your chat ID (for DMs):
#   - Start a chat with your bot (press Start), then visit:
#       https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
#     Look for "message":{"chat":{"id": <CHAT_ID> ...}}
#
import os
import re
import time
import json
import signal
import logging
from typing import Optional, Tuple
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)

STATE_FILE = "poll_state.json"

ID_PATTERNS = [
    re.compile(r'id="all_submissions_question_(\d+)"', re.I),  # Primary pattern for pe.app
    re.compile(r'action="/a/questions/(\d+)/responses"', re.I),  # Alternative pattern
    re.compile(r'data-activity-id="([^"]+)"', re.I),
    re.compile(r'name="activity_id"\s+value="([^"]+)"', re.I),
    re.compile(r'"activityId"\s*:\s*"([^"]+)"', re.I),  # JSON bootstrap fallback
]

ACCEPTING_HINTS = re.compile(
    r"(audience submissions|responding to the presenter|you may respond|accepting responses|respond|submit|send response|vote now)",
    re.I
)

WAITING_HINT = re.compile(r"(waiting)", re.I)

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def extract_activity(html: str) -> Tuple[Optional[str], bool, Optional[str]]:
    """
    Return (activity_id, accepting_flag, title)
    title is best-effort; used only for nicer Telegram text.
    """
    # Check if page is waiting (no activity)
    if WAITING_HINT.search(html):
        return None, False, None
    
    activity_id = None
    for pat in ID_PATTERNS:
        m = pat.search(html)
        if m:
            activity_id = m.group(1)
            break

    # naive title grab (best effort)
    title = None
    m_title = re.search(r'<h1[^>]*>([^<]{5,200})</h1>', html, re.I)
    if not m_title:
        m_title = re.search(r'<h2[^>]*>([^<]{5,200})</h2>', html, re.I)
    if m_title:
        title = re.sub(r"\s+", " ", m_title.group(1)).strip()

    accepting = bool(ACCEPTING_HINTS.search(html))
    return activity_id, accepting, title

def send_telegram(body: str, *, token: str, chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": body}, timeout=10)
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")

def main():
    load_dotenv()
    url = os.environ.get("POLL_URL")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    interval = int(os.environ.get("INTERVAL_SEC", "30"))

    if not all([url, tg_token, tg_chat]):
        raise SystemExit("Missing required env vars. See .env.telegram.example.")

    state = load_state()
    last_id = state.get("last_seen_id")
    etag = state.get("etag")
    last_modified = state.get("last_modified")

    stop = False
    def handle_sig(sig, frame):
        nonlocal stop
        stop = True
        logging.info("Stopping...")
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    sess = requests.Session()
    headers = {"User-Agent": "pollwatch-telegram/1.0"}

    while not stop:
        try:
            h = dict(headers)
            if etag:
                h["If-None-Match"] = etag
            if last_modified:
                h["If-Modified-Since"] = last_modified

            resp = sess.get(url, headers=h, timeout=10, allow_redirects=True)

            changed = False
            if resp.status_code == 304:
                logging.debug("304 Not Modified")
            elif resp.ok:
                etag = resp.headers.get("ETag") or etag
                last_modified = resp.headers.get("Last-Modified") or last_modified

                html = resp.text
                activity_id, accepting, title = extract_activity(html)

                logging.info("Detected id=%s accepting=%s title=%s", activity_id, accepting, title or "None")
                
                # Debug: log HTML snippet if no activity found
                if not activity_id:
                    html_snippet = html[:500] + "..." if len(html) > 500 else html
                    logging.debug("No activity ID found. HTML sample: %s", html_snippet)

                # Detect new activity: going from None/empty to an activity, or activity ID changed
                if activity_id and activity_id != last_id:
                    changed = True
                    reason = "New activity is live"
                elif activity_id and accepting and not state.get("alerted_accepting_for_id") == activity_id:
                    changed = True
                    reason = "Activity is now accepting responses"

                if changed:
                    snippet = f"“{title}”" if title else (f"ID {activity_id}" if activity_id else "")
                    body = f"Poll Everywhere: {reason}\n{snippet}\n{url}"
                    logging.info("Sending Telegram: %s", body)
                    send_telegram(body, token=tg_token, chat_id=tg_chat)

                    if activity_id:
                        last_id = activity_id
                        state["last_seen_id"] = last_id
                    else:
                        # Clear last_seen_id when poll goes down so we detect it when it comes back up
                        last_id = None
                        state["last_seen_id"] = None
                    
                    if activity_id and accepting:
                        state["alerted_accepting_for_id"] = activity_id
                elif not activity_id:
                    # Poll is down - clear last_seen_id so we detect when it comes back up
                    if last_id is not None:
                        logging.debug("Poll is down, clearing last_seen_id to detect when it comes back up")
                        last_id = None
                        state["last_seen_id"] = None

            else:
                logging.warning("HTTP %s from %s", resp.status_code, url)

            state["etag"] = etag
            state["last_modified"] = last_modified
            save_state(state)

        except Exception as e:
            logging.error("Error: %s", e)

        time.sleep(interval)

if __name__ == "__main__":
    main()

