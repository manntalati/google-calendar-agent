from datetime import datetime, timedelta
import os.path
import dateparser
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import re

SCOPES = ["https://www.googleapis.com/auth/calendar"]

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

def normalize_event_time(start_str, end_str=None, all_day=False, tz="America/Chicago"):
  if all_day:
      start_date = dateparser.parse(start_str).date()
      if not end_str:
          end_date = start_date + timedelta(days=1)
      else:
          end_date = dateparser.parse(end_str).date()
      return (
          {"date": start_date.isoformat()},
          {"date": end_date.isoformat()}
      )

  start_dt = dateparser.parse(start_str, settings={"RETURN_AS_TIMEZONE_AWARE": True})
  if not end_str:
      end_dt = start_dt + timedelta(hours=1)
  else:
      end_dt = dateparser.parse(end_str, settings={"RETURN_AS_TIMEZONE_AWARE": True})

  return (
    {"dateTime": start_dt.isoformat(), "timeZone": tz},
    {"dateTime": end_dt.isoformat(), "timeZone": tz}
  )

def get_next_event():
  calendars = service.calendarList().list().execute().get("items", [])
  now = datetime.utcnow().isoformat() + 'Z'
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

def create_new_event(calendar_id, summary, start_str, end_str=None, description=None, attendees=None, all_day=False, ignore_conflict=False):
    if not calendar_id:
        calendar_id = "primary"

    start, end = normalize_event_time(start_str, end_str, all_day)

    if all_day:
        start_dt = dateparser.parse(start_str)
        end_dt = dateparser.parse(end_str) if end_str else start_dt + timedelta(days=1)
    else:
        start_dt = dateparser.parse(start_str)
        end_dt = dateparser.parse(end_str) if end_str else start_dt + timedelta(hours=1)

    if not ignore_conflict:
        time_min = (start_dt - timedelta(hours=1)).isoformat() + "Z"
        time_max = (end_dt + timedelta(hours=1)).isoformat() + "Z"
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
            ev_start_dt = dateparser.parse(ev_start)
            ev_end_dt = dateparser.parse(ev_end)
            if events_overlap(start_dt, end_dt, ev_start_dt, ev_end_dt):
                return {"success": False, "error": "Event conflict detected", "conflict": event}

    event = {
        "summary": summary,
        "start": start,
        "end": end,
    }

    if description:
        event["description"] = description
    if attendees:
        event["attendees"] = [{"email": a} for a in attendees]

    try:
        result = service.events().insert(calendarId=calendar_id, body=event).execute()
        return {"success": True, "eventId": result["id"], "link": result.get("htmlLink")}
    except Exception as e:
        print("Error occurred:", e)
        return {"success": False}

def delete_event(calendar_id, summary=None, start_str=None, end_str=None):
    if not calendar_id:
        calendar_id = "primary"

    def clean_summary(text):
        text = re.sub(r'\b(on|at|tomorrow|today|am|pm|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}:\d{2})\b', '', text, flags=re.IGNORECASE)
        return text.strip()

    if summary:
        summary = clean_summary(summary)

    time_min, time_max = None, None
    if start_str:
        start_dt = dateparser.parse(start_str)
        if not end_str:
            end_dt = start_dt + timedelta(hours=1)
        else:
            end_dt = dateparser.parse(end_str)
        
        time_min = (start_dt - timedelta(hours=1)).isoformat() + "Z"
        time_max = (end_dt + timedelta(hours=1)).isoformat() + "Z"

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
            ev_summary = event.get("summary", "")
            if summary:
                if summary.lower() in ev_summary.lower():
                    matching_events.append(event)
            else:
                matching_events.append(event)

        if not matching_events:
            if not time_min and not time_max and summary:
                all_events = service.events().list(
                    calendarId=calendar_id,
                    singleEvents=True,
                    orderBy="startTime"
                ).execute().get("items", [])
                for event in all_events:
                    ev_summary = event.get("summary", "")
                    if summary.lower() in ev_summary.lower():
                        matching_events.append(event)

        if not matching_events:
            return {"success": False, "error": "No matching events found"}

        for event in matching_events:
            service.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()
            return {"success": True, "deleted": event.get("summary")}

        return {"success": False, "error": "No matching events found"}
    except Exception as e:
        return {"success": False, "error": str(e)}