import threading
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import speech_recognition as sr
import requests
from google.genai import Client, types
from google_calendar import create_new_event, get_next_event
from dotenv import load_dotenv
import os
import re
import dateparser
from datetime import timedelta

load_dotenv()

app = FastAPI(title="Calendar Voice Assistant", version="1.0.0")

class ToolCall(BaseModel):
    tool: str
    args: dict

CALENDAR_ALIASES = {
    "ameren": "8160dc3d8b6b84c62efadca0c8eef8e2d0d62a45c6792c7e2b10ed46c1ab802a@group.calendar.google.com",
    "cs357": "d6340b62c24a1d096de0b7c14484d9abeb6b63a90594dfb93c38adc7d929ab9b@group.calendar.google.com",
    "cs374": "9f7456dbab7bfe75cf32bbe1982258c9b25e8bfe1d081f7135bfdb9e5dd194a2@group.calendar.google.com",
    "cube": "6fef44e621c2e43a97f112abc3972767b201dfff869348528b8cace8a4b9d13a@group.calendar.google.com",
    "Family": "family09523738390342103298@group.calendar.google.com",
    "gym": "cbd9ff2375b8c8c457bf2d41b5d24e9148ad7f0a9b60ab5d56194dd9c062d9e2@group.calendar.google.com",
    "other stuff": "fa356ec657f24bccdac2c639cb3a66b04ee8a9785ded2b3ba6a5c6495f47eb09@group.calendar.google.com",
    "Personal": "4574979994ed4e05c27f544f2f639ae0e47747860a2954d092a4c0609f4fc486@group.calendar.google.com",
    "stat410": "68b970f4ac1bcbc7e1b4b041da37c32fc2a527126c23faa0eea5276c14a03333@group.calendar.google.com",
    "stat425": "926660ce2c937a562f8883195ecdf0a5df538553242aa521bcb2ab90b659e27c@group.calendar.google.com",
}

@app.get('/tools')
def list_tools():
  return {
    "tools": [
        {"name": "create_new_event", "description": "Create a new calendar event"},
        {"name": "get_next_event", "description": "Get the next event"}
    ]
  }

@app.post('/invoke')
def invoke_tool(call: ToolCall):
    try:
        if call.tool == 'create_new_event':
            return create_new_event(**call.args)
        elif call.tool == 'get_next_event':
            return get_next_event(**call.args)
        else:
            raise HTTPException(status_code=400, detail="Unknown tool")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
def run_server():
    uvicorn.run(app, host="0.0.0.0", port=8000)

def extract_calendar_id(command_text: str):
    for alias, cal_id in CALENDAR_ALIASES.items():
        if alias.lower() in command_text.lower():
            cleaned_text = re.sub(rf"\b{alias}\b( calendar)?", "", command_text, flags=re.IGNORECASE)
            return cal_id, cleaned_text.strip()
    return "primary", "mann.talati@gmail.com"

def listen_for_command():
    r = sr.Recognizer()
    with sr.Microphone() as source:
        print("Listening for your command... (you have ~15 seconds)")
        r.adjust_for_ambient_noise(source, duration=1)
        audio = r.listen(source, timeout=10, phrase_time_limit=15)
    try:
        return r.recognize_google(audio)
    except:
        return None
    
def parse_natural_language_event(command_text):
    event_data = {
        "calendar_id": "primary",
        "summary": "",
        "start_str": "",
        "end_str": "",
        "description": "",
        "attendees": [],
        "all_day": False
    }

    attendees_match = re.findall(r'with ([A-Za-z ,]+)', command_text, re.IGNORECASE)
    if attendees_match:
        attendees = [a.strip() for a in attendees_match[0].split(',')]
        event_data["attendees"] = attendees
        command_text = re.sub(r'with [A-Za-z ,]+', '', command_text, flags=re.IGNORECASE)

    if "all day" in command_text.lower():
        event_data["all_day"] = True

    dt = dateparser.parse(command_text, settings={'PREFER_DATES_FROM': 'future'})
    if dt:
        event_data["start_str"] = dt.isoformat()
        if not event_data["all_day"]:
            event_data["end_str"] = (dt + timedelta(hours=1)).isoformat()

    summary = re.sub(r'at \d{1,2}(:\d{2})?\s*(am|pm)?', '', command_text, flags=re.IGNORECASE)
    summary = summary.strip()
    if summary:
        event_data["summary"] = summary

    calendar_id, command_text = extract_calendar_id(command_text)
    event_data["calendar_id"] = calendar_id

    return event_data
    
client = Client(api_key=os.getenv("GOOGLE_API_KEY"))

create_event_declaration = {
    "name": "create_new_event",
    "description": "Create a new Google Calendar event.",
    "parameters": {
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string"},
            "summary": {"type": "string"},
            "start_str": {"type": "string"},
            "end_str": {"type": "string"},
            "description": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
            "all_day": {"type": "boolean"}
        },
        "required": ["calendar_id", "summary", "start_str"]
    }
}

get_next_event_declaration = {
    "name": "get_next_event",
    "description": "Get the next upcoming event.",
    "parameters": {"type": "object", "properties": {}}
}

tools = types.Tool(function_declarations=[create_event_declaration, get_next_event_declaration])
config = types.GenerateContentConfig(tools=[tools])

def agent_handle_command(command_text):
    parsed_event = parse_natural_language_event(command_text)
    if parsed_event.get("summary") and parsed_event.get("start_str"):
        r = requests.post("http://127.0.0.1:8000/invoke",
                          json={"tool": "create_new_event", "args": parsed_event})
        return r.json()

    contents = [types.Content(role="user", parts=[types.Part(text=command_text)])]
    response = client.models.generate_content(model="gemini-2.5-flash", contents=contents, config=config)
    if not response.candidates:
        return {"error": "No response from Gemini"}
    candidate = response.candidates[0]
    func_call = candidate.content.parts[0].function_call
    if func_call:
        tool_name = func_call.name
        args = func_call.args
        r = requests.post("http://127.0.0.1:8000/invoke", json={"tool": tool_name, "args": args})
        return r.json()
    else:
        return {"message": candidate.content.parts[0].text}
    
def main():
    # Start MCP server
    threading.Thread(target=run_server, daemon=True).start()
    print("🎤 Voice Assistant server running at http://127.0.0.1:8000")
    print("📱 Web interface available at http://127.0.0.1:8000")
    print("🔧 API endpoints available at http://127.0.0.1:8000/api/")
    print("\n🎯 Voice Commands:")
    print("- Say 'Team meeting tomorrow at 10am'")
    print("- Say 'Lunch with Sarah on Friday at noon'")
    print("- Say 'Doctor appointment next Tuesday at 3pm'")
    print("\nPress Ctrl+C to stop the server")
    
    while True:
        command = listen_for_command()
        if not command:
            print("Could not understand command.")
            continue
        print(f"✅ Heard: {command}")
        confirm = input("Should I create this event? (y/n): ")
        if confirm.lower() != "y":
            continue
        result = agent_handle_command(command)
        print("Result:", result)

if __name__ == "__main__":
    main()