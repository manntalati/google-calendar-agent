from datetime import datetime, timedelta, timezone
import json
import os
import dateparser
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import re
from rapidfuzz import fuzz
import uuid


def utcnow():
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def make_aware(dt):
    """Ensure a datetime is timezone-aware (assume UTC if naive)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_dt(s, **kwargs):
    """Parse a datetime string and ensure it's timezone-aware."""
    if not s:
        return None
    dt = dateparser.parse(str(s), settings={"RETURN_AS_TIMEZONE_AWARE": True, **kwargs})
    return make_aware(dt) if dt else None

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Load config
with open(os.path.join(os.path.dirname(__file__), "config.json")) as f:
    CONFIG = json.load(f)

TIMEZONE = CONFIG["timezone"]

def init_service():
    creds = None
    if os.path.exists("token.json"):
        try:
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)

service = init_service()


def normalize_event_time(start_str, end_str=None, all_day=False, tz=None):
    if tz is None:
        tz = TIMEZONE

    if all_day:
        start_date = parse_dt(start_str).date()
        if not end_str:
            end_date = start_date + timedelta(days=1)
        else:
            end_date = parse_dt(end_str).date()
        return (
            {"date": start_date.isoformat()},
            {"date": end_date.isoformat()}
        )

    start_dt = parse_dt(start_str)
    if not end_str:
        end_dt = start_dt + timedelta(hours=1)
    else:
        end_dt = parse_dt(end_str)

    return (
        {"dateTime": start_dt.isoformat(), "timeZone": tz},
        {"dateTime": end_dt.isoformat(), "timeZone": tz}
    )


def get_next_event():
    calendars = service.calendarList().list().execute().get("items", [])
    now = utcnow().isoformat()
    next_event, next_start = None, None
    for calendar in calendars:
        event = service.events().list(
            calendarId=calendar['id'],
            timeMin=now,
            maxResults=1,
            singleEvents=True,
            orderBy='startTime'
        ).execute().get('items', [])

        if event:
            start_str = event[0]["start"].get("dateTime", event[0]["start"].get("date"))
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if next_start is None or start_dt < next_start:
                next_start = start_dt
                next_event = {
                    "calendar": calendar.get("summary"),
                    "summary": event[0].get("summary"),
                    "start": start_str,
                    "id": event[0]["id"]
                }

    return next_event


# --- Free/busy and slot finding ---

def find_free_slots(start_str, end_str, duration_minutes=60):
    """Find free time slots in a date range across all calendars."""
    start_dt = parse_dt(start_str)
    end_dt = parse_dt(end_str)

    if not start_dt or not end_dt:
        return {"success": False, "error": "Could not parse date range"}

    # Get all calendar IDs
    calendars = service.calendarList().list().execute().get("items", [])
    calendar_ids = [{"id": c["id"]} for c in calendars]

    # Query FreeBusy API
    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "items": calendar_ids,
    }
    try:
        freebusy = service.freebusy().query(body=body).execute()
    except Exception as e:
        return {"success": False, "error": str(e)}

    # Merge all busy periods across calendars
    busy_periods = []
    for cal_id, cal_data in freebusy.get("calendars", {}).items():
        for busy in cal_data.get("busy", []):
            busy_start = datetime.fromisoformat(busy["start"].replace("Z", "+00:00"))
            busy_end = datetime.fromisoformat(busy["end"].replace("Z", "+00:00"))
            busy_periods.append((busy_start, busy_end))

    # Sort and merge overlapping busy periods
    busy_periods.sort(key=lambda x: x[0])
    merged = []
    for start, end in busy_periods:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Find gaps that fit the requested duration
    slot_duration = timedelta(minutes=duration_minutes)
    free_slots = []

    current = start_dt
    for busy_start, busy_end in merged:
        if current + slot_duration <= busy_start:
            free_slots.append({
                "start": current.isoformat(),
                "end": busy_start.isoformat(),
                "duration_minutes": int((busy_start - current).total_seconds() / 60),
            })
        current = max(current, busy_end)

    # Check gap after last busy period
    if current + slot_duration <= end_dt:
        free_slots.append({
            "start": current.isoformat(),
            "end": end_dt.isoformat(),
            "duration_minutes": int((end_dt - current).total_seconds() / 60),
        })

    return {"success": True, "free_slots": free_slots, "count": len(free_slots)}


