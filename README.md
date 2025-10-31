# Poll Everywhere Monitor

A monitoring tool that sends phone call notifications via Twilio when polls are posted on Poll Everywhere.

## Features

- 📞 Phone call notifications when polls are posted
- 🌐 Web interface for easy configuration
- 🔄 Supports multiple poll types (text polls, multiple choice polls)
- ⚡ Configurable polling interval
- 🔍 General poll detection that works with any Poll Everywhere poll format

## Setup

1. **Install dependencies:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements_phone.txt
   ```

2. **Configure Twilio credentials in `.env` file:**
   ```bash
   cp env.phone.example .env
   # Edit .env and add your Twilio credentials
   ```

3. **Start the TwiML server** (for phone call messages):
   ```bash
   python twiml_server.py
   # In another terminal, expose it with ngrok:
   ngrok http 80
   ```

4. **Update `.env` with your ngrok URL:**
   ```
   TWILIO_TWIML_URL=https://your-ngrok-url.ngrok-free.app/twiml
   ```

## Usage

### Web Interface (Recommended)

Start the web app:
```bash
python web_app.py
```

Open http://localhost:5001 in your browser and:
1. Enter your Poll Everywhere URL (e.g., `https://pe.app/krishavsingla`)
2. Enter your phone number (e.g., `+15551234567`)
3. Click "Start Monitoring"

### Command Line

Start monitoring:
```bash
python pollwatch_phone.py
```

The script uses environment variables from `.env`:
- `POLL_URL` - Poll Everywhere page URL
- `TWILIO_TO_NUMBER` - Phone number to receive calls
- `INTERVAL_SEC` - Polling interval (default: 30 seconds)

## Configuration

### Required Environment Variables

- `TWILIO_ACCOUNT_SID` - Your Twilio Account SID
- `TWILIO_AUTH_TOKEN` - Your Twilio Auth Token
- `TWILIO_FROM_NUMBER` - Your Twilio phone number (must be verified)
- `TWILIO_TWIML_URL` - Your TwiML endpoint URL (ngrok or Twilio TwiML Bin)

### Optional Environment Variables

- `INTERVAL_SEC` - Polling interval in seconds (default: 30)

## How It Works

1. Polls the Poll Everywhere page at regular intervals
2. Detects when a new poll is posted or when a poll starts accepting responses
3. Extracts the username from the Poll URL
4. Makes a phone call via Twilio with a custom message: "{username} has just posted a poll. Go check it out!"
5. Tracks state to avoid duplicate notifications

## Project Structure

- `pollwatch_phone.py` - Main monitoring script (command line)
- `web_app.py` - Web interface for managing monitors
- `twiml_server.py` - Flask server for generating TwiML responses
- `env.phone.example` - Example environment variables file

## License

MIT

