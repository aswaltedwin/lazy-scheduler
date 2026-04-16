import os
import json
import datetime
import re
import time
import uuid
import logging
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

# ====================== LOGGING SETUP ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("scheduler.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("LazyScheduler")

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
    duration_mins: int = Field(0, description="Minimum duration in minutes for finding a free slot")


class SessionState:
    last_event: EventDetails = None
    last_raw_input: str = ""

STATE = SessionState()
_console = Console()
SCOPES = ['https://www.googleapis.com/auth/calendar']

# ====================== SECURITY & SANITATION ======================

class Sanitizer:
    @staticmethod
    def sanitize_input(text: str) -> str:
        """Protect against excessively long inputs or basic prompt injection patterns."""
        if len(text) > 500:
            logger.warning(f"Input truncated due to excessive length: {len(text)} chars")
            text = text[:500]
        
        # Simple detection for obvious injection attempts
        injection_patterns = [r"ignore previous instructions", r"system prompt", r"override", r"you are now"]
        for pattern in injection_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                logger.warning(f"Potential prompt injection detected: '{pattern}'")
                # We don't block yet, but we log the attempt for potential filtering
        
        return text.strip()

# ====================== GOOGLE SERVICE ======================

def get_calendar_service():
    """Initializes and returns the Google Calendar API service."""
    creds = None
    try:
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
    except Exception as e:
        logger.error(f"Failed to initialize Calendar service: {e}")
        raise

# ====================== REFACTORED AI PARSING ======================

def _get_prompt_metadata():
    """Calculates all time-related variables for the prompt."""
    now_dt = datetime.datetime.now()
    today_str = now_dt.strftime('%A, %B %d, %Y')
    
    # Calculate Sunday-Saturday boundaries
    days_to_sat = 5 - now_dt.weekday()
    if days_to_sat < 0: days_to_sat += 7
    this_saturday = now_dt + datetime.timedelta(days=days_to_sat)
    sat_str = this_saturday.strftime('%Y-%m-%d')

    # 14-day calendar reference
    calendar_ref = [ (now_dt + datetime.timedelta(days=i)).strftime('%A (%Y-%m-%d)') for i in range(15) ]
    return now_dt, today_str, sat_str, "\n".join(calendar_ref)

def _build_prompt(text, context, today_str, now_dt, sat_str, calendar_str):
    """Constructs the system and user messages for Ollama."""
    system_prompt = f"""You are LazyScheduler, a precision calendar assistant.
Today is {today_str}. Local Time: {now_dt.strftime('%H:%M')}.

Upcoming Days Reference:
{calendar_str}

ACTION RULES:
- "list": Use for showing schedule. If "this week" mentioned, end is Saturday {sat_str}.
- "find_slot": Use for gap searches. Starts at CURRENT time.
- "create/update/delete": Standard event management.

RELATIVE BORDERS:
- "This [Day]" = Nearest occurrence after today.
- "Next [Day]" = 7 days after "This [Day]".
- "This Week" ends Saturday {sat_str} at 23:59:59.

JSON FORMAT:
{{
  "action": "create" | "list" | "delete" | "update" | "find_slot",
  "title": "string",
  "start": "ISO8601",
  "end": "ISO8601",
  "description": "string",
  "location": "string",
  "attendees": ["email"],
  "add_meeting": bool,
  "search_query": "string",
  "duration_mins": int
}}
- Only set "duration_mins" if the user specifies a length (e.g. "30m gap"). Otherwise keep it 0.
Return ONLY JSON. """

    
    messages = [{"role": "system", "content": system_prompt}]
    if context:
        messages.append({"role": "assistant", "content": f"Context: {context.json()}"})
        messages.append({"role": "user", "content": f"Correction: {text}"})
    else:
        messages.append({"role": "user", "content": text})
    return messages

def _execute_llm_call(messages):
    """Abstraction for the core LLM execution."""
    try:
        response = ollama.chat(model=CONFIG.model, messages=messages, format='json')
        return response['message']['content']
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        return None

def _validate_and_transform_response(raw_json, now_dt):
    """Parses, validates, and normalizes the AI response into EventDetails."""
    try:
        data = json.loads(raw_json)
    except Exception as e:
        logger.error(f"Failed to parse LLM JSON: {e}")
        return None

    local_tz = tz.gettz(CONFIG.timezone)
    def finalize_dt(dt_str):
        if not dt_str: return None
        try:
            parsed = parser.parse(dt_str)
            if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=local_tz)
            return parsed
        except: return None

    start_dt = finalize_dt(data.get('start'))
    if not start_dt:
        start_dt = now_dt.replace(hour=0, minute=0, second=0) if data.get('action') == 'list' else now_dt
        start_dt = start_dt.replace(tzinfo=local_tz)
    
    end_dt = finalize_dt(data.get('end')) or (start_dt + datetime.timedelta(minutes=CONFIG.default_duration))
    if end_dt <= start_dt:
        end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)

    # Email extraction logic
    valid_attendees, invalid_notes = [], []
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    for entry in data.get('attendees', []):
        entry = entry.strip()
        if re.match(email_regex, entry): valid_attendees.append(entry)
        else: invalid_notes.append(entry)

    desc = data.get('description', '')
    if invalid_notes:
        desc = (desc + f"\n\nAttendees: {', '.join(invalid_notes)}").strip()

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
        reminders_minutes=data.get('reminders_minutes', [15]),
        duration_mins=int(data.get('duration_mins', 0))
    )