def suggest_next_free_slot(start_str, duration_minutes=60):
    """Find the next available slot starting from a given time."""
    start_dt = parse_dt(start_str)
    if not start_dt:
        return {"success": False, "error": "Could not parse start time"}

    # Search the next 7 days
    end_dt = start_dt + timedelta(days=7)
    result = find_free_slots(start_dt.isoformat(), end_dt.isoformat(), duration_minutes)

    if not result.get("success"):
        return result

    # Filter to slots during reasonable hours (8am-9pm)
    reasonable_slots = []
    for slot in result["free_slots"]:
        slot_start = datetime.fromisoformat(slot["start"])
        hour = slot_start.hour
        if 8 <= hour <= 21 and slot["duration_minutes"] >= duration_minutes:
            reasonable_slots.append(slot)

    if reasonable_slots:
        return {"success": True, "suggested_slot": reasonable_slots[0], "all_slots": reasonable_slots[:5]}
    elif result["free_slots"]:
        return {"success": True, "suggested_slot": result["free_slots"][0], "all_slots": result["free_slots"][:5]}
    else:
        return {"success": False, "error": "No free slots found in the next 7 days"}


# --- List events by date range ---

def list_events(calendar_id=None, start_str=None, end_str=None, max_results=50):
    """List events in a date range. If no calendar_id, searches all calendars."""
    if not start_str:
        time_min = utcnow().isoformat()
    else:
        dt = parse_dt(start_str)
        time_min = dt.isoformat()

    if not end_str:
        if start_str:
            dt = parse_dt(start_str)
            time_max = (dt.replace(hour=23, minute=59, second=59)).isoformat()
        else:
            time_max = (utcnow() + timedelta(days=7)).isoformat()
    else:
        dt_end = parse_dt(end_str)
        time_max = dt_end.isoformat()

    calendar_ids = []
    if calendar_id:
        calendar_ids = [calendar_id]
    else:
        calendars = service.calendarList().list().execute().get("items", [])
        calendar_ids = [c["id"] for c in calendars]

    all_events = []
    for cal_id in calendar_ids:
        try:
            events_result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime"
            ).execute()
            for event in events_result.get("items", []):
                all_events.append({
                    "id": event["id"],
                    "calendar_id": cal_id,
                    "summary": event.get("summary", "(No title)"),
                    "start": event["start"].get("dateTime", event["start"].get("date")),
                    "end": event["end"].get("dateTime", event["end"].get("date")),
                    "location": event.get("location", ""),
                    "description": event.get("description", ""),
                    "attendees": [a.get("email") for a in event.get("attendees", [])],
                    "recurrence": event.get("recurrence", []),
                    "hangoutLink": event.get("hangoutLink", ""),
                })
        except Exception:
            continue

    all_events.sort(key=lambda e: e["start"])
    return {"success": True, "events": all_events, "count": len(all_events)}


def get_calendar_id(name_or_id):
    if name_or_id.lower() == "primary":
        return "primary"

    calendars = service.calendarList().list().execute().get("items", [])
    for cal in calendars:
        if cal["id"] == name_or_id or cal["summary"].lower() == name_or_id.lower():
            return cal["id"]
    raise ValueError(f"Calendar '{name_or_id}' not found. Available: {[c['summary'] for c in calendars]}")


def events_overlap(start1, end1, start2, end2):
    return max(start1, start2) < min(end1, end2)


# --- Step 6: Enhanced create_new_event with location, recurrence, reminders, color, Meet ---

