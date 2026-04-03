import threading
import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from google.genai import Client, types
from google_calendar import (
    create_new_event, get_next_event, delete_event, list_events,
    update_event, find_free_slots, suggest_next_free_slot,
)
from dotenv import load_dotenv
import os
import re
import dateparser
from datetime import datetime, timedelta
import whisper
import sounddevice as sd
import tempfile
import soundfile as sf

load_dotenv()

os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY")

# Load config
with open(os.path.join(os.path.dirname(__file__), "config.json")) as f:
    CONFIG = json.load(f)

CALENDAR_ALIASES = CONFIG["calendars"]
TIMEZONE = CONFIG["timezone"]
DEFAULT_DURATION = CONFIG["default_event_duration_minutes"]
DURATION_DEFAULTS = CONFIG.get("duration_defaults", {})
CONTACTS = CONFIG.get("contacts", {})

app = FastAPI(title="Calendar Voice Assistant", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
model = whisper.load_model("base")


def build_system_prompt():
    now = datetime.now()
    calendars_str = "\n".join(f'  - "{alias}" -> {cal_id}' for alias, cal_id in CALENDAR_ALIASES.items())
    contacts_str = "\n".join(f'  - "{name}" -> {email}' for name, email in CONTACTS.items()) if CONTACTS else "  (none configured)"
    duration_str = "\n".join(f'  - {event_type}: {mins} minutes' for event_type, mins in DURATION_DEFAULTS.items())

    return f"""You are a Google Calendar assistant. Your job is to interpret the user's natural language request and call the appropriate calendar tool with the correct parameters.

## Current Context
- **Today's date:** {now.strftime('%A, %B %d, %Y')}
- **Current time:** {now.strftime('%I:%M %p')}
- **Timezone:** {TIMEZONE}
- **Default event duration:** {DEFAULT_DURATION} minutes

## Available Calendars (alias -> ID)
{calendars_str}
- Default calendar: "{CONFIG['default_calendar']}" (use this when the user doesn't specify a calendar)

## Known Contacts (name -> email)
{contacts_str}

## Duration Defaults (use when user doesn't specify duration)
{duration_str}

## Rules
1. Always convert relative dates ("tomorrow", "next Tuesday", "in 3 days") to absolute ISO 8601 datetime strings based on today's date and timezone above.
2. When the user mentions a calendar by alias name, use the corresponding calendar ID from the list above.
3. When the user doesn't specify a calendar, use the default calendar.
4. When the user mentions a person by name and that name is in the contacts list, use their email address for attendees.
5. When the user doesn't specify an end time, infer duration from the event type using the duration defaults above. If the event type isn't listed, use {DEFAULT_DURATION} minutes.
6. For recurring events, generate proper RRULE strings (e.g., "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR").
7. For "every weekday" use BYDAY=MO,TU,WE,TH,FR. For "every day" use FREQ=DAILY.
8. When the user asks to see/view/list events, use list_events with appropriate date range.
9. When the user asks to move/change/update/edit/reschedule an event, use update_event.
10. When the user asks to delete/remove/cancel an event, use delete_event.
11. When the user asks "what's next" or "next event", use get_next_event.
12. Set reminders when the user asks (e.g., "remind me 30 min before" -> {{"method": "popup", "minutes": 30}}).
13. Set add_video_call=true when user mentions "video call", "Google Meet", "virtual meeting", or "zoom" (for Meet links).
14. Always call a tool. Never respond with plain text — always make a function call.
"""

class ToolCall(BaseModel):
    tool: str
    args: dict

@app.get('/tools')
def list_tools():
    return {
        "tools": [
            {"name": "create_new_event", "description": "Create a new calendar event"},
            {"name": "get_next_event", "description": "Get the next event"},
            {"name": "delete_event", "description": "Delete an existing calendar event"},
            {"name": "list_events", "description": "List events in a date range"},
            {"name": "update_event", "description": "Update an existing calendar event"},
        ]
    }

@app.post('/invoke')
def invoke_tool(call: ToolCall):
    try:
        fn = TOOL_DISPATCH.get(call.tool)
        if not fn:
            raise HTTPException(status_code=400, detail=f"Unknown tool: {call.tool}")
        return fn(**call.args)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class CommandRequest(BaseModel):
    command: str
    ignore_conflict: bool = False

@app.post('/command')
def handle_command_api(req: CommandRequest):
    """Web-facing endpoint: takes natural language, returns structured result."""
    try:
        parsed = gemini_parse(req.command)
        if not parsed:
            # Fallback to local parsing
            parsed_event = parse_natural_language_event(req.command)
            if parsed_event.get("summary") and parsed_event.get("start_str"):
                parsed = ("create_new_event", parsed_event)
            else:
                return {"status": "error", "message": "Could not understand command. Try rephrasing."}

        tool_name, args = parsed

        if req.ignore_conflict:
            args["ignore_conflict"] = True

        tool_result = call_tool(tool_name, args)

        # Build preview info
        cal_id = args.get("calendar_id", "primary")
        cal_name = cal_id
        for alias, cid in CALENDAR_ALIASES.items():
            if cid == cal_id:
                cal_name = alias
                break

        response = {
            "status": "ok",
            "tool": tool_name,
            "args": args,
            "calendar_name": cal_name,
            "result": tool_result,
        }

        # If conflict, add suggestion
        if (tool_name == "create_new_event"
                and isinstance(tool_result, dict)
                and tool_result.get("success") is False
                and tool_result.get("error") == "Event conflict detected"):
            response["status"] = "conflict"
            suggested = suggest_next_free_slot(args.get("start_str", ""), DEFAULT_DURATION)
            if suggested.get("success") and suggested.get("suggested_slot"):
                response["suggested_slot"] = suggested["suggested_slot"]

        return response
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get('/calendars')
def get_calendars():
    return {"calendars": CALENDAR_ALIASES}

@app.get('/', response_class=HTMLResponse)
def serve_frontend():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path) as f:
        return f.read()


