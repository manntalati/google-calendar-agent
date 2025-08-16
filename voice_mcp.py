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

@app.get('/tools')
def list_tools():
  return {
    "tools": [
        {"name": "create_event", "description": "Create a new calendar event"},
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

def listen_for_command():
    r = sr.Recognizer()
    with sr.Microphone() as source:
        print("Listening for your command...")
        audio = r.listen(source)
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

    return event_data
    
client = Client(api_key=os.getenv("GOOGLE_API_KEY"))

create_event_declaration = {
    "name": "create_event",
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
        result = agent_handle_command(command)
        print("Result:", result)

if __name__ == "__main__":
    main()