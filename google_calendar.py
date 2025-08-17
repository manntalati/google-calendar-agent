from datetime import datetime, timedelta
import os.path
import dateparser
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

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
  
def create_new_event(calendar_id, summary, start_str, end_str=None, description=None, attendees=None, all_day=False):
  if not calendar_id:
    calendar_id = "primary"

  start, end = normalize_event_time(start_str, end_str, all_day)

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