#!/usr/bin/env python3
# pollwatch_sms.py
#
# Watch a single Poll Everywhere link and send an SMS message when
# a poll goes live (new activity) or flips to "accepting responses".
#
# Setup:
#   1) pip install -r requirements_sms.txt
#   2) Copy .env.sms.example to .env and fill in values
#   3) python pollwatch_sms.py
#
# Get a Twilio account:
#   - Sign up at https://www.twilio.com/
#   - Get your Account SID and Auth Token from the dashboard
#   - Get a phone number from Twilio
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
from twilio.rest import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

STATE_FILE = "poll_state.json"

ID_PATTERNS = [
    re.compile(r'data-activity-id="([^"]+)"', re.I),
    re.compile(r'name="activity_id"\s+value="([^"]+)"', re.I),
    re.compile(r'"activityId"\s*:\s*"([^"]+)"', re.I),  # JSON bootstrap fallback
]

ACCEPTING_HINTS = re.compile(
    r"(accepting responses|respond|submit|send response|vote now)",
    re.I
)

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
    title is best-effort; used only for nicer SMS text.
    """
    activity_id = None
    for pat in ID_PATTERNS:
        m = pat.search(html)
        if m:
            activity_id = m.group(1)
            break

    # naive title grab (best effort)
    title = None
    m_title = re.search(r'data-(?:title|question)[="\']([^"\']{5,200})["\']', re.I)
    if not m_title:
        m_title = re.search(r"<h1[^>]*>([^<]{5,200})</h1>", re.I) or \
                  re.search(r"<h2[^>]*>([^<]{5,200})</h2>", re.I)
    if m_title:
        title = re.sub(r"\s+", " ", m_title.group(1)).strip()

    accepting = bool(ACCEPTING_HINTS.search(html))
    return activity_id, accepting, title

def send_sms(body: str, *, client: Client, from_number: str, to_number: str) -> None:
    try:
        message = client.messages.create(
            body=body,
            from_=from_number,
            to=to_number
        )
        logging.info(f"SMS sent successfully. SID: {message.sid}")
    except Exception as e:
        raise RuntimeError(f"Twilio SMS error: {e}")

def main():
    load_dotenv()
    url = os.environ.get("POLL_URL")
    twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from_number = os.environ.get("TWILIO_FROM_NUMBER")
    twilio_to_number = os.environ.get("TWILIO_TO_NUMBER")
    interval = int(os.environ.get("INTERVAL_SEC", "30"))

    if not all([url, twilio_account_sid, twilio_auth_token, twilio_from_number, twilio_to_number]):
        raise SystemExit("Missing required env vars. See .env.sms.example.")

    # Initialize Twilio client
    client = Client(twilio_account_sid, twilio_auth_token)

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
    headers = {"User-Agent": "pollwatch-sms/1.0"}

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

                logging.info("Detected id=%s accepting=%s", activity_id, accepting)

                if activity_id and activity_id != last_id:
                    changed = True
                    reason = "New activity is live"
                elif activity_id and accepting and not state.get("alerted_accepting_for_id") == activity_id:
                    changed = True
                    reason = "Activity is now accepting responses"

                if changed:
                    snippet = f"“{title}”" if title else (f"ID {activity_id}" if activity_id else "")
                    body = f"Poll Everywhere: {reason}\n{snippet}\n{url}"
                    logging.info("Sending SMS: %s", body)
                    send_sms(body, client=client, from_number=twilio_from_number, to_number=twilio_to_number)

                    last_id = activity_id or last_id
                    state["last_seen_id"] = last_id
                    if activity_id and accepting:
                        state["alerted_accepting_for_id"] = activity_id

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


