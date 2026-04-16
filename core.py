import os
import json
import datetime
import re
import time
import uuid
import ollama
from pydantic import BaseModel, Field
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dateutil import parser, tz
from rich.console import Console

# Import local configuration
from config import CONFIG

# ====================== DATA MODELS ======================

class EventDetails(BaseModel):
    action: str = Field(..., description="Action to perform: create, list, delete, update, find_slot")
    title: str = Field("", description="Summarized title of the event")
    start: str = Field("", description="ISO format start time")
    end: str = Field("", description="ISO format end time")
    description: str = Field("", description="Detailed notes or description")
    location: str = Field("", description="Physical or virtual location")
    attendees: list[str] = Field(default_factory=list, description="List of email addresses")
    add_meeting: bool = Field(False, description="Whether to add a Google Meet link")
    search_query: str = Field("", description="Query string for finding events to delete or update")
    recurrence: list[str] = Field(default_factory=list, description="RFC5545 RRULE string")
    reminders_minutes: list[int] = Field(default_factory=lambda: [15], description="Minutes before event for popup reminders")

class SessionState:
    last_event: EventDetails = None
    last_raw_input: str = ""

STATE = SessionState()
_console = Console()
SCOPES = ['https://www.googleapis.com/auth/calendar']

# ====================== GOOGLE SERVICE ======================

def get_calendar_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

# ====================== CORE LOGIC ======================

def parse_natural_language(text: str, context: EventDetails = None) -> EventDetails:
    """Uses Ollama to parse natural language into a structured EventDetails object."""
    now_dt = datetime.datetime.now()
    today_str = now_dt.strftime('%A, %B %d, %Y')
    
    # Generate a reference calendar for the next 14 days to prevent LLM date calculation errors
    calendar_ref = []
    for i in range(14):
        d = now_dt + datetime.timedelta(days=i)
        calendar_ref.append(d.strftime('%A (%Y-%m-%d)'))
    calendar_str = "\n".join(calendar_ref)

    system_prompt = f"""You are LazyScheduler, a precision calendar assistant.
Today is {today_str}. The user's timezone is {CONFIG.timezone}.

Upcoming Days Reference:
{calendar_str}

TASK: Convert user requests into a clean JSON object. 
Use the 'Upcoming Days Reference' to resolve relative dates like "this Sunday" or "next Friday".

JSON SCHEMA:
{{
  "action": "create" | "list" | "delete" | "update" | "find_slot",
  "title": "string",
  "start": "ISO8601 string",
  "end": "ISO8601 string",
  "description": "string",
  "location": "string",
  "attendees": ["email@domain.com", "name without email"],
  "add_meeting": boolean,
  "search_query": "string (used for delete/update search)",
  "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
  "reminders_minutes": [integer]
}}

EXAMPLES:
1. "Meet John at Starbucks tomorrow at 2pm" -> {{"action": "create", "title": "Coffee with John", "start": "2026-04-17T14:00:00", "location": "Starbucks"}}
2. "Cancel my dentist appointment" -> {{"action": "delete", "search_query": "dentist"}}
3. "Move team sync to 5 PM" -> {{"action": "update", "search_query": "team sync", "start": "2026-04-16T17:00:00"}}
4. "Lunch with parents online" -> {{"action": "create", "title": "Lunch with parents", "add_meeting": true, "location": ""}}

RULES:
- If duration is missing, default to {CONFIG.default_duration} minutes.
- If 'action' is update/delete, provide a 'search_query'.
- "This [Day]" always refers to the upcoming occurrence of that day.
- ONLINE MEETINGS: If user mentions "online", "virtual", "video call", "google meet", or "zoom", set "add_meeting" to true.
- LOCATION: If "add_meeting" is true, do NOT put "Google Meet" or "Online" in the location field. Leave it empty unless a physical place is also mentioned.
- Return ONLY the JSON object. No prose.
"""
    
    messages = [{"role": "system", "content": system_prompt}]
    
    if context:
        messages.append({"role": "assistant", "content": f"Context: {context.json()}"})
        messages.append({"role": "user", "content": f"Correction: {text}"})
    else:
        messages.append({"role": "user", "content": text})

    with _console.status(f"[bold yellow]Analyzing intent with {CONFIG.model}...", spinner="dots"):
        response = ollama.chat(model=CONFIG.model, messages=messages, format='json')
    
    try:
        data = json.loads(response['message']['content'])
    except Exception as e:
        _console.print(f"[red]Error parsing LLM response: {e}[/red]")
        raise ValueError("Invalid AI response")

    # Timezone-aware datetime parsing
    local_tz = tz.gettz(CONFIG.timezone)
    
    def finalize_dt(dt_str):
        if not dt_str: return None
        try:
            parsed = parser.parse(dt_str)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=local_tz)
            return parsed
        except: return None

    start_dt = finalize_dt(data.get('start'))
    if not start_dt:
        if data.get('action') == 'list':
            start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=local_tz)
        else:
            start_dt = now_dt.replace(tzinfo=local_tz)
    
    if data.get('end'):
        end_dt = finalize_dt(data['end']) or (start_dt + datetime.timedelta(minutes=CONFIG.default_duration))
    else:
        if data.get('action') == 'list':
            end_dt = start_dt.replace(hour=23, minute=59, second=59)
        else:
            end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)

    if end_dt <= start_dt:
        end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)

    # Hardened Email Extraction
    valid_attendees = []
    invalid_notes = []
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    
    raw_attendees = data.get('attendees', [])
    if isinstance(raw_attendees, str): raw_attendees = [raw_attendees]
    
    for entry in raw_attendees:
        entry = entry.strip()
        if re.match(email_regex, entry):
            valid_attendees.append(entry)
        else:
            invalid_notes.append(entry)

    desc = data.get('description', '')
    if invalid_notes:
        note = f"\n\nAttendees (to be manually added): {', '.join(invalid_notes)}"
        desc = (desc + note).strip()

    return EventDetails(
        action=data.get('action', 'create'),
        title=data.get('title') or "New Event",
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        description=desc,
        location=data.get('location', ''),
        attendees=valid_attendees,
        add_meeting=bool(data.get('add_meeting')),
        search_query=data.get('search_query', ''),
        recurrence=data.get('recurrence', []),
        reminders_minutes=data.get('reminders_minutes', [15])
    )

