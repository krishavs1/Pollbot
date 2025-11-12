#!/usr/bin/env python3
# pollwatch_phone.py
#
# Watch a single Poll Everywhere link and make a phone call when
# a poll goes live (new activity) or flips to "accepting responses".
#
# Setup:
#   1) pip install -r requirements_sms.txt (uses same deps)
#   2) Copy .env.phone.example to .env and fill in values
#   3) python pollwatch_phone.py
#
# Note: You'll need to create a TwiML Bin in Twilio or use a webhook URL
#       The script will use the TWILIO_TWIML_URL for the call message
#
import os
import re
import time
import json
import signal
import logging
import threading
from typing import Optional, Tuple
from urllib.parse import quote
import requests
from dotenv import load_dotenv
from twilio.rest import Client

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)

STATE_FILE = "poll_state.json"

ID_PATTERNS = [
    # Pattern for multiple choice/choice polls (most common)
    re.compile(r'id="response_root_question_(\d+)"', re.I),  # Choice polls: response_root_question_933456047
    # Pattern for text/open polls
    re.compile(r'id="all_submissions_question_(\d+)"', re.I),  # Text polls: all_submissions_question_933455006
    # Patterns from action URLs (extract question ID - first number)
    re.compile(r'action="/a/questions/(\d+)/responses', re.I),  # Any question response URL (matches both with/without response ID)
    # Pattern for turbo-frame src URLs (polleverywhere.com links)
    re.compile(r'src="/multiple_choice_polls/([^"/]+)/respond', re.I),  # Multiple choice polls via polleverywhere.com
    re.compile(r'src="/text_polls/([^"/]+)/respond', re.I),  # Text polls via polleverywhere.com
    # Generic patterns
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
    General detection - works for any poll type (text, choice, etc.)
    """
    respond_present = bool(re.search(r'<turbo-frame[^>]+src="[^"]+/respond', html, re.I))

    activity_id = None
    for pat in ID_PATTERNS:
        m = pat.search(html)
        if m:
            # For patterns with multiple groups, use first group (question ID)
            activity_id = m.group(1)
            break

    # If no activity ID could be parsed and the page is still showing the waiting screen,
    # treat it as inactive. Otherwise continue so we can react to lock/unlock transitions.
    if not activity_id and WAITING_HINT.search(html) and not respond_present:
        return None, False, None

    # More general title extraction - try various locations
    title = None
    # Try h1 tags
    m_title = re.search(r'<h1[^>]*>([^<]{5,200})</h1>', html, re.I)
    if not m_title:
        # Try h2 tags
        m_title = re.search(r'<h2[^>]*>([^<]{5,200})</h2>', html, re.I)
    if not m_title:
        # Try any heading with significant text
        m_title = re.search(r'<h[1-6][^>]*>([^<]{10,200})</h[1-6]>', html, re.I)
    if m_title:
        title = re.sub(r"\s+", " ", m_title.group(1)).strip()

    # General accepting detection - looks for any indication poll is active
    accepting = bool(ACCEPTING_HINTS.search(html))
    
    # Also detect if there are response forms (indicating poll is accepting)
    if not accepting:
        # Check for response forms/buttons which indicate accepting
        has_response_forms = bool(re.search(r'(action="/a/questions/\d+/responses|data-input--choice|data-response-to)', html, re.I))
        if has_response_forms:
            accepting = True
    if not accepting and respond_present:
        accepting = True
    
    return activity_id, accepting, title

def make_phone_call(body: str, *, client: Client, from_number: str, to_number: str, twiml_url: str = None) -> None:
    """Make a phone call using Twilio with TwiML URL."""
    try:
        if not twiml_url:
            raise RuntimeError("Please provide TWILIO_TWIML_URL in .env (your ngrok URL or TwiML Bin URL)")
        
        call = client.calls.create(
            to=to_number,
            from_=from_number,
            url=twiml_url,
            method='GET'
        )
        
        logging.info(f"Phone call initiated successfully. SID: {call.sid}")
    except Exception as e:
        raise RuntimeError(f"Twilio phone call error: {e}")

def main():
    load_dotenv()
    url = os.environ.get("POLL_URL")
    twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from_number = os.environ.get("TWILIO_FROM_NUMBER")
    twilio_to_number = os.environ.get("TWILIO_TO_NUMBER")
    twiml_url = os.environ.get("TWILIO_TWIML_URL")  # Your ngrok URL pointing to /twiml endpoint (e.g., https://xxxx.ngrok-free.app/twiml)
    interval = int(os.environ.get("INTERVAL_SEC", "30"))
    
    # Extract username from POLL_URL (e.g., "krishavsingla" from "https://pe.app/krishavsingla")
    from urllib.parse import urlparse
    parsed_url = urlparse(url)
    username = parsed_url.path.strip('/').split('/')[-1] if parsed_url.path else None

    # Support multiple phone numbers (comma-separated or single)
    if twilio_to_number:
        phone_numbers = [num.strip() for num in twilio_to_number.split(',')]
    else:
        phone_numbers = []

    if not all([url, twilio_account_sid, twilio_auth_token, twilio_from_number]) or not phone_numbers:
        raise SystemExit("Missing required env vars. See .env.phone.example. Need at least one phone number in TWILIO_TO_NUMBER.")

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
    headers = {"User-Agent": "pollwatch-phone/1.0"}

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
                # EXACT same logic as Telegram script
                if activity_id and activity_id != last_id:
                    changed = True
                    reason = "New activity is live"
                elif activity_id and accepting and not state.get("alerted_accepting_for_id") == activity_id:
                    changed = True
                    reason = "Activity is now accepting responses"

                if changed:
                    # Format phone message using username from POLL_URL
                    if username:
                        if "New activity is live" in reason:
                            body = f"{username} has just posted a poll. Go check it out!"
                        else:
                            body = f"{username} poll is now accepting responses. Go check it out!"
                    else:
                        # Fallback if username not available
                        snippet = f"“{title}”" if title else (f"ID {activity_id}" if activity_id else "")
                        body = f"Poll Everywhere: {reason}. {snippet}. Go check it out!"
                    
                    logging.info("Making phone calls to %d number(s) in parallel: %s", len(phone_numbers), body)
                    # Make calls to all phone numbers in parallel using threads
                    threads = []
                    for phone_num in phone_numbers:
                        def call_number(num):
                            try:
                                # Add message as query parameter to TwiML URL
                                twiml_with_message = f"{twiml_url}?message={quote(body)}"
                                make_phone_call(body, client=client, from_number=twilio_from_number, to_number=num, twiml_url=twiml_with_message)
                            except Exception as e:
                                logging.error("Failed to call %s: %s", num, e)
                        
                        thread = threading.Thread(target=call_number, args=(phone_num,))
                        thread.start()
                        threads.append(thread)
                    
                    # Wait for all calls to be initiated (they'll run in parallel)
                    for thread in threads:
                        thread.join(timeout=2)  # Give it 2 seconds max per call initiation

                    # EXACT same state management as Telegram script
                    if activity_id:
                        last_id = activity_id
                        state["last_seen_id"] = last_id
                    else:
                        # Clear last_seen_id when poll goes down so we detect it when it comes back up
                        last_id = None
                        state["last_seen_id"] = None
                        # Also clear the accepting flag so we can alert again when poll comes back
                        state["alerted_accepting_for_id"] = None
                    
                    if activity_id and accepting:
                        state["alerted_accepting_for_id"] = activity_id
                elif activity_id and not accepting:
                    if state.get("alerted_accepting_for_id") == activity_id:
                        logging.debug("Activity %s is locked/not accepting; clearing alerted flag", activity_id)
                        state["alerted_accepting_for_id"] = None
                elif not activity_id:
                    # Poll is down - clear last_seen_id and alerted_accepting_for_id so we detect when it comes back up
                    # EXACT same logic as Telegram script
                    if last_id is not None:
                        logging.debug("Poll is down, clearing last_seen_id and alerted_accepting_for_id to detect when it comes back up")
                        last_id = None
                        state["last_seen_id"] = None
                        # Also clear the accepting flag so we can alert again when poll comes back
                        state["alerted_accepting_for_id"] = None

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

