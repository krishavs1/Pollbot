#!/usr/bin/env python3
"""
Web interface for Poll Everywhere monitor
"""
import os
import re
import time
import json
import threading
from typing import Optional, Tuple, Dict
from urllib.parse import quote, urlparse
import requests
from flask import Flask, render_template_string, request, jsonify
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

app = Flask(__name__)

# Shared state
active_monitors: Dict[str, Dict] = {}
STATE_FILE = "poll_state.json"

ID_PATTERNS = [
    re.compile(r'id="response_root_question_(\d+)"', re.I),
    re.compile(r'id="all_submissions_question_(\d+)"', re.I),
    re.compile(r'action="/a/questions/(\d+)/responses', re.I),
    re.compile(r'data-activity-id="([^"]+)"', re.I),
    re.compile(r'name="activity_id"\s+value="([^"]+)"', re.I),
    re.compile(r'"activityId"\s*:\s*"([^"]+)"', re.I),
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
        except:
            pass
    return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def extract_activity(html: str) -> Tuple[Optional[str], bool, Optional[str]]:
    """Return (activity_id, accepting_flag, title)"""
    if WAITING_HINT.search(html):
        return None, False, None
    
    activity_id = None
    for pat in ID_PATTERNS:
        m = pat.search(html)
        if m:
            activity_id = m.group(1)
            break

    title = None
    m_title = re.search(r'<h1[^>]*>([^<]{5,200})</h1>', html, re.I)
    if not m_title:
        m_title = re.search(r'<h2[^>]*>([^<]{5,200})</h2>', html, re.I)
    if not m_title:
        m_title = re.search(r'<h[1-6][^>]*>([^<]{10,200})</h[1-6]>', html, re.I)
    if m_title:
        title = re.sub(r"\s+", " ", m_title.group(1)).strip()

    accepting = bool(ACCEPTING_HINTS.search(html))
    if not accepting:
        has_response_forms = bool(re.search(r'(action="/a/questions/\d+/responses|data-input--choice|data-response-to)', html, re.I))
        if has_response_forms:
            accepting = True
    
    return activity_id, accepting, title

def make_phone_call(body: str, *, client: Client, from_number: str, to_number: str, twiml_url: str) -> None:
    """Make a phone call using Twilio with TwiML URL."""
    try:
        twiml_with_message = f"{twiml_url}?message={quote(body)}"
        call = client.calls.create(
            to=to_number,
            from_=from_number,
            url=twiml_with_message,
            method='GET'
        )
        print(f"Phone call initiated. SID: {call.sid}")
    except Exception as e:
        print(f"Twilio phone call error: {e}")

def monitor_poll(poll_url: str, phone_number: str, monitor_id: str):
    """Monitor a Poll Everywhere page and make calls when polls are detected."""
    global active_monitors
    
    # Get Twilio config from .env
    twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from_number = os.environ.get("TWILIO_FROM_NUMBER")
    twiml_url = os.environ.get("TWILIO_TWIML_URL")
    interval = int(os.environ.get("INTERVAL_SEC", "5"))
    
    if not all([twilio_account_sid, twilio_auth_token, twilio_from_number, twiml_url]):
        active_monitors[monitor_id]["status"] = "error"
        active_monitors[monitor_id]["error"] = "Missing Twilio credentials in .env file"
        return
    
    # Extract username from poll URL
    parsed_url = urlparse(poll_url)
    username = parsed_url.path.strip('/').split('/')[-1] if parsed_url.path else None
    
    client = Client(twilio_account_sid, twilio_auth_token)
    state = load_state()
    state_key = f"monitor_{monitor_id}"
    
    if state_key not in state:
        state[state_key] = {}
    
    monitor_state = state[state_key]
    last_id = monitor_state.get("last_seen_id")
    etag = monitor_state.get("etag")
    last_modified = monitor_state.get("last_modified")
    
    sess = requests.Session()
    headers = {"User-Agent": "pollwatch-web/1.0"}
    
    active_monitors[monitor_id]["status"] = "running"
    
    while monitor_id in active_monitors and active_monitors[monitor_id].get("status") == "running":
        try:
            h = dict(headers)
            if etag:
                h["If-None-Match"] = etag
            if last_modified:
                h["If-Modified-Since"] = last_modified
            
            resp = sess.get(poll_url, headers=h, timeout=10, allow_redirects=True)
            
            if resp.status_code == 304:
                pass  # Not modified
            elif resp.ok:
                etag = resp.headers.get("ETag") or etag
                last_modified = resp.headers.get("Last-Modified") or last_modified
                
                html = resp.text
                activity_id, accepting, title = extract_activity(html)
                
                changed = False
                if activity_id and activity_id != last_id:
                    changed = True
                    reason = "New activity is live"
                elif activity_id and accepting and monitor_state.get("alerted_accepting_for_id") != activity_id:
                    changed = True
                    reason = "Activity is now accepting responses"
                
                if changed:
                    # Format message
                    if username:
                        if "New activity is live" in reason:
                            body = f"{username} has just posted a poll. Go check it out!"
                        else:
                            body = f"{username} poll is now accepting responses. Go check it out!"
                    elif title:
                        if "New activity is live" in reason:
                            body = f"{title} has just posted a poll. Go check it out!"
                        else:
                            body = f"{title} poll is now accepting responses. Go check it out!"
                    else:
                        if "New activity is live" in reason:
                            body = "A new poll has just been posted. Go check it out!"
                        else:
                            body = "A poll is now accepting responses. Go check it out!"
                    
                    # Make phone call
                    make_phone_call(body, client=client, from_number=twilio_from_number, 
                                  to_number=phone_number, twiml_url=twiml_url)
                    
                    # Update state
                    if activity_id:
                        last_id = activity_id
                        monitor_state["last_seen_id"] = last_id
                    else:
                        last_id = None
                        monitor_state["last_seen_id"] = None
                        monitor_state["alerted_accepting_for_id"] = None
                    
                    if activity_id and accepting:
                        monitor_state["alerted_accepting_for_id"] = activity_id
                elif not activity_id:
                    # Poll is down - clear state
                    if last_id is not None:
                        last_id = None
                        monitor_state["last_seen_id"] = None
                        monitor_state["alerted_accepting_for_id"] = None
                
                monitor_state["etag"] = etag
                monitor_state["last_modified"] = last_modified
                save_state(state)
                
                # Update monitor status if monitor still exists
                if monitor_id in active_monitors:
                    active_monitors[monitor_id]["last_check"] = time.time()
                    active_monitors[monitor_id]["last_activity"] = activity_id if activity_id else None
            
            time.sleep(interval)
            
        except Exception as e:
            print(f"Error monitoring {poll_url}: {e}")
            time.sleep(interval)
    
    # Remove monitor from tracking when loop exits (monitor stopped)
    if monitor_id in active_monitors:
        del active_monitors[monitor_id]

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Poll Everywhere Monitor</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }
        .header h1 { font-size: 28px; margin-bottom: 10px; }
        .header p { opacity: 0.9; }
        .content {
            padding: 40px;
        }
        .form-group {
            margin-bottom: 25px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #333;
        }
        input[type="text"], input[type="tel"] {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 16px;
            transition: border-color 0.3s;
        }
        input[type="text"]:focus, input[type="tel"]:focus {
            outline: none;
            border-color: #667eea;
        }
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 14px 28px;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.4);
        }
        .btn:active { transform: translateY(0); }
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }
        .monitors {
            margin-top: 40px;
        }
        .monitor-card {
            background: #f8f9fa;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 15px;
        }
        .monitor-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .status {
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .status.running { background: #d4edda; color: #155724; }
        .status.stopped { background: #f8d7da; color: #721c24; }
        .status.error { background: #fff3cd; color: #856404; }
        .monitor-info {
            color: #666;
            font-size: 14px;
            margin: 5px 0;
        }
        .stop-btn {
            background: #dc3545;
            padding: 8px 16px;
            font-size: 14px;
            margin-top: 10px;
        }
        .alert {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .alert.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .alert.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .note {
            background: #e7f3ff;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 25px;
            border-left: 4px solid #667eea;
        }
        .note p {
            color: #555;
            font-size: 14px;
            line-height: 1.6;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📞 Poll Everywhere Monitor</h1>
            <p>Get phone calls when polls are posted</p>
        </div>
        <div class="content">
            <div class="note">
                <p><strong>Note:</strong> Make sure your Twilio credentials (Account SID, Auth Token, From Number, TwiML URL) are configured in your .env file.</p>
            </div>
            
            <form id="monitorForm">
                <div class="form-group">
                    <label for="pollUrl">Poll Everywhere URL</label>
                    <input type="text" id="pollUrl" name="pollUrl" 
                           placeholder="https://pe.app/krishavsingla" 
                           required>
                </div>
                <div class="form-group">
                    <label for="phoneNumber">Your Phone Number</label>
                    <input type="tel" id="phoneNumber" name="phoneNumber" 
                           placeholder="+15551234567" 
                           required>
                    <small style="color: #666; font-size: 12px; display: block; margin-top: 5px;">
                        Include country code (e.g., +1 for US)
                    </small>
                </div>
                <button type="submit" class="btn" id="submitBtn">Start Monitoring</button>
            </form>
            
            <div id="alertContainer"></div>
            
            <div class="monitors" id="monitorsContainer">
                <h2 style="margin-bottom: 20px;">Active Monitors</h2>
                <div id="monitorsList"></div>
            </div>
        </div>
    </div>
    
    <script>
        function showAlert(message, type) {
            const container = document.getElementById('alertContainer');
            container.innerHTML = `<div class="alert ${type}">${message}</div>`;
            setTimeout(() => container.innerHTML = '', 5000);
        }
        
        function loadMonitors() {
            fetch('/api/monitors')
                .then(r => r.json())
                .then(data => {
                    const list = document.getElementById('monitorsList');
                    if (data.monitors.length === 0) {
                        list.innerHTML = '<p style="color: #999; text-align: center; padding: 20px;">No active monitors</p>';
                        return;
                    }
                    
                    list.innerHTML = data.monitors.map(m => `
                        <div class="monitor-card">
                            <div class="monitor-header">
                                <strong>${m.poll_url}</strong>
                                <span class="status ${m.status}">${m.status}</span>
                            </div>
                            <div class="monitor-info">📞 ${m.phone_number}</div>
                            ${m.status === 'running' ? `
                                <button class="btn stop-btn" onclick="stopMonitor('${m.id}')">Stop</button>
                            ` : ''}
                        </div>
                    `).join('');
                });
        }
        
        function stopMonitor(id) {
            fetch(`/api/monitors/${id}/stop`, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        showAlert('Monitor stopped', 'success');
                        loadMonitors();
                    } else {
                        showAlert(data.error || 'Failed to stop monitor', 'error');
                    }
                });
        }
        
        document.getElementById('monitorForm').addEventListener('submit', (e) => {
            e.preventDefault();
            const btn = document.getElementById('submitBtn');
            btn.disabled = true;
            btn.textContent = 'Starting...';
            
            const formData = new FormData(e.target);
            fetch('/api/monitors/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    pollUrl: formData.get('pollUrl'),
                    phoneNumber: formData.get('phoneNumber')
                })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showAlert('Monitor started!', 'success');
                    e.target.reset();
                    loadMonitors();
                } else {
                    showAlert(data.error || 'Failed to start monitor', 'error');
                }
            })
            .finally(() => {
                btn.disabled = false;
                btn.textContent = 'Start Monitoring';
            });
        });
        
        // Load monitors on page load
        loadMonitors();
        setInterval(loadMonitors, 5000);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/monitors/start', methods=['POST'])