# --- Integration endpoints ---

class PrairieTestRequest(BaseModel):
    url: str
    calendar: str

class PrairieLearnRequest(BaseModel):
    url: str
    calendar: str

class SlackScanRequest(BaseModel):
    channel: str
    lookback_hours: int = 24

@app.post('/integrations/prairietest')
def sync_prairietest_api(req: PrairieTestRequest):
    """Sync PrairieTest exam reservations to a calendar."""
    try:
        # Resolve calendar alias to ID
        cal_id = CALENDAR_ALIASES.get(req.calendar, req.calendar)

        # Try to import the integration module
        try:
            from integrations.prairietest import fetch_exams
            exams = fetch_exams(req.url)
        except ImportError:
            return {
                "success": False,
                "error": "PrairieTest integration not yet configured. Add your session credentials to config.json and create integrations/prairietest.py"
            }

        created = []
        for exam in exams:
            result = create_new_event(
                calendar_id=cal_id,
                summary=f"[EXAM] {exam['name']}",
                start_str=exam["start"],
                end_str=exam.get("end"),
                location=exam.get("location", "CBTF"),
                reminders=[
                    {"method": "popup", "minutes": 1440},  # 1 day
                    {"method": "popup", "minutes": 60},     # 1 hour
                ],
                ignore_conflict=True,
            )
            if result.get("success"):
                created.append(exam)

        return {"success": True, "exams": created, "count": len(created)}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post('/integrations/prairielearn')
def sync_prairielearn_api(req: PrairieLearnRequest):
    """Sync PrairieLearn deadlines to a calendar."""
    try:
        cal_id = CALENDAR_ALIASES.get(req.calendar, req.calendar)

        try:
            from integrations.prairielearn import fetch_deadlines
            deadlines = fetch_deadlines(req.url)
        except ImportError:
            return {
                "success": False,
                "error": "PrairieLearn integration not yet configured. Add your session credentials to config.json and create integrations/prairielearn.py"
            }

        created = []
        for dl in deadlines:
            result = create_new_event(
                calendar_id=cal_id,
                summary=f"[DUE] {dl['name']}",
                start_str=dl["due_date"],
                end_str=dl.get("due_date"),  # Point event at deadline
                reminders=[
                    {"method": "popup", "minutes": 1440},  # 1 day
                    {"method": "popup", "minutes": 120},    # 2 hours
                ],
                ignore_conflict=True,
            )
            if result.get("success"):
                created.append(dl)

        return {"success": True, "deadlines": created, "count": len(created)}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post('/integrations/slack')
