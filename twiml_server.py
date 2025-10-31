#!/usr/bin/env python3
"""
Simple TwiML server for phone calls (following Twilio quickstart pattern).
Run this with ngrok to expose it publicly:
  ngrok http 5000
Then use the ngrok URL as TWILIO_TWIML_URL: https://xxxx.ngrok-free.app/twiml
"""
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse

app = Flask(__name__)

@app.route('/twiml', methods=['GET', 'POST'])
def twiml():
    """Respond to incoming calls with a text message."""
    # Get the message from query parameter, or use default
    message = request.args.get('message', 'Poll Everywhere notification. A new poll is active.')
    
    resp = VoiceResponse()
    resp.say(message, voice='alice', language='en-US')
    resp.hangup()
    
    return str(resp)

if __name__ == '__main__':
    print("Starting TwiML server on http://localhost:80")
    print("Use ngrok to expose it: ngrok http 80")
    print("Then set TWILIO_TWIML_URL in .env to: https://<your-ngrok-url>.ngrok-free.app/twiml")
    app.run(host='0.0.0.0', port=80, debug=True)

