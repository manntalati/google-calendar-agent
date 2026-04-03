"""
Slack integration — scans channels for scheduling-related messages.

To use:
1. Create a Slack App at https://api.slack.com/apps
2. Add Bot Token Scopes: channels:history, channels:read, users:read
3. Install to workspace and copy the Bot Token
4. Add to config.json:  "slack_bot_token": "xoxb-your-token-here"
"""

import json
import os
import re
from datetime import datetime, timedelta

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")

def load_token():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    token = config.get("slack_bot_token")
    if not token:
        raise ValueError("No slack_bot_token found in config.json. Create a Slack App and add your bot token.")
    return token

# Patterns that suggest scheduling intent
SCHEDULE_PATTERNS = [
    r"let'?s\s+(meet|sync|chat|call|connect)",
    r"(can|could)\s+(we|you)\s+(meet|do|schedule|set up)",
    r"(are|is)\s+(you|everyone)\s+(free|available)",
    r"(meeting|standup|sync|call|event)\s+(at|on|tomorrow|next|this)",
    r"(schedule|set up|book|plan)\s+(a|an|the)\s+",
    r"\b(tomorrow|next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday))\s+at\s+\d",
    r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b",
    r"(zoom|meet|teams)\s*(link|call|meeting)",
    r"https://(zoom\.us|meet\.google\.com|teams\.microsoft\.com)/",
]

SCHEDULE_RE = [re.compile(p, re.IGNORECASE) for p in SCHEDULE_PATTERNS]

def is_scheduling_message(text):
    """Check if a message contains scheduling signals."""
    for pattern in SCHEDULE_RE:
        if pattern.search(text):
            return True
    return False

def resolve_channel_id(token, channel_name):
    """Resolve a channel name (e.g. #general) to a channel ID."""
    import requests

    channel_name = channel_name.lstrip("#").strip()
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        "https://slack.com/api/conversations.list",
        headers=headers,
        params={"types": "public_channel,private_channel", "limit": 200},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Slack API error: {data.get('error', 'unknown')}")

    for ch in data.get("channels", []):
        if ch["name"] == channel_name or ch["id"] == channel_name:
            return ch["id"]

    raise ValueError(f"Channel '{channel_name}' not found")

def get_user_name(token, user_id, user_cache={}):
    """Get a user's display name from their ID."""
    if user_id in user_cache:
        return user_cache[user_id]

    import requests
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        "https://slack.com/api/users.info",
        headers=headers,
        params={"user": user_id},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        name = data["user"].get("real_name") or data["user"].get("name", user_id)
        user_cache[user_id] = name
        return name
    return user_id

def scan_channel_for_events(channel, lookback_hours=24):
    """
    Scan a Slack channel for messages that contain scheduling signals.

    Args:
        channel: Channel name or ID (e.g. "#team-general" or "C01234567")
        lookback_hours: How far back to scan (default 24 hours)

    Returns:
        List of dicts with keys: text, summary, start, from
    """
    import requests

    token = load_token()
    channel_id = resolve_channel_id(token, channel)

    headers = {"Authorization": f"Bearer {token}"}
    oldest = (datetime.now() - timedelta(hours=lookback_hours)).timestamp()

    resp = requests.get(
        "https://slack.com/api/conversations.history",
        headers=headers,
        params={"channel": channel_id, "oldest": str(oldest), "limit": 200},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Slack API error: {data.get('error', 'unknown')}")

    events = []
    for msg in data.get("messages", []):
        text = msg.get("text", "")
        if not text or not is_scheduling_message(text):
            continue

        user_name = get_user_name(token, msg.get("user", ""))

        # Extract a rough summary and time from the message
        # Truncate long messages
        summary = text[:100] + ("..." if len(text) > 100 else "")

        # Try to find a time mention
        time_match = re.search(
            r'(?:at|@)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))',
            text, re.IGNORECASE
        )
        time_str = time_match.group(1) if time_match else None

        # Try to find a date mention
        date_match = re.search(
            r'(tomorrow|today|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|'
            r'\d{1,2}/\d{1,2}(?:/\d{2,4})?)',
            text, re.IGNORECASE
        )
        date_str = date_match.group(1) if date_match else None

        start = None
        if date_str and time_str:
            start = f"{date_str} at {time_str}"
        elif time_str:
            start = f"today at {time_str}"
        elif date_str:
            start = date_str

        events.append({
            "text": text,
            "summary": summary,
            "start": start,
            "from": user_name,
            "timestamp": msg.get("ts"),
        })

    return events