def scan_slack_api(req: SlackScanRequest):
    """Scan a Slack channel for scheduling messages."""
    try:
        try:
            from integrations.slack import scan_channel_for_events
            events = scan_channel_for_events(req.channel, req.lookback_hours)
        except ImportError:
            return {
                "success": False,
                "error": "Slack integration not yet configured. Add your slack_bot_token to config.json and create integrations/slack.py"
            }

        return {"success": True, "events": events, "count": len(events)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_server():
    uvicorn.run(app, host="0.0.0.0", port=8000)

def normalize(s: str):
    return re.sub(r"\s+", "", s).lower()

def extract_calendar_id(command_text: str):
    normalized_command = normalize(command_text)
    for alias, cal_id in CALENDAR_ALIASES.items():
        if normalize(alias) in normalized_command:
            cleaned_text = re.sub(
                rf"\b{re.escape(alias)}\b( calendar)?", "", command_text, flags=re.IGNORECASE
            )
            return cal_id, cleaned_text.strip()
    default_id = CALENDAR_ALIASES.get(CONFIG["default_calendar"], "primary")
    return default_id, command_text.strip()

def listen_for_command():
    print("Listening... (speak now)")
    duration = 10
    sample_rate = 16000
    audio = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
        sf.write(wav_path, audio, sample_rate)

    result = model.transcribe(wav_path)
    text = result["text"].strip()
    return text if text else None

def resolve_contacts(attendee_names):
    resolved = []
    for name in attendee_names:
        name_lower = name.strip().lower()
        if name_lower in CONTACTS:
            resolved.append(CONTACTS[name_lower])
        elif "@" in name:
            resolved.append(name.strip())
        else:
            resolved.append(name.strip())
    return resolved

def parse_natural_language_event(command_text):
    calendar_id, cleaned_text = extract_calendar_id(command_text)

    event_data = {
        "calendar_id": calendar_id,
        "summary": "",
        "start_str": "",
        "end_str": "",
        "description": "",
        "attendees": [],
        "all_day": False
    }

    attendees_match = re.findall(r'with ([A-Za-z ,]+)', cleaned_text, re.IGNORECASE)
    if attendees_match:
        attendees = [a.strip() for a in attendees_match[0].split(',')]
        event_data["attendees"] = resolve_contacts(attendees)
        cleaned_text = re.sub(r'with [A-Za-z ,]+', '', cleaned_text, flags=re.IGNORECASE)

    if "all day" in cleaned_text.lower():
        event_data["all_day"] = True

    dt = dateparser.parse(cleaned_text, settings={'PREFER_DATES_FROM': 'future'})
    if dt:
        event_data["start_str"] = dt.isoformat()
        if not event_data["all_day"]:
            event_data["end_str"] = (dt + timedelta(minutes=DEFAULT_DURATION)).isoformat()

    summary = re.sub(r'at \d{1,2}(:\d{2})?\s*(am|pm)?', '', cleaned_text, flags=re.IGNORECASE)
    event_data["summary"] = summary.strip()
    return event_data

def resolve_calendar_id(args):
    if "calendar_id" in args:
        alias_lower = args["calendar_id"].lower().replace(" calendar", "").strip()
        if alias_lower in CALENDAR_ALIASES:
            args["calendar_id"] = CALENDAR_ALIASES[alias_lower]
    return args

client = Client(api_key=os.getenv("GOOGLE_API_KEY"))

create_event_declaration = {
    "name": "create_new_event",
    "description": "Create a new Google Calendar event.",
    "parameters": {
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string", "description": "Calendar ID or alias"},
            "summary": {"type": "string", "description": "Event title"},
            "start_str": {"type": "string", "description": "Start date/time as ISO string"},
            "end_str": {"type": "string", "description": "End date/time as ISO string"},
            "description": {"type": "string", "description": "Event description/notes"},
            "attendees": {"type": "array", "items": {"type": "string"}, "description": "List of attendee emails"},
            "all_day": {"type": "boolean", "description": "True for all-day events"},
            "location": {"type": "string", "description": "Event location"},
            "recurrence": {"type": "array", "items": {"type": "string"}, "description": "RRULE strings, e.g. ['RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR']"},
            "reminders": {"type": "array", "items": {"type": "object"}, "description": "Reminder overrides, e.g. [{'method': 'popup', 'minutes': 10}]"},
            "color_id": {"type": "string", "description": "Google Calendar color ID (1-11)"},
            "add_video_call": {"type": "boolean", "description": "True to auto-create a Google Meet link"}
        },
        "required": ["calendar_id", "summary", "start_str"]
    }
}