def create_new_event(calendar_id, summary, start_str, end_str=None, description=None,
                     attendees=None, all_day=False, ignore_conflict=False,
                     location=None, recurrence=None, reminders=None,
                     color_id=None, add_video_call=False):
    if not calendar_id:
        calendar_id = "primary"

    start, end = normalize_event_time(start_str, end_str, all_day)

    if all_day:
        start_dt = parse_dt(start_str)
        end_dt = parse_dt(end_str) if end_str else start_dt + timedelta(days=1)
    else:
        start_dt = parse_dt(start_str)
        end_dt = parse_dt(end_str) if end_str else start_dt + timedelta(hours=1)

    if not ignore_conflict and not all_day:
        time_min = (start_dt - timedelta(minutes=5)).isoformat()
        time_max = (end_dt + timedelta(minutes=5)).isoformat()
        try:
            events_result = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime"
            ).execute()
            events = events_result.get("items", [])
            for event in events:
                ev_start = event["start"].get("dateTime", event["start"].get("date"))
                ev_end = event["end"].get("dateTime", event["end"].get("date"))
                ev_start_dt = parse_dt(ev_start)
                ev_end_dt = parse_dt(ev_end)
                if ev_start_dt and ev_end_dt and events_overlap(start_dt, end_dt, ev_start_dt, ev_end_dt):
                    return {"success": False, "error": "Event conflict detected", "conflict": {
                        "summary": event.get("summary"),
                        "start": ev_start,
                        "end": ev_end,
                    }}
        except Exception:
            pass  # If conflict check fails, proceed with creation

    event = {
        "summary": summary,
        "start": start,
        "end": end,
    }

    if description:
        event["description"] = description
    if attendees:
        event["attendees"] = [{"email": a} for a in attendees]
    if location:
        event["location"] = location
    if recurrence:
        # Accept either a list of RRULE strings or a single string
        if isinstance(recurrence, str):
            event["recurrence"] = [recurrence]
        else:
            event["recurrence"] = recurrence
    if color_id:
        event["colorId"] = str(color_id)

    # Reminders
    if reminders:
        # Accept list of dicts like [{"method": "popup", "minutes": 10}]
        event["reminders"] = {
            "useDefault": False,
            "overrides": reminders
        }
    else:
        # Use default reminder from config
        default_mins = CONFIG.get("default_reminder_minutes")
        if default_mins:
            event["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": default_mins}]
            }

    # Google Meet / video conferencing
    conference_data_version = 0
    if add_video_call:
        event["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"}
            }
        }
        conference_data_version = 1

    try:
        result = service.events().insert(
            calendarId=calendar_id,
            body=event,
            conferenceDataVersion=conference_data_version
        ).execute()
        response = {
            "success": True,
            "eventId": result["id"],
            "link": result.get("htmlLink"),
        }
        if result.get("hangoutLink"):
            response["meetLink"] = result["hangoutLink"]
        return response
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- Step 5: Update/edit existing events ---

