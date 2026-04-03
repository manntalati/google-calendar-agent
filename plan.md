# Google Calendar Agent - Implementation Plan

## Goal
Build the most capable natural-language Google Calendar agent that can handle any type of meeting, event, or idea and get it onto the right calendar with the right details — every time.

---

## Phase 1: Architecture Cleanup
*Fix the foundation before adding features.*

### 1.1 Remove self-calling HTTP loop
- **Current:** `agent_handle_command()` POSTs to `localhost:8000/invoke` to call its own functions
- **Change:** Call `create_new_event()`, `delete_event()`, etc. directly from the agent logic
- **Why:** Eliminates latency, removes a failure point, simplifies the code

### 1.2 Unify NLP pipeline — Gemini-first
- **Current:** Regex-based `parse_natural_language_event()` with Gemini as fallback
- **Change:** Route all user input through Gemini with a structured system prompt containing:
  - Today's date and current time
  - User's timezone
  - Available calendars (aliases + IDs)
  - Known contacts (name → email map)
  - All available tools with full parameter schemas
- **Why:** Regex breaks on anything non-trivial; Gemini handles ambiguity, relative dates, and multi-part commands naturally
- **Keep:** `dateparser` as a validation/normalization layer after Gemini output

### 1.3 Configurable calendar aliases
- **Current:** Hardcoded `CALENDAR_ALIASES` dict in `voice_mcp.py`
- **Change:** Move to `config.json` with structure:
  ```json
  {
    "timezone": "America/Chicago",
    "default_calendar": "primary",
    "calendars": { "alias": "calendar_id", ... },
    "contacts": { "name": "email", ... },
    "default_reminder_minutes": 10,
    "default_event_duration_minutes": 60
  }
  ```
- **Why:** Adding a calendar or contact shouldn't require code changes

### 1.4 Proper error propagation
- **Current:** `create_new_event()` catches all exceptions and returns `{"success": False}` with no detail
- **Change:** Return the actual error message from the Google API in all failure responses
- **Files:** `google_calendar.py` — all try/except blocks

---

## Phase 2: Core Feature Completeness
*The features needed to handle real daily calendar usage.*

### 2.1 List events by date range
- **New function:** `list_events(calendar_id, start_str, end_str)`
- **Enables:** "What's on my calendar tomorrow?", "Show me next week", "Am I free Friday afternoon?"
- **Returns:** List of events with summary, start, end, location, attendees
- **File:** `google_calendar.py`