get_next_event_declaration = {
    "name": "get_next_event",
    "description": "Get the next upcoming event.",
    "parameters": {"type": "object", "properties": {}}
}

delete_event_declaration = {
    "name": "delete_event",
    "description": "Delete a Google Calendar event by ID or by searching with summary + start time.",
    "parameters": {
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string"},
            "event_id": {"type": "string"},
            "summary": {"type": "string"},
            "start_str": {"type": "string"}
        },
        "required": ["calendar_id"]
    }
}

list_events_declaration = {
    "name": "list_events",
    "description": "List calendar events in a date range. Use to answer questions like 'what do I have tomorrow' or 'show me next week'.",
    "parameters": {
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string", "description": "Calendar ID. If omitted, searches all calendars."},
            "start_str": {"type": "string", "description": "Start of range as ISO string or natural language"},
            "end_str": {"type": "string", "description": "End of range as ISO string or natural language"},
            "max_results": {"type": "integer", "description": "Max events to return (default 50)"}
        },
        "required": []
    }
}

update_event_declaration = {
    "name": "update_event",
    "description": "Update an existing calendar event. Find by event_id or fuzzy-match summary. Can change time, title, location, attendees, etc.",
    "parameters": {
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string"},
            "event_id": {"type": "string", "description": "Event ID if known"},
            "summary_search": {"type": "string", "description": "Search for event by title (fuzzy match)"},
            "start_str_search": {"type": "string", "description": "Narrow search to events near this time"},
            "new_summary": {"type": "string"},
            "new_start_str": {"type": "string"},
            "new_end_str": {"type": "string"},
            "new_description": {"type": "string"},
            "new_location": {"type": "string"},
            "new_attendees": {"type": "array", "items": {"type": "string"}},
            "new_recurrence": {"type": "array", "items": {"type": "string"}},
            "new_reminders": {"type": "array", "items": {"type": "object"}},
            "new_color_id": {"type": "string"},
            "add_video_call": {"type": "boolean"}
        },
        "required": ["calendar_id"]
    }
}

find_free_slots_declaration = {
    "name": "find_free_slots",
    "description": "Find free time slots in a date range across all calendars. Use when user asks 'when am I free', 'find me a slot', etc.",
    "parameters": {
        "type": "object",
        "properties": {
            "start_str": {"type": "string", "description": "Start of range as ISO string"},
            "end_str": {"type": "string", "description": "End of range as ISO string"},
            "duration_minutes": {"type": "integer", "description": "Minimum slot duration in minutes (default 60)"}
        },
        "required": ["start_str", "end_str"]
    }
}

suggest_next_free_slot_declaration = {
    "name": "suggest_next_free_slot",
    "description": "Find the next available time slot starting from a given time. Use when user asks 'when is the next free slot' or after a conflict.",
    "parameters": {
        "type": "object",
        "properties": {
            "start_str": {"type": "string", "description": "Start searching from this time (ISO string)"},
            "duration_minutes": {"type": "integer", "description": "Required slot duration in minutes (default 60)"}
        },
        "required": ["start_str"]
    }
}

gemini_tools = types.Tool(function_declarations=[
    create_event_declaration,
    get_next_event_declaration,
    delete_event_declaration,
    list_events_declaration,
    update_event_declaration,
    find_free_slots_declaration,
    suggest_next_free_slot_declaration,
])

# --- Direct function dispatch (no more HTTP self-calls) ---