def list_upcoming_events(service, start_time: str, end_time: str):
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=start_time, timeMax=end_time,
            singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        _console.print(f"[red]❌ Calendar List Error: {e}[/red]")
        return []

def find_event(service, search_query: str):
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        
        events_result = service.events().list(
            calendarId='primary', q=search_query, timeMin=start_of_day,
            maxResults=5, singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        _console.print(f"[red]❌ Search Error: {e}[/red]")
        return []

def delete_event(service, event_id: str):
    return service.events().delete(calendarId='primary', eventId=event_id).execute()

def update_event(service, event_id: str, new_data: EventDetails):
    event = service.events().get(calendarId='primary', eventId=event_id).execute()
    if new_data.title: event['summary'] = new_data.title
    event['start'] = {'dateTime': new_data.start, 'timeZone': CONFIG.timezone}
    event['end'] = {'dateTime': new_data.end, 'timeZone': CONFIG.timezone}
    if new_data.description: event['description'] = new_data.description
    if new_data.location: event['location'] = new_data.location
    return service.events().update(calendarId='primary', eventId=event_id, body=event).execute()

def create_event(service, event: EventDetails):
    event_body = {
        'summary': event.title,
        'description': event.description,
        'location': event.location,
        'start': {'dateTime': event.start, 'timeZone': CONFIG.timezone},
        'end':   {'dateTime': event.end,   'timeZone': CONFIG.timezone},
        'attendees': [{'email': e} for e in event.attendees],
        'reminders': {
            'useDefault': False,
            'overrides': [{'method': 'popup', 'minutes': m} for m in event.reminders_minutes]
        }
    }
    if event.add_meeting:
        event_body['conferenceData'] = {
            'createRequest': {'requestId': str(uuid.uuid4()), 'conferenceSolutionKey': {'type': 'hangoutsMeet'}}
        }
    if event.recurrence:
        event_body['recurrence'] = event.recurrence
    for attempt in range(3):
        try:
            return service.events().insert(
                calendarId='primary', 
                body=event_body, 
                conferenceDataVersion=1 if event.add_meeting else 0
            ).execute()
        except Exception as e:
            if attempt < 2 and any(k in str(e).lower() for k in ["eof", "protocol", "timeout"]):
                time.sleep(1.5)
                continue
            raise e

def check_conflicts(service, start_time: str, end_time: str):
    try:
        body = {"timeMin": start_time, "timeMax": end_time, "items": [{"id": "primary"}]}
        result = service.freebusy().query(body=body).execute()
        return result['calendars']['primary']['busy']
    except: return []

def find_free_slots(service, start_search: str, duration_mins=None):
    if duration_mins is None: duration_mins = CONFIG.default_duration
    try:
        search_dt = parser.parse(start_search)
        max_search = search_dt + datetime.timedelta(days=7)
        busy_data = service.freebusy().query(body={
            "timeMin": search_dt.isoformat(), 
            "timeMax": max_search.isoformat(), 
            "items": [{"id": "primary"}]
        }).execute()
        busy = busy_data['calendars']['primary']['busy']
        free_slots = []
        current_dt = search_dt
        while len(free_slots) < 3 and current_dt < max_search:
            if current_dt.hour < CONFIG.working_start: 
                current_dt = current_dt.replace(hour=CONFIG.working_start, minute=0)
            if current_dt.hour >= CONFIG.working_end: 
                current_dt = (current_dt + datetime.timedelta(days=1)).replace(hour=CONFIG.working_start, minute=0)
            end_dt = current_dt + datetime.timedelta(minutes=duration_mins)
            conflict = False
            for b in busy:
                b_start, b_end = parser.parse(b['start']), parser.parse(b['end'])
                if (current_dt < b_end) and (end_dt > b_start):
                    current_dt = b_end; conflict = True; break
            if not conflict:
                free_slots.append(current_dt)
                current_dt = end_dt + datetime.timedelta(minutes=15)
        return free_slots
    except Exception as e:
        _console.print(f"[dim red]Free slot calc error: {e}[/dim red]")
        return []
