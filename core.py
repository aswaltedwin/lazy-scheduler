import os
import json
import datetime
import re
import time
import uuid
import ollama
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dateutil import parser, tz
from rich.console import Console

# Import local configuration
from config import CONFIG, EventDetails

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

# ====================== IMPROVED PARSING ======================

def parse_natural_language(text: str, context: EventDetails = None) -> EventDetails:
    today = datetime.datetime.now().strftime('%Y-%m-%d %A')
    context_str = ""
    if context:
        context_str = f"\nUser previously proposed: {context.json()}\nNow says: \"{text}\". If it's a correction, update the JSON. If new request, start fresh."

    prompt = f"""You are LazyScheduler, a smart calendar assistant.{context_str}
Return ONLY valid JSON. Rules: Today is {today}. Timezone is {CONFIG.timezone}.

JSON structure:
{{
  "action": "create | list | delete | update | find_slot",
  "title": "{context.title if context else 'Short title'}",
  "start": "2026-04-16T16:00:00",
  "end": "2026-04-16T16:45:00",
  "description": "",
  "location": "",
  "attendees": [],
  "add_meeting": true | false,
  "search_query": "event name",
  "recurrence": []
}}
"""
    with _console.status(f"[bold yellow]Thinking with {CONFIG.model}...", spinner="dots"):
        response = ollama.chat(model=CONFIG.model, messages=[{'role': 'user', 'content': prompt + f'\nRequest: "{text}"'}], format='json')
    data = json.loads(response['message']['content'])

    # Datetime handling
    start_dt = parser.parse(data['start'])
    local_tz = tz.gettz(CONFIG.timezone)
    if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=local_tz)

    if data.get('end'):
        end_dt = parser.parse(data['end'])
        if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=local_tz)
    else:
        end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)

    if end_dt <= start_dt:
        end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)

    # Email extraction logic
    valid_attendees = []
    invalid_notes = []
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    for entry in data.get('attendees', []):
        entry = entry.strip()
        if re.match(email_regex, entry):
            valid_attendees.append(entry)
        else:
            invalid_notes.append(entry)

    description = data.get('description', '')
    if invalid_notes:
        note = f"\nNote: Attendees without emails: {', '.join(invalid_notes)}"
        description = (description + note).strip()

    return EventDetails(
        action=data.get('action', 'create'),
        title=data.get('title', 'Untitled Event'),
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        description=description,
        location=data.get('location', ''),
        attendees=valid_attendees,
        add_meeting=data.get('add_meeting', False),
        search_query=data.get('search_query', ''),
        recurrence=data.get('recurrence', [])
    )

# ====================== API ACTIONS ======================

def list_upcoming_events(service, start_time: str, end_time: str):
    """Returns a list of event items."""
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=start_time, timeMax=end_time,
            singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        _console.print(f"[red]❌ List failed: {e}[/red]")
        return []

def find_event(service, search_query: str):
    """Searches for an event starting from the BEGINNING of the current day."""
    try:
        # Wider window: Search from 00:00 today to 7 days from now
        now = datetime.datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone().isoformat()
        
        events_result = service.events().list(
            calendarId='primary', q=search_query, timeMin=start_of_day,
            maxResults=5, singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except: return []

def delete_event(service, event_id: str):
    return service.events().delete(calendarId='primary', eventId=event_id).execute()

def update_event(service, event_id: str, new_data: EventDetails):
    # Fetch existing first so we don't wipe description/location
    event = service.events().get(calendarId='primary', eventId=event_id).execute()
    event['summary'] = new_data.title
    event['start'] = {'dateTime': new_data.start, 'timeZone': CONFIG.timezone}
    event['end'] = {'dateTime': new_data.end, 'timeZone': CONFIG.timezone}
    return service.events().update(calendarId='primary', eventId=event_id, body=event).execute()

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
        busy = service.freebusy().query(body={"timeMin": search_dt.isoformat(), "timeMax": max_search.isoformat(), "items": [{"id": "primary"}]}).execute()['calendars']['primary']['busy']
        
        free_slots = []
        current_dt = search_dt
        while len(free_slots) < 3 and current_dt < max_search:
            if current_dt.hour < CONFIG.working_start: current_dt = current_dt.replace(hour=CONFIG.working_start, minute=0)
            if current_dt.hour >= CONFIG.working_end: current_dt = (current_dt + datetime.timedelta(days=1)).replace(hour=CONFIG.working_start, minute=0)
            
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
    except: return []

def create_event(service, event: EventDetails):
    event_body = {
        'summary': event.title,
        'description': event.description,
        'location': event.location,
        'start': {'dateTime': event.start, 'timeZone': CONFIG.timezone},
        'end':   {'dateTime': event.end,   'timeZone': CONFIG.timezone},
        'attendees': [{'email': e} for e in event.attendees],
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
                calendarId='primary', body=event_body, 
                conferenceDataVersion=1 if event.add_meeting else 0
            ).execute()
        except Exception as e:
            if attempt < 2 and any(k in str(e).lower() for k in ["eof", "protocol", "timeout"]): time.sleep(1); continue
            raise e