def update_event(calendar_id, event_id=None, summary_search=None, start_str_search=None,
                 new_summary=None, new_start_str=None, new_end_str=None,
                 new_description=None, new_location=None, new_attendees=None,
                 new_recurrence=None, new_reminders=None, new_color_id=None,
                 add_video_call=False):
    """Update an existing event. Find by event_id or by fuzzy-matching summary + time."""
    if not calendar_id:
        calendar_id = "primary"

    # Find the event
    event = None
    if event_id:
        try:
            event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as e:
            return {"success": False, "error": f"Event not found: {str(e)}"}
    elif summary_search:
        # Fuzzy search for the event
        now_str = utcnow().isoformat()
        time_min = now_str
        time_max = None
        if start_str_search:
            dt = parse_dt(start_str_search)
            if dt:
                time_min = (dt - timedelta(hours=12)).isoformat()
                time_max = (dt + timedelta(hours=12)).isoformat()

        try:
            kwargs = {
                "calendarId": calendar_id,
                "timeMin": time_min,
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 50,
            }
            if time_max:
                kwargs["timeMax"] = time_max
            events_result = service.events().list(**kwargs).execute()
            events = events_result.get("items", [])

            best_match = None
            best_score = 0
            for ev in events:
                ev_summary = ev.get("summary", "")
                score = fuzz.partial_ratio(ev_summary.lower(), summary_search.lower())
                if score > best_score and score > 60:
                    best_score = score
                    best_match = ev

            if best_match:
                event = best_match
            else:
                return {"success": False, "error": f"No event matching '{summary_search}' found"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    else:
        return {"success": False, "error": "Provide event_id or summary_search to find the event"}

    # Apply updates
    if new_summary:
        event["summary"] = new_summary
    if new_start_str or new_end_str:
        is_all_day = "date" in event.get("start", {})
        start_s = new_start_str or event["start"].get("dateTime", event["start"].get("date"))
        end_s = new_end_str or event["end"].get("dateTime", event["end"].get("date"))
        new_start, new_end = normalize_event_time(start_s, end_s, all_day=is_all_day)
        event["start"] = new_start
        event["end"] = new_end
    if new_description is not None:
        event["description"] = new_description
    if new_location is not None:
        event["location"] = new_location
    if new_attendees is not None:
        event["attendees"] = [{"email": a} for a in new_attendees]
    if new_recurrence is not None:
        if isinstance(new_recurrence, str):
            event["recurrence"] = [new_recurrence]
        else:
            event["recurrence"] = new_recurrence
    if new_reminders is not None:
        event["reminders"] = {
            "useDefault": False,
            "overrides": new_reminders
        }
    if new_color_id is not None:
        event["colorId"] = str(new_color_id)

    conference_data_version = 0
    if add_video_call and not event.get("hangoutLink"):
        event["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"}
            }
        }
        conference_data_version = 1

    try:
        result = service.events().update(
            calendarId=calendar_id,
            eventId=event["id"],
            body=event,
            conferenceDataVersion=conference_data_version
        ).execute()
        response = {
            "success": True,
            "eventId": result["id"],
            "link": result.get("htmlLink"),
            "updated_summary": result.get("summary"),
        }
        if result.get("hangoutLink"):
            response["meetLink"] = result["hangoutLink"]
        return response
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_event(calendar_id, summary=None, start_str=None, end_str=None):
    if not calendar_id:
        calendar_id = "primary"

    def extract_summary_and_time(text):
        cleaned = re.sub(r'on [^ ]+ calendar', '', text, flags=re.IGNORECASE)
        dt = parse_dt(cleaned, PREFER_DATES_FROM='future')
        summary = re.sub(r'at \d{1,2}(:\d{2})?\s*(am|pm)?', '', cleaned, flags=re.IGNORECASE)
        summary = re.sub(r'tomorrow|today|on \d{1,2}/\d{1,2}/\d{2,4}', '', summary, flags=re.IGNORECASE)
        summary = summary.strip()
        return summary, dt

    if summary and (not start_str):
        summary, dt = extract_summary_and_time(summary)
        if dt:
            start_str = dt.isoformat()

    time_min = utcnow().isoformat()
    time_max = None
    if start_str:
        start_dt = parse_dt(start_str)
        if not end_str:
            end_dt = start_dt + timedelta(hours=1)
        else:
            end_dt = parse_dt(end_str)
        time_min = start_dt.isoformat()
        time_max = (end_dt + timedelta(hours=1)).isoformat()

    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])

        matching_events = []
        for event in events:
            ev_summary = event.get("summary", "").strip().lower()
            if summary:
                if fuzz.partial_ratio(ev_summary, summary.lower()) > 60:
                    matching_events.append(event)
            else:
                matching_events.append(event)

        if not matching_events:
            return {"success": False, "error": "No matching events found"}

        for event in matching_events:
            service.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()
            return {"success": True, "deleted": event.get("summary")}

        return {"success": False, "error": "No matching events found"}
    except Exception as e:
        return {"success": False, "error": str(e)}
