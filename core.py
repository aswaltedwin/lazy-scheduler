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
    priority: int = Field(2, description="Priority: 1=Low, 2=Normal, 3=High")


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
        
        injection_patterns = [r"ignore previous instructions", r"system prompt", r"override", r"you are now"]
        for pattern in injection_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                logger.warning(f"Potential prompt injection detected: '{pattern}'")
        
        return text.strip()

# ====================== FALLBACK PARSER (Non-AI) ======================

class RuleBasedParser:
    """Handles high-confidence commands without AI dependency."""
    
    @staticmethod
    def parse(text: str) -> EventDetails:
        text = text.lower().strip()
        local_tz = tz.gettz(CONFIG.timezone)
        now = datetime.datetime.now(local_tz)

        # Regex for "list" commands
        list_patterns = [r"show (my )?schedule", r"list (all )?events", r"what (do|is) i have"]
        if any(re.search(p, text) for p in list_patterns):
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + datetime.timedelta(days=1)
            
            if "tomorrow" in text:
                start += datetime.timedelta(days=1)
                end += datetime.timedelta(days=1)
            elif "week" in text:
                days_to_sat = 5 - now.weekday()
                if days_to_sat < 0: days_to_sat += 7
                end = (now + datetime.timedelta(days=days_to_sat)).replace(hour=23, minute=59, second=59)
                
            return EventDetails(action="list", start=start.isoformat(), end=end.isoformat())

        # Regex for "delete" commands
        delete_match = re.search(r"(?:delete|remove|cancel) (.+)", text)
        if delete_match:
            query = delete_match.group(1).strip()
            return EventDetails(action="delete", search_query=query)

        # Regex for "free slots"
        free_patterns = [r"when am i free", r"any free (slots|time)", r"find free time"]
        if any(re.search(p, text) for p in free_patterns):
            start = now.replace(hour=CONFIG.working_start, minute=0, second=0)
            if "tomorrow" in text: start += datetime.timedelta(days=1)
            # Default to 30m if not specified
            dur = 0
            dur_match = re.search(r"(\d+)\s*(?:min|minute)", text)
            if dur_match: dur = int(dur_match.group(1))
            
            return EventDetails(action="find_slot", start=start.isoformat(), duration_mins=dur)

        return None

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

# ====================== SCHEDULING & PARSING LOGIC ======================

def _get_prompt_metadata():
    now_dt = datetime.datetime.now()
    today_str = now_dt.strftime('%A, %B %d, %Y')
    days_to_sat = 5 - now_dt.weekday()
    if days_to_sat < 0: days_to_sat += 7
    this_saturday = now_dt + datetime.timedelta(days=days_to_sat)
    sat_str = this_saturday.strftime('%Y-%m-%d')
    calendar_ref = [ (now_dt + datetime.timedelta(days=i)).strftime('%A (%Y-%m-%d)') for i in range(15) ]
    return now_dt, today_str, sat_str, "\n".join(calendar_ref)