def parse_natural_language(text: str, context: EventDetails = None) -> EventDetails:
    """Orchestrates the decomposition of parsing natural language."""
    text = Sanitizer.sanitize_input(text)
    now_dt, today_str, sat_str, calendar_str = _get_prompt_metadata()
    messages = _build_prompt(text, context, today_str, now_dt, sat_str, calendar_str)
    
    with _console.status(f"[bold yellow]Analyzing intent with {CONFIG.model}...", spinner="dots"):
        raw_response = _execute_llm_call(messages)
    
    if not raw_response:
        raise ValueError("AI Service Unavailable")
        
    event = _validate_and_transform_response(raw_response, now_dt)
    if not event:
        raise ValueError("AI Response Validation Failed")
        
    logger.info(f"Successfully parsed intent: {event.action} -> {event.title}")
    return event

# ====================== CALENDAR OPERATIONS ======================

def list_upcoming_events(service, start_time: str, end_time: str):
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=start_time, timeMax=end_time,
            singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        logger.error(f"Calendar List Error: {e}")
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
        logger.error(f"Event Search Error: {e}")
        return []

def delete_event(service, event_id: str):
    logger.info(f"Deleting event: {event_id}")
    return service.events().delete(calendarId='primary', eventId=event_id).execute()

def update_event(service, event_id: str, new_data: EventDetails):
    logger.info(f"Updating event: {event_id}")
    event = service.events().get(calendarId='primary', eventId=event_id).execute()
    if new_data.title: event['summary'] = new_data.title
    event['start'] = {'dateTime': new_data.start, 'timeZone': CONFIG.timezone}
    event['end'] = {'dateTime': new_data.end, 'timeZone': CONFIG.timezone}
    if new_data.description: event['description'] = new_data.description
    if new_data.location: event['location'] = new_data.location
    return service.events().update(calendarId='primary', eventId=event_id, body=event).execute()

def create_event(service, event: EventDetails):
    logger.info(f"Creating event: {event.title}")
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
    if event.recurrence: event_body['recurrence'] = event.recurrence

    for attempt in range(3):
        try:
            return service.events().insert(
                calendarId='primary', 
                body=event_body, 
                conferenceDataVersion=1 if event.add_meeting else 0
            ).execute()
        except Exception as e:
            if attempt < 2 and any(k in str(e).lower() for k in ["eof", "protocol", "timeout"]):
                time.sleep(1.5); continue
            logger.error(f"Create Event Failed: {e}")
            raise e

def check_conflicts(service, start_time: str, end_time: str):
    try:
        body = {"timeMin": start_time, "timeMax": end_time, "items": [{"id": "primary"}]}
        result = service.freebusy().query(body=body).execute()
        return result['calendars']['primary']['busy']
    except Exception as e:
        logger.error(f"Conflict Check Error: {e}")
        return []

def find_free_slots(service, start_search: str, min_duration_mins=None):
    """Identifies all contiguous blocks of free time within working hours."""
    try:
        search_dt = parser.parse(start_search)
        now = datetime.datetime.now(search_dt.tzinfo)
        if search_dt < now: search_dt = now
            
        # Search for the next 7 days
        max_search = search_dt + datetime.timedelta(days=7)
        busy_data = service.freebusy().query(body={
            "timeMin": search_dt.isoformat(), "timeMax": max_search.isoformat(), "items": [{"id": "primary"}]
        }).execute()
        
        busy = sorted([ (parser.parse(b['start']), parser.parse(b['end'])) for b in busy_data['calendars']['primary']['busy'] ])
        free_blocks = []
        
        # Iterate day by day
        for i in range(7):
            day_target = (search_dt + datetime.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            work_start = day_target.replace(hour=CONFIG.working_start)
            work_end = day_target.replace(hour=CONFIG.working_end)
            
            # Don't search in the past
            if work_end < now: continue
            if work_start < now: work_start = now
            
            # Find gaps in this day
            current_ptr = work_start
            for b_s, b_e in busy:
                if b_e <= work_start: continue
                if b_s >= work_end: break
                
                # If there's a gap before this busy block
                if b_s > current_ptr:
                    gap_dur = (b_s - current_ptr).total_seconds() / 60
                    if not min_duration_mins or gap_dur >= min_duration_mins:
                        free_blocks.append((current_ptr, b_s))
                
                current_ptr = max(current_ptr, b_e)
            
            # Check for gap after last busy block until end of workday
            if current_ptr < work_end:
                gap_dur = (work_end - current_ptr).total_seconds() / 60
                if not min_duration_mins or gap_dur >= min_duration_mins:
                    free_blocks.append((current_ptr, work_end))

        return free_blocks[:10] # Return top 10 blocks
    except Exception as e:
        logger.error(f"Free Block Detection Error: {e}")
        return []