TOOL_DISPATCH = {
    "create_new_event": create_new_event,
    "get_next_event": get_next_event,
    "delete_event": delete_event,
    "list_events": list_events,
    "update_event": update_event,
    "find_free_slots": find_free_slots,
    "suggest_next_free_slot": suggest_next_free_slot,
}

def call_tool(tool_name, args):
    fn = TOOL_DISPATCH.get(tool_name)
    if not fn:
        return {"error": f"Unknown tool: {tool_name}"}
    return fn(**args)

def gemini_parse(command_text):
    """Send command to Gemini with full context. Returns (tool_name, args) or None."""
    system_prompt = build_system_prompt()
    gemini_config = types.GenerateContentConfig(
        tools=[gemini_tools],
        system_instruction=system_prompt,
    )
    contents = [types.Content(role="user", parts=[types.Part(text=command_text)])]
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
        config=gemini_config,
    )
    if not response.candidates:
        return None
    candidate = response.candidates[0]
    part = candidate.content.parts[0]
    if hasattr(part, 'function_call') and part.function_call:
        func_call = part.function_call
        args = dict(func_call.args)
        args = resolve_calendar_id(args)
        return func_call.name, args
    return None

def agent_handle_command(command_text):
    """Gemini-first: all commands go through Gemini for parsing."""
    # Try Gemini first for all commands
    result = gemini_parse(command_text)
    if result:
        tool_name, args = result
        tool_result = call_tool(tool_name, args)

        # Handle conflict on create
        if (tool_name == "create_new_event"
                and isinstance(tool_result, dict)
                and tool_result.get("success") is False
                and tool_result.get("error") == "Event conflict detected"):
            conflict = tool_result["conflict"]
            print("⚠️ Conflict detected with existing event:")
            print(f"  Summary: {conflict.get('summary')}")
            print(f"  Time: {conflict.get('start')} to {conflict.get('end')}")

            # Auto-suggest next free slot
            suggested = suggest_next_free_slot(args.get("start_str", ""), DEFAULT_DURATION)
            if suggested.get("success") and suggested.get("suggested_slot"):
                slot = suggested["suggested_slot"]
                print(f"\n💡 Suggested alternative: {slot['start']}")

            choice = input("\nWould you like to (1) create anyway, (2) use suggested time, or (3) enter a new time? ")
            if choice == "1":
                args["ignore_conflict"] = True
                return call_tool("create_new_event", args)
            elif choice == "2" and suggested.get("success") and suggested.get("suggested_slot"):
                slot = suggested["suggested_slot"]
                args["start_str"] = slot["start"]
                end_dt = dateparser.parse(slot["start"]) + timedelta(minutes=DEFAULT_DURATION)
                args["end_str"] = end_dt.isoformat()
                args["ignore_conflict"] = True
                return call_tool("create_new_event", args)
            elif choice == "3":
                new_time = input("New time: ")
                dt = dateparser.parse(new_time, settings={"PREFER_DATES_FROM": "future"})
                if dt:
                    args["start_str"] = dt.isoformat()
                    args["end_str"] = (dt + timedelta(minutes=DEFAULT_DURATION)).isoformat()
                    args["ignore_conflict"] = True
                    return call_tool("create_new_event", args)
                return {"error": "Could not parse the new time."}
            return {"error": "No action taken."}

        return tool_result

    # If Gemini didn't return a tool call, fall back to local parsing
    parsed_event = parse_natural_language_event(command_text)
    if parsed_event.get("summary") and parsed_event.get("start_str"):
        return call_tool("create_new_event", parsed_event)

    return {"error": "Could not understand command. Please try rephrasing."}


# --- Step 13: Event preview formatting ---

