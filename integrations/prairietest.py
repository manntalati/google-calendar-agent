"""
PrairieTest integration — fetches scheduled CBTF exam reservations.

To use:
1. Log into PrairieTest in your browser
2. Copy your session cookie
3. Add to config.json:  "prairietest_session": "your_cookie_here"

This module scrapes the PrairieTest reservations page for upcoming exams.
"""

import json
import os
import requests
from datetime import datetime
from bs4 import BeautifulSoup

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")

def load_session():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    session_cookie = config.get("prairietest_session")
    if not session_cookie:
        raise ValueError("No prairietest_session found in config.json. Log into PrairieTest and add your session cookie.")
    return session_cookie

def fetch_exams(base_url):
    """
    Fetch upcoming exam reservations from PrairieTest.

    Args:
        base_url: PrairieTest base URL (e.g. https://us.prairietest.com)

    Returns:
        List of dicts with keys: name, start, end, location
    """
    session_cookie = load_session()

    headers = {
        "Cookie": f"session={session_cookie}",
        "User-Agent": "CalendarAgent/1.0",
    }

    # Fetch the reservations page
    reservations_url = f"{base_url.rstrip('/')}/reservations"
    resp = requests.get(reservations_url, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    exams = []

    # Parse reservation entries — this depends on PrairieTest's HTML structure
    # and may need adjustment based on their current page layout
    for row in soup.select("table tbody tr, .reservation-card, .exam-row"):
        try:
            # Try to extract exam info from table rows or card elements
            cells = row.find_all("td")
            if cells and len(cells) >= 3:
                name = cells[0].get_text(strip=True)
                date_str = cells[1].get_text(strip=True)
                location = cells[2].get_text(strip=True) if len(cells) > 2 else "CBTF"

                # Parse the date
                try:
                    start_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
                except ValueError:
                    try:
                        start_dt = datetime.strptime(date_str, "%m/%d/%Y %I:%M %p")
                    except ValueError:
                        continue

                exams.append({
                    "name": name,
                    "start": start_dt.isoformat(),
                    "end": None,  # Duration set by create_new_event default
                    "location": location,
                })
            else:
                # Try card-style layout
                title_el = row.select_one(".exam-name, .course-name, h4, h5, strong")
                time_el = row.select_one(".exam-time, .reservation-time, time")
                loc_el = row.select_one(".exam-location, .room")

                if title_el and time_el:
                    exams.append({
                        "name": title_el.get_text(strip=True),
                        "start": time_el.get("datetime", time_el.get_text(strip=True)),
                        "end": None,
                        "location": loc_el.get_text(strip=True) if loc_el else "CBTF",
                    })
        except Exception:
            continue

    return exams