def start_monitor():
    data = request.json
    poll_url = data.get('pollUrl', '').strip()
    phone_number = data.get('phoneNumber', '').strip()
    
    if not poll_url or not phone_number:
        return jsonify({"success": False, "error": "Poll URL and phone number are required"}), 400
    
    # Validate poll URL
    if not poll_url.startswith('http'):
        return jsonify({"success": False, "error": "Invalid poll URL"}), 400
    
    # Create monitor ID
    import hashlib
    monitor_id = hashlib.md5(f"{poll_url}{phone_number}".encode()).hexdigest()[:12]
    
    # If monitor already exists and is running, reject the request
    if monitor_id in active_monitors:
        existing_status = active_monitors[monitor_id].get("status", "unknown")
        if existing_status == "running":
            return jsonify({"success": False, "error": "Monitor already exists for this URL and phone number"}), 400
    
    # Start monitoring thread
    active_monitors[monitor_id] = {
        "poll_url": poll_url,
        "phone_number": phone_number,
        "status": "starting",
        "started_at": time.time()
    }
    
    thread = threading.Thread(target=monitor_poll, args=(poll_url, phone_number, monitor_id), daemon=True)
    thread.start()
    
    return jsonify({"success": True, "monitor_id": monitor_id})

@app.route('/api/monitors/<monitor_id>/stop', methods=['POST'])
def stop_monitor(monitor_id):
    if monitor_id not in active_monitors:
        return jsonify({"success": False, "error": "Monitor not found"}), 404
    
    # Delete the monitor instead of marking it as stopped
    del active_monitors[monitor_id]
    return jsonify({"success": True})

@app.route('/api/monitors', methods=['GET'])
def list_monitors():
    monitors = []
    for mid, info in active_monitors.items():
        monitors.append({
            "id": mid,
            "poll_url": info.get("poll_url", ""),
            "phone_number": info.get("phone_number", ""),
            "status": info.get("status", "unknown"),
            "last_check": info.get("last_check"),
            "last_activity": info.get("last_activity")
        })
    return jsonify({"monitors": monitors})

if __name__ == '__main__':
    print("Starting Poll Everywhere Monitor Web App...")
    print("Open http://localhost:5001 in your browser")
    app.run(host='0.0.0.0', port=5001, debug=True)


