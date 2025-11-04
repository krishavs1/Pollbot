#!/usr/bin/env python3
"""Quick test to verify phone call setup"""
import os
from dotenv import load_dotenv
from twilio.rest import Client
from urllib.parse import quote

load_dotenv()

# Get credentials
account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
from_number = os.environ.get("TWILIO_FROM_NUMBER")
to_number = os.environ.get("TWILIO_TO_NUMBER")
twiml_url = os.environ.get("TWILIO_TWIML_URL")

print("Testing phone call setup...")
print(f"From: {from_number}")
print(f"To: {to_number}")
print(f"TwiML URL: {twiml_url}")

# Check required vars
if not all([account_sid, auth_token, from_number, to_number, twiml_url]):
    print("❌ Missing required environment variables!")
    missing = []
    if not account_sid: missing.append("TWILIO_ACCOUNT_SID")
    if not auth_token: missing.append("TWILIO_AUTH_TOKEN")
    if not from_number: missing.append("TWILIO_FROM_NUMBER")
    if not to_number: missing.append("TWILIO_TO_NUMBER")
    if not twiml_url: missing.append("TWILIO_TWIML_URL")
    print(f"Missing: {', '.join(missing)}")
    exit(1)

# Initialize Twilio client
client = Client(account_sid, auth_token)

# Test message
test_message = "Hello! This is a test call from your Poll Everywhere monitor. If you hear this, your setup is working correctly!"

# Add message to URL
full_twiml_url = f"{twiml_url}?message={quote(test_message)}"

print(f"\nMaking test call...")
print(f"Calling {to_number} from {from_number}")
print(f"TwiML URL: {full_twiml_url}\n")

try:
    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=full_twiml_url,
        method='GET'
    )
    
    print(f"✅ Phone call initiated successfully!")
    print(f"Call SID: {call.sid}")
    print(f"\n📞 Your phone should ring now!")
    print(f"   You'll hear: '{test_message}'")
    
except Exception as e:
    print(f"❌ Error making call: {e}")
    print("\nTroubleshooting:")
    print("- Make sure twiml_server.py is running on port 5000")
    print("- Make sure ngrok is running and pointing to port 5000")
    print("- Check that your Twilio credentials are correct")
    print("- Verify your phone number format: +1XXXXXXXXXX")