### 2.2 Update/edit existing events
- **New function:** `update_event(calendar_id, event_id, **fields_to_update)`
- **Enables:** "Move my 3pm to 4pm", "Add John to the team meeting", "Change the location to Room 204"
- **Approach:** Fetch event by ID or fuzzy match (reuse delete's matching logic), then PATCH
- **File:** `google_calendar.py`

### 2.3 Recurrence support
- **Add `recurrence` parameter** to `create_new_event()`
- **Format:** RRULE strings (Google Calendar native format)
- **Gemini handles translation:** "every MWF" → `RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR`
- **Examples that should work:**
  - "Gym every Monday Wednesday Friday at 7am"
  - "Weekly team standup every Tuesday at 10am"
  - "Monthly rent reminder on the 1st"
  - "Daily standup for the next 2 weeks"
- **File:** `google_calendar.py`

### 2.4 Location support
- **Add `location` parameter** to `create_new_event()`
- **Enables:** "Meeting at Siebel 1404", "Dinner at Nando's on Green Street"
- **File:** `google_calendar.py`

### 2.5 Google Meet / video conferencing
- **Add `add_video_call` boolean** to `create_new_event()`
- **Uses:** `conferenceData` field with `createRequest` to auto-generate a Meet link
- **Enables:** "Set up a video call with the team tomorrow at 2pm"
- **File:** `google_calendar.py`

### 2.6 Reminders / notifications
- **Add `reminders` parameter** to `create_new_event()`
- **Format:** List of `{"method": "popup"|"email", "minutes": int}`
- **Default:** Configurable in `config.json` (e.g., 10-minute popup)
- **Enables:** "Remind me 30 minutes before", "Set an email reminder 1 hour before"
- **File:** `google_calendar.py`

### 2.7 Color coding
- **Add `color_id` parameter** to `create_new_event()`
- **Enables:** Gemini can auto-assign colors based on event type (work = blue, personal = green, etc.)
- **File:** `google_calendar.py`

---

## Phase 3: Smart Agent Capabilities
*Make the agent intelligent, not just functional.*

### 3.1 Free/busy lookup
- **New function:** `find_free_slots(start_str, end_str, duration_minutes)`
- **Uses:** Google Calendar FreeBusy API
- **Enables:** "When am I free tomorrow afternoon?", "Find me a 2-hour block this week"
- **File:** `google_calendar.py`

### 3.2 Intelligent conflict resolution
- **Current:** Detects conflicts with a fixed ±1hr window, asks user to override or pick a new time
- **Change:**
  - Use actual event durations for overlap detection (not ±1hr)
  - When conflict detected, auto-suggest the next available slot
  - Support "schedule around" logic — find the best fit in a given range
- **File:** `google_calendar.py` (conflict detection), `voice_mcp.py` (agent logic)

### 3.3 Duration inference
- **Add smart defaults** based on event type (parsed by Gemini):
  - "lunch" → 1 hour
  - "coffee" → 30 minutes
  - "meeting" → 30 minutes
  - "workout" / "gym" → 1 hour
  - "class" / "lecture" → 50 minutes
  - "doctor" / "appointment" → 1 hour
  - "flight" → pulled from description or ask
- **Implementation:** Part of the Gemini system prompt — instruct it to set `end_str` based on event type when user doesn't specify duration

### 3.4 Multi-event / batch operations
- **Enables:**
  - "Cancel all my meetings tomorrow"
  - "Move all Friday events to Monday"
  - "What's my busiest day this week?"
- **Implementation:** Combine `list_events` + `update_event`/`delete_event` in agent logic

### 3.5 Contacts resolution
- **Load contacts from `config.json`** (name → email mapping)
- **Gemini resolves names:** "with Sarah" → looks up email from contacts map
- **Fallback:** If name not found, ask user for email and offer to save to contacts
- **File:** `voice_mcp.py` (agent logic), `config.json`

---

## Phase 4: Input & Interface
*Make it easy to interact with the agent.*

### 4.1 Re-enable voice input with toggle
- **Current:** `listen_for_command()` is commented out
- **Change:** Add `--voice` / `--text` CLI flag, default to text
- **File:** `voice_mcp.py` (main loop)

### 4.2 Conversational multi-turn
- **Current:** `AgentContext` tracks one pending field
- **Change:** Support multi-turn conversations:
  - "Schedule a meeting" → "When?" → "Tomorrow at 2" → "What calendar?" → "Work" → Created
  - Context carries forward across turns
- **Implementation:** Expand `AgentContext` to track full conversation state and missing required fields

### 4.3 Confirmation with event preview
- **Before creating/deleting:** Show a formatted preview:
  ```
  Event: Team Standup
  Calendar: CS 357
  When: Tuesday, Apr 8, 2026 at 10:00 AM - 10:30 AM
  Location: Zoom
  Recurrence: Every Tuesday
  Attendees: sarah@example.com, john@example.com
  Reminder: 10 min before
  ```
- **Then ask:** "Create this event? (y/n)"

---

## Phase 5: External Integrations
*Pull events, deadlines, and scheduling cues from external platforms automatically.*

### 5.1 PrairieTest → Calendar sync
- **What:** Scrape or pull upcoming exam/test slots from PrairieTest and create calendar events automatically
- **Approach:**
  - PrairieTest doesn't have a public API — use browser automation (Playwright/Selenium) or session-cookie-based HTTP requests to fetch scheduled test reservations
  - Authenticate via university SSO (store session token securely)
  - Parse the test list page for: course name, test name, date/time, location (CBTF room), duration
  - Map course → calendar using existing calendar aliases (e.g., "cs357" → CS357 calendar)
  - Create events with: summary = "[EXAM] CS 357 Midterm 2", location = "CBTF", duration from PrairieTest slot, reminder = 1 day + 1 hour before
  - **Dedup:** Before creating, check if an event with the same summary and time already exists to avoid duplicates on re-sync
- **New function:** `sync_prairietest()`
- **Config additions:** `prairietest_url`, `university_credentials` (or token path)
- **File:** New `integrations/prairietest.py`

### 5.2 PrairieLearn deadlines → Calendar sync
- **What:** Pull assignment/homework deadlines from PrairieLearn and add them as all-day events or timed reminders
- **Approach:**
  - PrairieLearn has a student-facing API and also exposes deadlines on the course assessments page
  - Authenticate via university SSO (same session as PrairieTest if possible)
  - For each enrolled course, fetch assessments list with: assessment name, type (homework/quiz/exam), due date, late date (if applicable)
  - Create events: summary = "[DUE] CS 374 HW 5", time = deadline time, calendar = course calendar
  - For homeworks: create as timed event at the deadline, with reminders at 1 day and 2 hours before
  - For exams: defer to PrairieTest sync (avoid duplicates)
  - **Incremental sync:** Track last sync time, only fetch/create new or changed deadlines
- **New function:** `sync_prairielearn_deadlines()`
- **Config additions:** `prairielearn_url`, `prairielearn_courses` (list of course instance IDs to sync)
- **File:** New `integrations/prairielearn.py`

### 5.3 Slack → Calendar event detection
- **What:** Monitor Slack messages for scheduling-related content and surface them as calendar event suggestions
- **Approach:**
  - Use Slack Bolt SDK (Python) to connect via a Slack App (Bot Token + Socket Mode for real-time, or periodic fetch via Web API)
  - Monitor configured channels and DMs for messages that contain scheduling signals:
    - Explicit: "let's meet", "schedule a call", "are you free", "can we do Tuesday at 3?"
    - Links: Zoom/Meet/Teams links paired with a time
    - Reactions: e.g., a custom `:calendar:` emoji reaction as a manual trigger
  - When a scheduling signal is detected, pass the message text through Gemini to extract: event summary, proposed time(s), attendees (from Slack usernames → email via Slack API), location/link
  - Present the parsed event to the user for confirmation before creating
  - **Modes:**
    - **Passive/poll:** Periodically scan recent messages in configured channels (e.g., every 15 minutes)
    - **Active/real-time:** Socket Mode listener that triggers on each new message
    - **Manual:** User says "check Slack for events" and agent scans last N hours
  - **Dedup:** Track processed message IDs to avoid re-suggesting the same event
- **New function:** `scan_slack_for_events(channels, lookback_hours)`
- **Config additions:** `slack_bot_token`, `slack_channels` (list of channel IDs to monitor), `slack_mode` ("manual" | "poll" | "realtime")
- **File:** New `integrations/slack.py`

---

## Phase 6: Robustness & Quality
*Make it reliable.*

### 6.1 Input validation
- Validate all dates are in the future (or warn for past dates)
- Validate calendar IDs exist before attempting operations
- Validate attendee emails are well-formed

### 6.2 Logging
- **Replace all `print()` calls** with Python `logging` module
- Log levels: DEBUG for API calls, INFO for operations, WARNING for conflicts, ERROR for failures
- **File:** All files

### 6.3 Tests
- **Unit tests** for:
  - `normalize_event_time()` with edge cases (DST, all-day, missing end)
  - `events_overlap()` boundary conditions
  - `extract_calendar_id()` alias matching
  - Fuzzy matching thresholds for deletion
- **Integration tests** for:
  - Create → List → Update → Delete lifecycle
  - Conflict detection and resolution
  - Recurrence creation and expansion
- **File:** `tests/` directory

### 6.4 Timezone handling
- **Current:** Hardcoded `America/Chicago`
- **Change:** Pull from `config.json`, pass through all date operations
- Support explicit timezone in commands: "3pm EST", "10am Pacific"

---

## Implementation Order

| Step | What | Files Modified | Depends On |
|------|------|---------------|------------|
| 1 | Config file (`config.json`) | new file | — |
| 2 | Error propagation fixes | `google_calendar.py` | — |
| 3 | Remove HTTP self-call loop | `voice_mcp.py` | — |
| 4 | `list_events()` function | `google_calendar.py` | — |
| 5 | `update_event()` function | `google_calendar.py` | — |
| 6 | Add location, recurrence, reminders, color, Meet to `create_new_event()` | `google_calendar.py` | — |
| 7 | Gemini-first NLP pipeline with structured system prompt | `voice_mcp.py` | 1, 4, 5, 6 |
| 8 | `find_free_slots()` function | `google_calendar.py` | — |
| 9 | Smart conflict resolution | `google_calendar.py`, `voice_mcp.py` | 4, 8 |
| 10 | Duration inference (in Gemini prompt) | `voice_mcp.py` | 7 |
| 11 | Contacts resolution | `voice_mcp.py`, `config.json` | 1, 7 |
| 12 | Multi-turn conversation | `voice_mcp.py` | 7 |
| 13 | Event preview before confirmation | `voice_mcp.py` | 7 |
| 14 | Voice toggle | `voice_mcp.py` | — |
| 15 | PrairieTest sync | new `integrations/prairietest.py` | 1, 6, 7 |
| 16 | PrairieLearn deadline sync | new `integrations/prairielearn.py` | 1, 6, 7 |
| 17 | Slack event detection | new `integrations/slack.py` | 1, 7 |
| 18 | Logging overhaul | all files | — |
| 19 | Timezone from config | `google_calendar.py`, `config.json` | 1 |
| 20 | Input validation | `google_calendar.py` | — |
| 21 | Tests | `tests/` | all above |

---

## Success Criteria
When complete, the agent should handle all of these without breaking a sweat:

- "Schedule a team meeting tomorrow at 2pm in Room 204 with Sarah and John, add a Google Meet link, and remind me 15 minutes before"
- "Gym every Monday Wednesday Friday at 7am on my gym calendar"
- "What do I have tomorrow?"
- "Move my 3pm meeting to 4:30pm"
- "Cancel all meetings on Friday"
- "When am I free next Tuesday afternoon?"
- "Monthly rent reminder on the 1st of every month, all day, on my personal calendar"
- "Lunch with Jake on Thursday" (auto: 1hr duration, resolves Jake's email, primary calendar)
- "Delete the standup on Wednesday"
- "What's my busiest day this week?"
- "Sync my PrairieTest exams" (pulls all scheduled CBTF slots into course calendars)
- "Pull my PrairieLearn deadlines" (adds all HW/quiz due dates with reminders)
- "Check Slack for anything I need to schedule" (scans channels, surfaces event suggestions)
