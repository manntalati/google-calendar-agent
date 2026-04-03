"""
PrairieLearn integration — fetches assignment/quiz deadlines.

To use:
1. Log into PrairieLearn in your browser
2. Copy your session cookie (pl2_session or prairielearn_session)
3. Add to config.json:  "prairielearn_session": "your_cookie_here"

This module fetches assessment deadlines via PrairieLearn's student API.
"""

import json
import os
import requests
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")

def load_session():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    session_cookie = config.get("prairielearn_session")
    if not session_cookie:
        raise ValueError("No prairielearn_session found in config.json. Log into PrairieLearn and add your session cookie.")
    return session_cookie

def parse_course_url(url):
    """
    Parse a PrairieLearn course URL to extract the API path.
    e.g. https://us.prairielearn.com/pl/course_instance/12345
    Returns (base_url, course_instance_id)
    """
    url = url.rstrip("/")
    parts = url.split("/")

    # Find course_instance in the URL
    for i, part in enumerate(parts):
        if part == "course_instance" and i + 1 < len(parts):
            course_instance_id = parts[i + 1]
            base_url = "/".join(parts[:parts.index("pl") + 1]) if "pl" in parts else "/".join(parts[:3])
            return base_url, course_instance_id

    raise ValueError(f"Could not parse course instance from URL: {url}")

def fetch_deadlines(course_url):
    """
    Fetch assessment deadlines from a PrairieLearn course.

    Args:
        course_url: PrairieLearn course URL
            (e.g. https://us.prairielearn.com/pl/course_instance/12345)

    Returns:
        List of dicts with keys: name, type, due_date
    """
    session_cookie = load_session()
    base_url, course_instance_id = parse_course_url(course_url)

    headers = {
        "Cookie": f"prairielearn_session={session_cookie}",
        "User-Agent": "CalendarAgent/1.0",
        "Accept": "application/json",
    }

    # PrairieLearn API endpoint for assessments
    api_url = f"{base_url}/course_instance/{course_instance_id}/assessments"
    resp = requests.get(api_url, headers=headers, timeout=15)

    deadlines = []

    # Try JSON API first
    if resp.headers.get("content-type", "").startswith("application/json"):
        data = resp.json()
        assessments = data if isinstance(data, list) else data.get("assessments", [])
        for assessment in assessments:
            due = assessment.get("due_date") or assessment.get("close_date")
            if not due:
                continue
            deadlines.append({
                "name": assessment.get("title") or assessment.get("tid", "Unknown"),
                "type": assessment.get("type", "homework"),
                "due_date": due,
            })
    else:
        # Fallback: parse HTML page
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        for row in soup.select("table tbody tr, .assessment-row"):
            try:
                cells = row.find_all("td")
                if cells and len(cells) >= 2:
                    name = cells[0].get_text(strip=True)
                    # Look for a date in remaining cells
                    for cell in cells[1:]:
                        text = cell.get_text(strip=True)
                        # Try parsing common date formats
                        for fmt in ["%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M %p", "%b %d, %Y %I:%M %p"]:
                            try:
                                dt = datetime.strptime(text, fmt)
                                deadlines.append({
                                    "name": name,
                                    "type": "homework",
                                    "due_date": dt.isoformat(),
                                })
                                break
                            except ValueError:
                                continue
            except Exception:
                continue

    # Filter to future deadlines only
    now = datetime.now()
    future = []
    for dl in deadlines:
        try:
            dt = datetime.fromisoformat(dl["due_date"].replace("Z", "+00:00"))
            if dt.replace(tzinfo=None) > now:
                future.append(dl)
        except Exception:
            future.append(dl)  # Include if we can't parse the date

    return sorted(future, key=lambda d: d.get("due_date", ""))