def format_event_preview(tool_name, args):
    """Format a preview of what will be created/updated/deleted."""
    if tool_name == "create_new_event":
        lines = ["", "📅 Event Preview:"]
        lines.append(f"  Title:      {args.get('summary', '(untitled)')}")

        # Calendar name lookup
        cal_id = args.get("calendar_id", "primary")
        cal_name = cal_id
        for alias, cid in CALENDAR_ALIASES.items():
            if cid == cal_id:
                cal_name = alias
                break
        lines.append(f"  Calendar:   {cal_name}")

        start = args.get("start_str", "")
        end = args.get("end_str", "")
        if args.get("all_day"):
            lines.append(f"  When:       {start} (all day)")
        else:
            lines.append(f"  When:       {start} to {end}")

        if args.get("location"):
            lines.append(f"  Location:   {args['location']}")
        if args.get("attendees"):
            lines.append(f"  Attendees:  {', '.join(args['attendees'])}")
        if args.get("recurrence"):
            rules = args["recurrence"] if isinstance(args["recurrence"], list) else [args["recurrence"]]
            lines.append(f"  Recurrence: {', '.join(rules)}")
        if args.get("add_video_call"):
            lines.append(f"  Video Call: Google Meet (will be auto-created)")
        if args.get("reminders"):
            reminder_strs = [f"{r.get('minutes', '?')}min ({r.get('method', 'popup')})" for r in args["reminders"]]
            lines.append(f"  Reminders:  {', '.join(reminder_strs)}")
        if args.get("description"):
            lines.append(f"  Notes:      {args['description']}")
        return "\n".join(lines)

    elif tool_name == "update_event":
        lines = ["", "✏️ Update Preview:"]
        if args.get("summary_search"):
            lines.append(f"  Finding:    '{args['summary_search']}'")
        if args.get("new_summary"):
            lines.append(f"  New Title:  {args['new_summary']}")
        if args.get("new_start_str"):
            lines.append(f"  New Start:  {args['new_start_str']}")
        if args.get("new_end_str"):
            lines.append(f"  New End:    {args['new_end_str']}")
        if args.get("new_location"):
            lines.append(f"  Location:   {args['new_location']}")
        if args.get("new_attendees"):
            lines.append(f"  Attendees:  {', '.join(args['new_attendees'])}")
        return "\n".join(lines)

    elif tool_name == "delete_event":
        lines = ["", "🗑️ Delete Preview:"]
        if args.get("summary"):
            lines.append(f"  Event:      '{args['summary']}'")
        if args.get("start_str"):
            lines.append(f"  Near time:  {args['start_str']}")
        return "\n".join(lines)

    return ""


def format_result(tool_name, result):
    """Format the result for display."""
    if not isinstance(result, dict):
        return str(result)

    if tool_name == "list_events" and result.get("success"):
        events = result.get("events", [])
        if not events:
            return "No events found."
        lines = [f"\n📋 Found {len(events)} event(s):\n"]
        for ev in events:
            start = ev.get("start", "")
            summary = ev.get("summary", "(No title)")
            location = ev.get("location", "")
            loc_str = f" @ {location}" if location else ""
            lines.append(f"  • {start} — {summary}{loc_str}")
        return "\n".join(lines)

    if tool_name == "find_free_slots" and result.get("success"):
        slots = result.get("free_slots", [])
        if not slots:
            return "No free slots found."
        lines = [f"\n🟢 Found {len(slots)} free slot(s):\n"]
        for slot in slots[:10]:
            lines.append(f"  • {slot['start']} to {slot['end']} ({slot['duration_minutes']} min)")
        return "\n".join(lines)

    if tool_name == "suggest_next_free_slot" and result.get("success"):
        slot = result.get("suggested_slot", {})
        return f"\n💡 Next free slot: {slot.get('start', '?')} ({slot.get('duration_minutes', '?')} min available)"

    if tool_name == "get_next_event" and result:
        return f"\n📌 Next event: {result.get('summary', '?')} at {result.get('start', '?')} on {result.get('calendar', '?')}"

    return json.dumps(result, indent=2, default=str)


import argparse