def _build_prompt(text, context, today_str, now_dt, sat_str, calendar_str):
    system_prompt = f"""You are LazyScheduler. Today is {today_str}. Local Time: {now_dt.strftime('%H:%M')}.
Upcoming Days: {calendar_str}
JSON FORMAT: {{ "action": "create"|"list"|"delete"|"update"|"find_slot", "title": "string", "start": "ISO8601", "end": "ISO8601", "description": "string", "location": "string", "attendees": ["email"], "add_meeting": bool, "search_query": "string",  "duration_mins": int,
  "priority": int
}}
- "priority": 1 (Low: Gym/Lunch/Coffee), 2 (Normal), 3 (High: Meeting/Sync/CEO/Urgent). Default: 2.
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
    try:
        response = ollama.chat(model=CONFIG.model, messages=messages, format='json')
        return response['message']['content']
    except Exception as e:
        logger.warning(f"Ollama call failed (fallback engine will be used if possible): {e}")
        return None

def _validate_and_transform_response(raw_json, now_dt):
    try:
        data = json.loads(raw_json)
    except: return None
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
    if end_dt <= start_dt: end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)
    valid_attendees = [e.strip() for e in data.get('attendees', []) if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', e.strip())]
    return EventDetails(
        action=data.get('action', 'create'),
        title=data.get('title') or "New Event",
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        description=data.get('description', ''),
        location=data.get('location', ''),
        attendees=valid_attendees,
        add_meeting=bool(data.get('add_meeting')),
        search_query=data.get('search_query', ''),
        recurrence=data.get('recurrence', []),
        reminders_minutes=data.get('reminders_minutes', [15]),
        duration_mins=int(data.get('duration_mins', 0)),
        priority=int(data.get('priority', 2))
    )


def parse_natural_language(text: str, context: EventDetails = None) -> EventDetails:
    """Orchestrates parsing using AI with RuleBased fallback."""
    text = Sanitizer.sanitize_input(text)
    
    # 1. Try AI first (if enabled and reachable)
    raw_response = None
    try:
        now_dt, today_str, sat_str, calendar_str = _get_prompt_metadata()
        messages = _build_prompt(text, context, today_str, now_dt, sat_str, calendar_str)
        with _console.status(f"[bold yellow]Analyzing with {CONFIG.model}...", spinner="dots"):
            raw_response = _execute_llm_call(messages)
            if raw_response:
                event = _validate_and_transform_response(raw_response, now_dt)
                if event: 
                    logger.info(f"AI Parsed: {event.action}")
                    return event
    except Exception as e:
        logger.debug(f"AI Path skipped: {e}")

    # 2. Fallback to RuleBasedParser
    logger.info("Using RuleBasedParser fallback.")
    event = RuleBasedParser.parse(text)
    if event: return event
    
    raise ValueError("Could not understand the command. AI was unreachable and no patterns matched.")

# ====================== CALENDAR OPERATIONS ======================

def list_upcoming_events(service, start_time: str, end_time: str):
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=start_time, timeMax=end_time,
            singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        logger.error(f"Calendar List Error: {e}"); return []

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
        logger.error(f"Event Search Error: {e}"); return []

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
        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': m} for m in event.reminders_minutes]}
    }
    if event.add_meeting:
        event_body['conferenceData'] = {'createRequest': {'requestId': str(uuid.uuid4()), 'conferenceSolutionKey': {'type': 'hangoutsMeet'}}}
    if event.recurrence: event_body['recurrence'] = event.recurrence

    for attempt in range(3):
        try:
            return service.events().insert(calendarId='primary', body=event_body, conferenceDataVersion=1 if event.add_meeting else 0).execute()
        except:
            if attempt < 2: time.sleep(1.5); continue
            raise

def check_conflicts(service, start_time: str, end_time: str):
    """Returns a list of actual event items that overlap with the given range."""
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=start_time, timeMax=end_time,
            singleEvents=True, orderBy='startTime'
        ).execute()
        return [e for e in events_result.get('items', []) if e.get('transparency') != 'transparent']
    except Exception as e:
        logger.error(f"Conflict Check Error: {e}"); return []


def _eval_priority(summary: str) -> int:
    """Heuristic to evaluate priority of an existing event summary."""
    summary = summary.lower()
    if any(k in summary for k in ["urgent", "ceo", "meeting", "sync", "call", "interview"]): return 3
    if any(k in summary for k in ["gym", "lunch", "coffee", "break", "personal", "workout"]): return 1
    return 2

def get_magic_fix_proposal(service, new_event: EventDetails, conflicts: list):
    """Checks if a 'Magic Fix' (auto-reschedule) is possible."""
    if not conflicts: return None
    
    # Check if all conflicts are lower priority
    can_bump = True
    for c in conflicts:
        p = _eval_priority(c.get('summary', ''))
        if p >= new_event.priority:
            can_bump = False; break
            
    if not can_bump: return None
    
    # Propose moving the FIRST conflict to the next gap
    target = conflicts[0]
    duration = parser.parse(target['end']['dateTime']) - parser.parse(target['start']['dateTime'])
    
    # Find next gap for the bumped event
    suggestions = find_free_slots(service, new_event.end, min_duration_mins=int(duration.total_seconds() / 60))
    if suggestions:
        s_start, s_end = suggestions[0]
        return {
            "target_id": target['id'],
            "target_summary": target['summary'],
            "new_start": s_start,
            "new_end": s_end
        }
    return None

def find_free_slots(service, start_search: str, min_duration_mins=None):

    try:
        search_dt = parser.parse(start_search)
        now = datetime.datetime.now(search_dt.tzinfo)
        if search_dt < now: search_dt = now
        max_search = search_dt + datetime.timedelta(days=7)
        busy_data = service.freebusy().query(body={"timeMin": search_dt.isoformat(), "timeMax": max_search.isoformat(), "items": [{"id": "primary"}]}).execute()
        busy = sorted([ (parser.parse(b['start']), parser.parse(b['end'])) for b in busy_data['calendars']['primary']['busy'] ])
        free_blocks = []
        for i in range(7):
            day_target = (search_dt + datetime.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            work_start = day_target.replace(hour=CONFIG.working_start)
            work_end = day_target.replace(hour=CONFIG.working_end)
            if work_end < now: continue
            if work_start < now: work_start = now
            current_ptr = work_start
            for b_s, b_e in busy:
                if b_e <= work_start: continue
                if b_s >= work_end: break
                if b_s > current_ptr:
                    gap_dur = (b_s - current_ptr).total_seconds() / 60
                    if not min_duration_mins or gap_dur >= min_duration_mins: free_blocks.append((current_ptr, b_s))
                current_ptr = max(current_ptr, b_e)
            if current_ptr < work_end:
                gap_dur = (work_end - current_ptr).total_seconds() / 60
                if not min_duration_mins or gap_dur >= min_duration_mins: free_blocks.append((current_ptr, work_end))
        return free_blocks[:10]
    except Exception as e:
        logger.error(f"Free Block Error: {e}"); return []