def main():
    parser = argparse.ArgumentParser(description="Google Calendar Voice/Text Assistant")
    parser.add_argument("--voice", action="store_true", help="Enable voice input mode")
    cli_args = parser.parse_args()
    use_voice = cli_args.voice

    threading.Thread(target=run_server, daemon=True).start()
    print("=" * 50)
    print("  Google Calendar Assistant")
    print("=" * 50)
    mode = "Voice" if use_voice else "Text"
    print(f"  Mode: {mode} | Server: http://127.0.0.1:8000")
    print(f"  Timezone: {TIMEZONE}")
    print()
    print("  Examples:")
    print("  • Team meeting tomorrow at 10am in Room 204")
    print("  • Gym every MWF at 7am on gym calendar")
    print("  • What do I have tomorrow?")
    print("  • Move my 3pm to 4:30pm")
    print("  • When am I free this afternoon?")
    print("  • Cancel the standup on Wednesday")
    print()

    conversation_history = []

    while True:
        command = None

        if use_voice:
            command = listen_for_command()
            if command:
                print(f"🎤 Heard: {command}")

        if not command:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break
            if user_input.lower() in ("quit", "exit", "q"):
                print("Goodbye!")
                break
            if not user_input:
                continue
            command = user_input

        conversation_history.append(command)

        # Determine command type
        lower = command.lower()
        is_read_only = any(word in lower for word in [
            "what", "show", "list", "schedule", "next event", "free", "busy", "when am i",
        ])
        is_delete = any(word in lower for word in ["delete", "remove", "cancel"])
        is_update = any(word in lower for word in ["move", "update", "change", "edit", "reschedule"])

        if is_read_only:
            # No confirmation needed for queries
            result = gemini_parse(command)
            if result:
                tool_name, args = result
                tool_result = call_tool(tool_name, args)
                print(format_result(tool_name, tool_result))
            else:
                print("Could not understand. Try rephrasing.")
            continue

        # For write operations, parse first, preview, then confirm
        result = gemini_parse(command)
        if not result:
            # Fall back to local parsing for simple creates
            parsed_event = parse_natural_language_event(command)
            if parsed_event.get("summary") and parsed_event.get("start_str"):
                result = ("create_new_event", parsed_event)
            else:
                print("Could not understand. Try rephrasing or provide more details.")
                continue

        tool_name, args = result

        # Show preview
        preview = format_event_preview(tool_name, args)
        if preview:
            print(preview)

        # Confirmation
        if is_delete:
            confirm = input("\n⚠️ Confirm DELETE? (y/n): ")
        elif is_update:
            confirm = input("\nConfirm UPDATE? (y/n): ")
        else:
            confirm = input("\nConfirm CREATE? (y/n): ")

        if confirm.lower() != "y":
            print("Cancelled.")
            continue

        tool_result = call_tool(tool_name, args)

        # Handle conflict
        if (tool_name == "create_new_event"
                and isinstance(tool_result, dict)
                and tool_result.get("success") is False
                and tool_result.get("error") == "Event conflict detected"):
            conflict = tool_result["conflict"]
            print(f"\n⚠️ Conflict with: {conflict.get('summary')} ({conflict.get('start')} to {conflict.get('end')})")

            suggested = suggest_next_free_slot(args.get("start_str", ""), DEFAULT_DURATION)
            if suggested.get("success") and suggested.get("suggested_slot"):
                slot = suggested["suggested_slot"]
                print(f"💡 Suggested alternative: {slot['start']}")

            choice = input("\n(1) Create anyway  (2) Use suggested time  (3) Enter new time  (4) Cancel: ")
            if choice == "1":
                args["ignore_conflict"] = True
                tool_result = call_tool("create_new_event", args)
            elif choice == "2" and suggested.get("success") and suggested.get("suggested_slot"):
                slot = suggested["suggested_slot"]
                args["start_str"] = slot["start"]
                end_dt = dateparser.parse(slot["start"]) + timedelta(minutes=DEFAULT_DURATION)
                args["end_str"] = end_dt.isoformat()
                args["ignore_conflict"] = True
                tool_result = call_tool("create_new_event", args)
            elif choice == "3":
                new_time = input("New time: ")
                dt = dateparser.parse(new_time, settings={"PREFER_DATES_FROM": "future"})
                if dt:
                    args["start_str"] = dt.isoformat()
                    args["end_str"] = (dt + timedelta(minutes=DEFAULT_DURATION)).isoformat()
                    args["ignore_conflict"] = True
                    tool_result = call_tool("create_new_event", args)
                else:
                    print("Could not parse time. Cancelled.")
                    continue
            else:
                print("Cancelled.")
                continue

        print(format_result(tool_name, tool_result))

if __name__ == "__main__":
    main()
