import os
import json
import datetime
import re
import time
import uuid
import logging
import ollama
from ortools.sat.python import cp_model
from typing import List, Dict, Any, Optional
from models import EventDetails
from logging.handlers import RotatingFileHandler
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dateutil import parser, tz
from rich.console import Console

# Import local configuration (imports moved to line 32 to avoid duplicates)

# ====================== LOGGING SETUP ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler("scheduler.log", maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("LazyScheduler")

from config import CONFIG, STATE, save_config, PROFILE, save_profile, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_PROJECT_ID
_console = Console()
SCOPES = ['https://www.googleapis.com/auth/calendar']


# ====================== SCIENTIFIC SCORING & VALIDATION ======================

class PriorityScorer:
    """Calculates event importance using a granular scoring matrix."""
    SCORING_MATRIX = {
        # Syncs/Work (High)
        "sync": 3.5, "meeting": 3.5, "ceo": 5.0, "urgent": 4.5, "deadline": 4.0, "standup": 3.0, "call": 3.0,
        # Social/Personal (Medium/Low)
        "coffee": 1.2, "lunch": 1.5, "dinner": 1.8, "gym": 0.8, "workout": 0.8, "walk": 0.5,
        # Focus/Deep Work
        "focus": 2.5, "deep work": 3.0, "study": 2.0
    }

    @staticmethod
    def calculate_priority(title: str) -> int:
        title_lower = title.lower()
        score = None
        
        for key, val in PriorityScorer.SCORING_MATRIX.items():
            if key in title_lower:
                if score is None or val > score:
                    score = val
                # Specific high-value override
                if key == "ceo": score = 5.0; break 

        # Check persistent locked preferences from PROFILE
        if hasattr(PROFILE, "locked_titles"):
            for locked_title, count in PROFILE.locked_titles.items():
                if locked_title in title_lower and count >= 2:
                    return 3 # Force to High priority if locked frequently
        
        if score is None: score = 2.0  # Default to Normal

        # Map to 1-3 scale
        if score >= 3.5: return 3 # High
        if score <= 1.2: return 1 # Low
        return 2 # Normal

class ValidationLayer:
    """Ensures parsed events contain all mandatory fields before scheduling."""
    
    @staticmethod
    def validate(event: EventDetails) -> bool:
        if not event: return False
        
        if event.action == "create":
            # Must have a clean title and a non-now fallback start
            if not event.title or event.title == "New Event": return False
            if not event.start: return False
        
        elif event.action in ["update", "delete"]:
            if not event.search_query and not event.title: return False
            
        elif event.action == "find_slot":
            if event.duration_mins <= 0: return False
            
        return True

# ====================== SECURITY & SANITATION ======================

class ParsingError(ValueError):
    """Raised when parsing fails, carries a smart suggestion for the user."""
    def __init__(self, message, suggestion=None):
        super().__init__(message)
        self.suggestion = suggestion


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

        # Regex for "list" commands (Schedule/Status/Search)
        list_patterns = [r"show (my )?schedule", r"list (all )?events", r"what (do|is) i have", r"list (all )?the events", r"when (is|do i|i) have (.+)"]
        list_match = any(re.search(p, text) for p in list_patterns)
        if list_match:
            # Specific range logic
            if "tomorrow" in text:
                start = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                end = start + datetime.timedelta(days=1, seconds=-1)
            elif "week" in text:
                days_to_sun = (now.weekday() + 1) % 7
                start = (now - datetime.timedelta(days=days_to_sun)).replace(hour=0, minute=0, second=0, microsecond=0)
                end = (start + datetime.timedelta(days=7, seconds=-1))
            else:
                # Check for Month/Day patterns (e.g., "July 20", "20th Oct")
                month_match = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
                if not month_match:
                    month_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", text)
                
                if month_match:
                    try:
                        g1, g2 = month_match.groups()
                        if g1.isdigit(): day, mon = int(g1), g2
                        else: mon, day = g1, int(g2)
                        
                        target_dt = parser.parse(f"{mon} {day}")
                        if target_dt.replace(tzinfo=local_tz) < now.replace(hour=0, minute=0, second=0, microsecond=0):
                            target_dt = target_dt.replace(year=now.year + 1)
                        start = target_dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=local_tz)
                        end = start + datetime.timedelta(days=1, seconds=-1)
                    except Exception:
                        logger.debug("Failed to parse month/day pattern, falling back to 31-day search")
                        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                        end = start + datetime.timedelta(days=31, seconds=-1)
                else:
                    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    if "today" in text or "what do i have" in text or "what's happening" in text:
                        end = start + datetime.timedelta(days=1, seconds=-1)
                    else:
                        end = start + datetime.timedelta(days=31, seconds=-1)

            # Unified "When is [Query]" handling
            when_match = re.search(r"when (is|do i|i) have (.+)", text)
            if when_match:
                query = when_match.group(2).strip()
                query = re.sub(r"^(my|the|a|an)\s+", "", query)
                query = re.sub(r"['’]s(\s+|$)", " ", query).strip()
                return EventDetails(action="list", search_query=query, start=start.isoformat(), end=(start + datetime.timedelta(days=31)).isoformat())

            return EventDetails(action="list", start=start.isoformat(), end=end.isoformat())


        # Regex for "delete" commands (Strict: only simple phrases or 'last')
        # We skip if words like 'all', 'everything', or multiple days are mentioned
        complex_keywords = ["all", "everything", "every", "schedules", "clear"]
        if any(w in text for w in complex_keywords):
            pass # Fall through to AI
        else:
            delete_patterns = [r"delete (.+)", r"remove (.+)", r"cancel (.+)"]
            for p in delete_patterns:
                match = re.search(p, text)
                if match:
                    query = match.group(1).strip()
                    # If query is too long (> 3 words), it's likely complex NLP
                    if len(query.split()) > 3:
                        continue
                    return EventDetails(action="delete", search_query=query)


        # Regex for "optimize day"
        if re.search(r"\b(optimize|rearrange|defrag|perfect|fix)\b.*\b(day|schedule|today)\b", text):
            return EventDetails(action="optimize_day", title=text)

        # Regex for "free slots"
        free_patterns = [r"when am i free", r"any free (slots|time)", r"find free time"]
        if any(re.search(p, text) for p in free_patterns):
            start_fs = now.replace(hour=CONFIG.working_start, minute=0, second=0)
            if "tomorrow" in text: start_fs += datetime.timedelta(days=1)
            # Default to 30m if not specified
            dur = 30
            dur_match = re.search(r"(\d+)\s*(?:min|minute)", text)
            if dur_match: dur = int(dur_match.group(1))
            
            return EventDetails(action="find_slot", start=start_fs.isoformat(), duration_mins=dur)

        # Regex for "create" commands (High-confidence: "schedule [title] at [time]")
        create_match = re.search(r"(?:schedule|create|add)\s+(.+?)\s+(?:at|on)\s+(.+)", text)
        if create_match:
            title = create_match.group(1).strip()
            time_str = create_match.group(2).strip()
            try:
                # Use dateutil.parser for the time part
                parsed_dt = parser.parse(time_str, default=now)
                if parsed_dt.tzinfo is None: parsed_dt = parsed_dt.replace(tzinfo=local_tz)
                
                # If the parsed date is in the past (e.g. "8pm" when it's 9pm), move to tomorrow
                if parsed_dt < now and "today" not in text:
                     parsed_dt += datetime.timedelta(days=1)
                
                return EventDetails(
                    action="create",
                    title=title.capitalize(),
                    start=parsed_dt.isoformat(),
                    end=(parsed_dt + datetime.timedelta(minutes=CONFIG.default_duration)).isoformat(),
                    priority=PriorityScorer.calculate_priority(title)
                )
            except Exception:
                pass # Fall back to AI if date parsing fails

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
                if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
                    logger.error("Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET in environment.")
                    raise ValueError("Google API credentials not configured. Please check your .env file.")

                # Construct client configuration from environment variables
                client_config = {
                    "installed": {
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "project_id": GOOGLE_PROJECT_ID,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                        "redirect_uris": ["http://localhost"]
                    }
                }
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
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
- "create": Use for NEW events. If provided with "Context" (a proposed event), KEEP "action": "create" to refine its details unless the user explicitly wants to switch tasks.
- "update": Use ONLY to modify events ALREADY ON THE CALENDAR. Requires a "search_query" to find the target.
- "list": Use this for "Show my schedule", "What's happening", "When is my [event]", "When do I have [event]", "List events".
- "find_slot": Use this ONLY if searching for GAPS/FREE TIME (e.g. "Find a 30m gap", "When am I free"). DO NOT use for "When is my [event]".


- "priority": 1 (Low: Gym/Lunch/Coffee), 2 (Normal), 3 (High: Meeting/Sync/CEO/Urgent). Default: 2.
- Only set "duration_mins" if the user specifies a length (e.g. "30m gap"). Otherwise keep it 0.

- "HONOR NEGATIVE CONSTRAINTS": If the user says "no notes", "no description", "remove [field]", or "[field] is not needed", you MUST set that field to "" (empty string), [] (empty list), or False. NEVER hallucinate helpful notes if the user has suppressed them or hasn't provided details.

- "VIRTUAL vs PHYSICAL": ONLY set "add_meeting": true if the user explicitly mentions words like "online", "virtual", "zoom", "meet", "video", or if the context is clearly a remote sync. For social, family, or physical activities (Lunch, Gym, Dinner, with parents, etc.), set it to false and leave "location" empty if not explicitly provided. Do NOT automatically add video links to physical events.

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
        content = response['message']['content']
        # Tolerance: Extract JSON if LLM added commentary around the JSON block
        if "{" in content and "}" in content:
            start_idx = content.find("{")
            end_idx = content.rfind("}") + 1
            return content[start_idx:end_idx]
        return content
    except Exception as e:
        logger.warning(f"Ollama call failed: {e}")
        return None

def _validate_and_transform_response(raw_json, now_dt):
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse LLM JSON response: {raw_json}")
        return None
    local_tz = tz.gettz(CONFIG.timezone)
    def finalize_dt(dt_str):
        if not dt_str: return None
        try:
            parsed = parser.parse(dt_str)
            if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=local_tz)
            return parsed
        except (ValueError, TypeError, parser.ParserError):
            logger.debug(f"Could not parse dateTime string: {dt_str}")
            return None
    start_dt = finalize_dt(data.get('start'))
    if not start_dt:
        start_dt = now_dt.replace(hour=0, minute=0, second=0) if data.get('action') == 'list' else now_dt
        start_dt = start_dt.replace(tzinfo=local_tz)

    # Smart duration: Tasks/Reminders can be 0-duration. Meetings default to 45m.
    is_task = any(k in (data.get('title') or "").lower() for k in ["deadline", "reminder", "due", "task", "check", "pay", "buy", "bill"])
    
    end_dt = finalize_dt(data.get('end'))
    if not end_dt:
        if is_task:
            end_dt = start_dt
        else:
            end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)
    
    if not is_task and end_dt <= start_dt:
        end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)
    valid_attendees = [e.strip() for e in data.get('attendees', []) if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', e.strip())]
    # Apply Scientific Priority Scoring if not explicitly set by user/high-quality LLM
    final_priority = int(data.get('priority', 0))
    if final_priority == 0 or final_priority == 2: # 2 is default, let's refine it
        final_priority = PriorityScorer.calculate_priority(data.get('title') or "")

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
        priority=final_priority
    )


def parse_natural_language(text: str, context: EventDetails = None) -> EventDetails:
    """Orchestrates parsing by prioritizing Rule-Based patterns over the AI Engine."""
    text_sanitized = Sanitizer.sanitize_input(text)
    
    # 1. Rule-Based Path
    with _console.status("[bold cyan]Consulting rule matches...", spinner="simpleDots"):
        rule_event = RuleBasedParser.parse(text_sanitized)
        if rule_event:
            # Validate rule output too (safety first)
            if ValidationLayer.validate(rule_event):
                 logger.info(f"Rule-Based Match: {rule_event.action}")
                 return rule_event

    # 2. AI Path
    try:
        now_dt, today_str, sat_str, calendar_str = _get_prompt_metadata()
        messages = _build_prompt(text_sanitized, context, today_str, now_dt, sat_str, calendar_str)
        
        with _console.status(f"[bold yellow]Analyzing intent with {CONFIG.model}...", spinner="dots"):
            raw_response = _execute_llm_call(messages)
            if raw_response:
                event = _validate_and_transform_response(raw_response, now_dt)
                
                # 3. Validation Layer
                with _console.status("[bold magenta]Validating atoms...", spinner="bouncingBar"):
                    if ValidationLayer.validate(event):
                        logger.info(f"AI Parsed & Validated: {event.action}")
                        return event
                    else:
                        logger.warning("AI output failed validation layer.")
    except Exception as e:
        logger.debug(f"AI Path error: {e}")

    # 4. Fallback Path (Repair Suggestion)
    suggestion = get_smart_suggestion(text_sanitized)
    raise ParsingError("Could not understand or validate the command.", suggestion=suggestion)


def get_smart_suggestion(text: str) -> str:
    """Uses a lightweight AI call to guess what the user meant when parsing fails."""
    prompt = f"""
    The user entered an invalid command: "{text}"
    
    Strictly suggest a valid correction based on these formats:
    1. schedule [title] [time]
    2. find free slots [duration_mins]
    3. list upcoming events
    4. delete [title]
    
    If it's close to one, return JUST the corrected command text.
    If it's total nonsense, return "Show me help".
    
    Correction:
    """
    try:
        # Use a faster/smaller model or same model with short output
        response = ollama.chat(model=CONFIG.model, messages=[{"role": "user", "content": prompt}])
        suggestion = response['message']['content'].strip().strip('"').strip("'")
        return suggestion if len(suggestion) < 100 else "Show me help"
    except Exception as e:
        logger.error(f"Suggestion engine error: {e}")
        return "Show me help"


# ====================== CALENDAR OPERATIONS ======================

def list_upcoming_events(service, start_time: str, end_time: str):
    try:
        if not start_time or not end_time:
            local_tz = tz.gettz(CONFIG.timezone)
            now = datetime.datetime.now(local_tz)
            start_time = start_time or now.replace(hour=0, minute=0, second=0).isoformat()
            end_time = end_time or (parser.parse(start_time) + datetime.timedelta(days=30)).isoformat()
            
        events_result = service.events().list(
            calendarId='primary', timeMin=start_time, timeMax=end_time,
            singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])

    except Exception as e:
        logger.error(f"Calendar List Error: {e}")
        raise # Propagate error instead of silent failure

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
        raise

def delete_event(service, event_id: str):
    logger.info(f"Deleting event: {event_id}")
    return service.events().delete(calendarId='primary', eventId=event_id).execute()

def update_event(service, event_id: str, new_data: EventDetails):
    logger.info(f"Updating event: {event_id}")
    event = service.events().get(calendarId='primary', eventId=event_id).execute()
    
    if new_data.title: event['summary'] = new_data.title
    event['start'] = {'dateTime': new_data.start, 'timeZone': CONFIG.timezone}
    event['end'] = {'dateTime': new_data.end, 'timeZone': CONFIG.timezone}
    
    # Strictly honor negative constraints or updates
    event['description'] = new_data.description
    event['location'] = new_data.location
    
    if new_data.attendees:
        event['attendees'] = [{'email': e} for e in new_data.attendees]
    
    # Handle Video Link (Google Meet)
    if new_data.add_meeting and 'conferenceData' not in event:
        event['conferenceData'] = {'createRequest': {'requestId': str(uuid.uuid4()), 'conferenceSolutionKey': {'type': 'hangoutsMeet'}}}
    
    return service.events().update(calendarId='primary', eventId=event_id, body=event, conferenceDataVersion=1 if new_data.add_meeting else 0).execute()

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
        except Exception:
            logger.warning(f"Create event attempt {attempt+1} failed", exc_info=True)
            if attempt < 2: time.sleep(1.5); continue
            raise

def check_conflicts(service, start_time: str, end_time: str):
    """Returns a list of actual event items that overlap with the given range."""
    try:
        local_tz = tz.gettz(CONFIG.timezone)
        s_dt = parser.parse(start_time)
        if s_dt.tzinfo is None: s_dt = s_dt.replace(tzinfo=local_tz)
        e_dt = parser.parse(end_time)
        if e_dt.tzinfo is None: e_dt = e_dt.replace(tzinfo=local_tz)

        events_result = service.events().list(
            calendarId='primary', timeMin=s_dt.isoformat(), timeMax=e_dt.isoformat(),
            singleEvents=True, orderBy='startTime'
        ).execute()
        return [e for e in events_result.get('items', []) if e.get('transparency') != 'transparent']
    except Exception as e:
        logger.error(f"Conflict Check Error: {e}")
        raise



def _get_time_of_day(hour: int) -> str:
    if 5 <= hour < 12: return 'morning'
    elif 12 <= hour < 17: return 'afternoon'
    elif 17 <= hour < 21: return 'evening'
    else: return 'night'

def adjust_cost(priority: int, time_category: str, user_profile) -> float:
    """Simplified cost function: base - time_bias."""
    base = priority * 2.0
    time_bias = user_profile.time_preferences.get(time_category, 0)
    return float(base - time_bias)

def _calculate_move_cost(event, old_start, new_start):
    """Computes a pain score for moving an event, considering priority, distance, and time preference."""
    summary = ""
    if hasattr(event, 'title'): summary = event.title
    elif hasattr(event, 'summary'): summary = event.summary
    elif isinstance(event, dict): summary = event.get('summary', '')
    
    priority = PriorityScorer.calculate_priority(summary)
    
    # Handle both dict-like and object-like event targets
    if hasattr(event, 'start'):
        # It's an EventDetails or similar object
        e_s = parser.parse(event.start)
        e_e = parser.parse(event.end)
    elif 'start' in event and isinstance(event['start'], dict):
        # It's a Google API dict
        e_s = parser.parse(event['start'].get('dateTime', event['start'].get('date')))
        e_e = parser.parse(event['end'].get('dateTime', event['end'].get('date')))
    elif 'old_start' in event or 'new_start' in event:
        # It's an internal proposal target dict
        e_s = event.get('new_start') or event.get('old_start')
        e_e = event.get('new_end') or e_s
    else:
        # Fallback if no timing info found
        e_s = old_start
        e_e = old_start

    duration_mins = (e_e - e_s).total_seconds() / 60
    shift_hours = abs((new_start - old_start).total_seconds()) / 3600
    w = CONFIG.cost_weights
    
    # Base Cost Components (Incorporate Profile Bias)
    p_cost = (priority * PROFILE.priority_bias * w.priority)
    d_cost = (shift_hours * w.distance)
    dur_cost = (duration_mins * w.duration)
    
    # Apply User-requested Persistent Learning logic
    tod = _get_time_of_day(new_start.hour)
    bias_cost = adjust_cost(priority, tod, PROFILE)
    
    # 3. Legacy / Supplementary distance & duration costs
    d_cost = (shift_hours * w.distance)
    dur_cost = (duration_mins * w.duration)
    
    total = bias_cost + d_cost + dur_cost
    
    breakdown = {
        "priority": p_cost,
        "distance": d_cost,
        "duration": dur_cost,
        "bias": bias_cost,
        "priority_val": priority,
        "distance_val": shift_hours,
        "duration_val": duration_mins
    }
    return total, breakdown


class StrategicPartner:
    """The tactical brain behind negotiation and schedule health."""
    
    @staticmethod
    def extract_intent(user_input: str, busy_events: list) -> Dict[str, Any]:
        """Analyzes user resistance to find specific events or constraints to lock."""
        text = user_input.lower().strip()
        words = set(text.split())
        
        # 1. Look for specific event titles in the conflict list
        for event in busy_events:
            summary = event.get('summary', '').lower()
            summary_words = set(summary.split())
            
            # Exact or Substring match
            if summary in text or text in summary:
                return {"type": "lock_event", "id": event['id'], "summary": event['summary']}
            
            # Significant word overlap (ignoring very common fillers)
            meaningful_words = summary_words - {"the", "a", "an", "at", "to", "sync", "meeting"}
            if words.intersection(meaningful_words):
                return {"type": "lock_event", "id": event['id'], "summary": event['summary']}
        
        # 2. Look for temporal constraints
        if "morning" in text: return {"type": "constrain_time", "constraint": "morning"}
        if "afternoon" in text: return {"type": "constrain_time", "constraint": "afternoon"}
        
        return {"type": "general_refusal"}

    @staticmethod
    def calculate_day_health(events: list) -> Dict[str, Any]:
        """Calculates a health status for the day."""
        count = len(events)
        anchors = len([e for e in events if PriorityScorer.calculate_priority(e.get('summary','')) >= 3])
        
        score = max(0, 100 - (count * 5) - (anchors * 10))
        status = "Stable"
        if score < 40: status = "Gridlocked"
        elif score < 70: status = "Fragile"
        
        return {"score": score, "status": status, "anchors": anchors}

    @staticmethod
    def generate_advice(proposal: dict, fixed_ids: list, conflicts: list) -> str:
        """Generates a human-like tactical summary for a proposal, including behavioral insights."""
        strategy = proposal.get('strategy', 'balanced')
        
        # Dominance Detection
        forbidden = []
        for tod, misses in PROFILE.time_misses.items():
            if (misses - PROFILE.time_hits.get(tod, 0)) >= getattr(PROFILE, "dominance_threshold", 5):
                forbidden.append(tod)
        
        intelligence_brief = ""
        if forbidden:
            intelligence_brief = f" [bold cyan](Deep Adaptation: Avoiding {', '.join(forbidden)} based on your rejection history)[/bold cyan]"
        
        if strategy == "guardian":
            return "I am protecting your existing anchors while finding a surgical gap for this request." + intelligence_brief
        if strategy == "swapper":
            return "Since your day is dense, I've swapped a lower priority task to make space." + intelligence_brief
        if strategy == "evictor" and fixed_ids:
            return "Because you locked specific items, I had to displace a medium-priority task to ensure feasibility." + intelligence_brief
        
        return "I've optimized this path to minimize shift costs while respecting your constraints." + intelligence_brief

class OptimizationEngine:
    """Formal constraint optimization engine powered by Google OR-Tools."""
    
    @staticmethod
    def solve_scheduling_problem(service, new_event: Optional[EventDetails], conflicts: list, all_day_events: list, strategy: str = "optimal", target_date: Optional[str] = None, fixed_ids: Optional[List[str]] = None):
        """Finds a globally optimal arrangement using CP-SAT solver with specific strategies."""
        model = cp_model.CpModel()
        local_tz = tz.gettz(CONFIG.timezone)
        
        # 1. Horizon & Granularity
        # Determine the reference date (Today and 00:00)
        if new_event:
            ref_date = parser.parse(new_event.start).replace(hour=0, minute=0, second=0, microsecond=0)
        elif target_date:
            ref_date = parser.parse(target_date).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            ref_date = datetime.datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        
        if ref_date.tzinfo is None: ref_date = ref_date.replace(tzinfo=local_tz)

        def to_min(dt_str):
            dt = parser.parse(dt_str)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=local_tz)
            # Normalize to the same timezone as ref_date for subtraction
            return int((dt.astimezone(ref_date.tzinfo) - ref_date).total_seconds() / 60)

        work_start_min = CONFIG.working_start * 60
        work_end_min = CONFIG.working_end * 60
        max_shift_min = int(CONFIG.max_shift_hours * 60)

        # 2. Collect all candidates for movement (All events in the day except urgent ones?)
        # For efficiency, we only move events that are involved in the conflict loop
        movable_events = []
        for e in all_day_events:
            p = PriorityScorer.calculate_priority(e.get('summary', ''))
            if p < 3 or e.get('id') in [c.get('id') for c in conflicts]:
                 movable_events.append(e)

        # 3. Define Variables
        vars = {}
        intervals = []
        
        # Candidate List (All events in the day + the NEW event)
        if new_event:
            new_event_start = to_min(new_event.start)
            new_event_end = to_min(new_event.end)
            new_event_dur = new_event_end - new_event_start

        # Prepare unified movable list
        movables = []
        for e in all_day_events:
            if e in movable_events: movables.append(e)
            else:
                # Fixed Anchors
                e_start = to_min(e['start'].get('dateTime', e['start'].get('date')))
                e_end = to_min(e['end'].get('dateTime', e['end'].get('date')))
                e_dur = e_end - e_start
                intervals.append(model.NewIntervalVar(e_start, e_dur, e_end, f"fixed_{e.get('id')}"))

        # Add New Event to Movables if present
        if new_event:
            movables.append({
                "id": "new_event",
                "summary": new_event.title,
                "start_min": new_event_start,
                "end_min": new_event_end,
                "duration": new_event_dur,
                "priority": new_event.priority
            })

        for m in movables:
            m_id = m.get('id')
            m_summary = m.get('summary', 'Unknown')
            # Handle both Google API dicts and our 'virtual' new_event dict
            if m_id == "new_event":
                m_orig_start = m.get('start_min')
                m_dur = m.get('duration')
                m_priority = m.get('priority')
            else:
                m_orig_start = to_min(m['start'].get('dateTime', m['start'].get('date')))
                m_dur = to_min(m['end'].get('dateTime', m['end'].get('date'))) - m_orig_start
                m_priority = PriorityScorer.calculate_priority(m_summary)

            # Constraints: stay within working hours
            lb = work_start_min
            ub = work_end_min - m_dur
            
            # Distance Constraint: Limit total shift for efficiency and reality
            # If strategy is 'resilient' or 'swapper', we remove these limits to escape gridlock or allow trades
            if strategy in ["resilient", "swapper"]:
                shift_limit = 24 * 60 
            else:
                shift_limit = max_shift_min if m_id != "new_event" else 240 # Let new event move up to 4 hours
            
            lb = max(lb, m_orig_start - shift_limit)
            ub = min(ub, m_orig_start + shift_limit)

            s_var = model.NewIntVar(lb, ub, f"start_{m_id}")
            e_var = model.NewIntVar(lb + m_dur, ub + m_dur, f"end_{m_id}")
            i_var = model.NewIntervalVar(s_var, m_dur, e_var, f"interval_{m_id}")
            
            # Distance: abs(start_var - original_start)
            diff = model.NewIntVar(-shift_limit, shift_limit, f"diff_{m_id}")
            abs_diff = model.NewIntVar(0, shift_limit, f"abs_diff_{m_id}")
            model.Add(diff == s_var - m_orig_start)
            model.AddAbsEquality(abs_diff, diff)
            
            vars[m_id] = {
                "start": s_var, "abs_diff": abs_diff, "original_start": m_orig_start,
                "priority": m_priority, "summary": m_summary, "duration": m_dur
            }
            intervals.append(i_var)

        # 4. Constraints
        model.AddNoOverlap(intervals)

        # Persona-based Strict Constraints
        if strategy == "juggernaut":
            # Force the new event to land EXACTLY where requested
            if 'new_event' in vars:
                model.Add(vars['new_event']['start'] == vars['new_event']['original_start'])
        elif strategy == "guardian":
            # Force all EXISTING events to remain stationary
            for e_id, v in vars.items():
                if e_id != "new_event":
                    model.Add(v['start'] == v['original_start'])
        elif strategy == "evictor":
            # Force the new event to land EXACTLY where requested (Juggernaut-style)
            if 'new_event' in vars:
                model.Add(vars['new_event']['start'] == vars['new_event']['original_start'])
            # AND specifically lock ALL High Priority events (P3) in place
            for e_id, v in vars.items():
                if v['priority'] >= 3 and e_id != "new_event":
                    model.Add(v['start'] == v['original_start'])
                    logger.info(f"Eviction Strategy: Locking anchor event '{v['summary']}' (P{v['priority']})")

        # Negotiation Locks: Explicitly fixed IDs from user feedback
        if fixed_ids:
            for e_id in fixed_ids:
                if e_id in vars:
                    model.Add(vars[e_id]['start'] == vars[e_id]['original_start'])

        # Adjust weights based on strategy
        w = CONFIG.cost_weights
        distance_multiplier = 10 if strategy == "minimal_disruption" else 1
        
        total_cost_vars = []
        
        # Dominant Adaptation Logic: Identify categories reaching the dominance threshold
        forbidden_categories = []
        loved_categories = []
        for tod, misses in PROFILE.time_misses.items():
            hits = PROFILE.time_hits.get(tod, 0)
            if (misses - hits) >= getattr(PROFILE, "dominance_threshold", 5):
                forbidden_categories.append(tod)
            elif (hits - misses) >= getattr(PROFILE, "dominance_threshold", 5):
                loved_categories.append(tod)

        # Precompute the hourly profile cost array (scaled by 10 for integer math)
        hour_cost_array = []
        for h in range(24):
            tod = _get_time_of_day(h)
            time_bias = PROFILE.time_preferences.get(tod, 0)
            
            # Confidence Scaling
            hits = PROFILE.time_hits.get(tod, 0)
            misses = PROFILE.time_misses.get(tod, 0)
            confidence_mult = 2 if (hits + misses) > 5 else 1
            
            # Base preference cost
            cost = -time_bias * 250 * confidence_mult
            
            # DOMINANT OVERRIDE: Extreme penalties for forbidden categories (P2/P3)
            if tod in forbidden_categories:
                cost += 50000 # Massive penalty
            elif tod in loved_categories:
                cost -= 50000 # Massive reward
                
            hour_cost_array.append(cost) 

        for e_id, v in vars.items():
            # 3. Profile / Bias Cost
            hour_var = model.NewIntVar(0, 23, f"hour_{e_id}")
            model.AddDivisionEquality(hour_var, v['start'], 60)
            
            # DOMINANT OVERRIDE: Hard constraints for Low Priority (P1) events
            if v['priority'] <= 1:
                for h in range(24):
                    if _get_time_of_day(h) in forbidden_categories:
                        model.Add(hour_var != h)
                        logger.info(f"Dominant Adaptation: Hard blocking hour {h} for P1 event '{v['summary']}'")

            bias_cost_var = model.NewIntVar(-100000, 100000, f"bias_cost_{e_id}")
            model.AddElement(hour_var, hour_cost_array, bias_cost_var)
            
            # Boolean variable: is_moved (True if abs_diff > 0)
            is_moved = model.NewBoolVar(f"is_moved_{e_id}")
            model.Add(v['abs_diff'] > 0).OnlyEnforceIf(is_moved)
            model.Add(v['abs_diff'] == 0).OnlyEnforceIf(is_moved.Not())
            
            # 1. Priority Cost: Only pay this static pain IF the event is moved
            p_weight = int(v['priority'] * PROFILE.priority_bias * w.priority * 10)
            p_cost_var = model.NewIntVar(0, max(1, p_weight), f"p_cost_{e_id}")
            model.Add(p_cost_var == p_weight).OnlyEnforceIf(is_moved)
            model.Add(p_cost_var == 0).OnlyEnforceIf(is_moved.Not())
            
            # 2. Distance Cost: (shift_hours * w.distance) -> scaled by 10
            d_weight_per_min = max(1, int((w.distance * 10 * distance_multiplier) / 60))
            d_cost_var = model.NewIntVar(0, 1000000, f"d_cost_{e_id}")
            model.Add(d_cost_var == v['abs_diff'] * d_weight_per_min)
            
            # 3. Profile / Bias Cost: Pay this penalty/reward based on the newly chosen hour
            hour_var = model.NewIntVar(0, 23, f"hour_{e_id}")
            model.AddDivisionEquality(hour_var, v['start'], 60)
            
            bias_cost_var = model.NewIntVar(-10000, 10000, f"bias_cost_{e_id}")
            model.AddElement(hour_var, hour_cost_array, bias_cost_var)
            
            v['segments'] = {
                "p_var": p_cost_var,
                "d_var": d_cost_var,
                "b_var": bias_cost_var
            }
            
            item_cost = model.NewIntVar(-100000, 1000000, f"item_cost_{e_id}")
            
            # Penalty Multiplier for shifting the NEW event (to discourage slight drifts unless necessary)
            m_mult = 1.5 if e_id == "new_event" else 1.0
            
            # Global Optimization Tuning: If no new event, we are 'defragging'. 
            # We reduce distance penalty to allow the schedule to actually move.
            # Scaling down distance cost by factor of 5 (using integer math)
            if not new_event and strategy == "balanced":
                # Scale up bias to make distance and priority moves relatively cheaper during 'defrag'
                model.Add(item_cost == p_cost_var + d_cost_var + (bias_cost_var * 100))
            else:
                # Normal mode: bias is already boosted by 50x in the array
                model.Add(item_cost == p_cost_var + d_cost_var + bias_cost_var)
            total_cost_vars.append(item_cost)
            
        model.Minimize(sum(total_cost_vars))

        # 6. Solve
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 5.0
        status = solver.Solve(model)

        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            proposal_targets = []
            for e_id, v in vars.items():
                final_start = solver.Value(v['start'])
                if final_start != v['original_start'] or e_id == "new_event":
                    # Extract actual mathematical weights
                    seg = v.get('segments', {})
                    shift_mins = int(abs(final_start - v['original_start']))
                    breakdown = {
                        "priority_cost": solver.Value(seg['p_var']) / 10.0 if 'p_var' in seg else 0,
                        "distance_cost": solver.Value(seg['d_var']) / 10.0 if 'd_var' in seg else 0,
                        "bias_points": solver.Value(seg['b_var']) / 1.0 if 'b_var' in seg else 0,
                        "raw_p_rank": v['priority'],
                        "shift_mins": shift_mins
                    }
                    
                    proposal_targets.append({
                        "id": e_id,
                        "summary": v['summary'],
                        "new_start": ref_date + datetime.timedelta(minutes=final_start),
                        "new_end": ref_date + datetime.timedelta(minutes=final_start + v['duration']),
                        "old_start": ref_date + datetime.timedelta(minutes=v['original_start']),
                        "breakdown": breakdown
                    })
            
            if not proposal_targets: return None
            
            return {
                "targets": proposal_targets,
                "cost": solver.ObjectiveValue() / 10.0,
                "reason": f"Globally Optimized ({strategy})",
                "status": "success"
            }
        else:
            # Failure Case: Capture Diagnostics
            diag = {
                "status": "failure",
                "solver_status": status,
                "strategy": strategy,
                "event_count": len(vars),
                "total_duration": sum([v['duration'] for v in vars.values()]),
                "fixed_count": len(fixed_ids) if fixed_ids else 0
            }
            logger.warning(f"Optimization Failure [{strategy}]: {diag}")
            
            # Persistent Failure Logging
            try:
                with open("scheduler_failures.log", "a") as f:
                    timestamp = datetime.datetime.now().isoformat()
                    f.write(f"[{timestamp}] FAIL | {strategy} | Events: {diag['event_count']} | Fix: {diag['fixed_count']}\n")
            except: pass
            
            return diag


    @staticmethod
    def optimize_day_transaction(service, date_str: str):
        """Killer Feature: Rearranges the entire day for peak productivity alignment."""
        local_tz = tz.gettz(CONFIG.timezone)
        try:
            # Use provided date or today
            target_dt = parser.parse(date_str) if date_str else datetime.datetime.now(local_tz)
            today = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            if today.tzinfo is None: today = today.replace(tzinfo=local_tz)
            tomorrow = today + datetime.timedelta(days=1)
            
            all_events = list_upcoming_events(service, today.isoformat(), tomorrow.isoformat())
            if not all_events:
                return {"error": "No events found to optimize."}

            # Run Full-Day Optimization
            proposal = OptimizationEngine.solve_scheduling_problem(
                service, 
                new_event=None, 
                conflicts=[], 
                all_day_events=all_events, 
                strategy="balanced", 
                target_date=today.isoformat()
            )
            
            if not proposal or not proposal.get('targets'):
                return {"error": "Your schedule is already perfectly optimal!"}
                
            return {
                "targets": proposal['targets'],
                "cost": proposal['cost'],
                "original_events": all_events
            }
        except Exception as e:
            logger.error(f"Day Optimization failed: {e}")
            return {"error": str(e)}

    @staticmethod
    def solve_split_problem(service, new_event: EventDetails, all_day_events: list):
        """Finds two separate blocks that satisfy the total duration."""
        local_tz = tz.gettz(CONFIG.timezone)
        try:
            # Simple splitter: 50/50 split
            total_dur = (parser.parse(new_event.end) - parser.parse(new_event.start)).total_seconds() / 60
            part1_dur = int(total_dur // 2)
            part2_dur = int(total_dur - part1_dur)
            
            # Find free slots for Part 1
            slots1 = find_free_slots(service, new_event.start, min_duration_mins=part1_dur)
            if not slots1: return None
            
            # Find free slots for Part 2 (starting after Part 1 slot)
            slots2 = find_free_slots(service, slots1[0][1].isoformat(), min_duration_mins=part2_dur)
            if not slots2: return None
            
            s1_start, s1_end = slots1[0]
            s2_start, s2_end = slots2[0]
            
            return {
                "targets": [
                    {"id": "new_event_part1", "summary": f"{new_event.title} (Part 1)", "new_start": s1_start, "new_end": s1_end, "old_start": parser.parse(new_event.start).replace(tzinfo=local_tz), "breakdown": {"bias": -20}},
                    {"id": "new_event_part2", "summary": f"{new_event.title} (Part 2)", "new_start": s2_start, "new_end": s2_end, "old_start": parser.parse(new_event.start).replace(tzinfo=local_tz), "breakdown": {"bias": -20}}
                ],
                "cost": 50, # Penalty for splitting
                "reason": "Split Strategy (Fragmented slots)"
            }
        except Exception as e:
            logger.error(f"Split problem logic failed: {e}")
            return None

class DecisionEngine:
    """Multi-solution ranking engine that generates and compares scheduling options."""

    @staticmethod
    def solve_reverse_split_problem(service, new_event: EventDetails, conflicts: list):
        """Creative Strategy: The Fragmentor. Instead of splitting the new event, we split a long existing blocker."""
        if not conflicts: return None
        local_tz = tz.gettz(CONFIG.timezone)
        try:
            # Helper to parse Google API time dicts
            def parse_raw(t_dict):
                raw = t_dict.get('dateTime', t_dict.get('date'))
                dt = parser.parse(raw)
                return dt.replace(tzinfo=local_tz) if dt.tzinfo is None else dt

            # Find the longest movable blocker
            blocker = sorted(conflicts, key=lambda x: (parse_raw(x['end']) - parse_raw(x['start'])).total_seconds(), reverse=True)[0]
            b_start = parse_raw(blocker['start'])
            b_end = parse_raw(blocker['end'])
            b_dur = (b_end - b_start).total_seconds() / 60
            
            if b_dur < 120: return None # Only split substantial blocks
            
            req_start = parser.parse(new_event.start)
            req_end = parser.parse(new_event.end)
            if req_start.tzinfo is None: req_start = req_start.replace(tzinfo=local_tz)
            if req_end.tzinfo is None: req_end = req_end.replace(tzinfo=local_tz)
            
            # Fragment the blocker around the new event
            p1_start, p1_end = b_start, req_start
            p2_start, p2_end = req_end, b_end
            
            if (p1_end - p1_start).total_seconds() < 900 or (p2_end - p2_start).total_seconds() < 900:
                return None

            return {
                "targets": [
                    {"id": "new_event", "summary": new_event.title, "new_start": req_start, "new_end": req_end, "old_start": req_start, "breakdown": {}},
                    {"id": f"split_{blocker['id']}_pt1", "summary": f"{blocker['summary']} (Part 1)", "new_start": p1_start, "new_end": p1_end, "old_start": b_start, "breakdown": {"priority": 10}},
                    {"id": f"split_{blocker['id']}_pt2", "summary": f"{blocker['summary']} (Part 2)", "new_start": p2_start, "new_end": p2_end, "old_start": b_start, "breakdown": {"priority": 10}}
                ],
                "cost": 60, # High complexity cost
                "reason": "The Fragmentor (Reverse Splitting)"
            }
        except Exception as e:
            logger.error(f"Reverse Split failed: {e}")
            return None
    
    @staticmethod
    def build_explanation(proposal, new_event: EventDetails, conflicts: list):
        """Generates structured, data-driven reasoning for a scheduling proposal."""
        reasons = []
        targets = proposal.get('targets', [])
        
        # 1. Persona Profile
        strategy = proposal.get('reason', 'Optimal')
        reasons.append(f"Tactical Profile: [bold]{strategy}[/bold]")

        # 2. Mathematical Root Reasoning
        total_p_pain = sum([t.get('breakdown', {}).get('priority_cost', 0) for t in targets])
        total_d_pain = sum([t.get('breakdown', {}).get('distance_cost', 0) for t in targets])
        total_gain = -sum([t.get('breakdown', {}).get('bias_points', 0) for t in targets])
        
        reasons.append(f"Root Rational: Optimized for global efficiency (Total Cost: {proposal.get('cost', 0):.1f}).")
        
        # 3. Scientific Priority Evidence
        req_p = new_event.priority
        for t in targets:
            if t['id'] == "new_event": continue
            
            b = t.get('breakdown', {})
            p_val = b.get('raw_p_rank', 2)
            shift_mins = b.get('shift_mins', 0)
            
            if req_p > p_val:
                reasons.append(f"• [bold]Priority Trade-off[/bold]: Protected '{new_event.title}' (P{req_p}) vs '{t['summary']}' (P{p_val}).")
            elif req_p == p_val:
                reasons.append(f"• [bold]Fairness Resolve[/bold]: Balanced sacrifice for equal-priority tasks (P{req_p}).")
            
            if shift_mins > 0:
                reasons.append(f"• [bold]Surgical Shift[/bold]: '{t['summary']}' displaced by exactly {shift_mins}m to clear slot.")

        # 4. Behavioral Evidence
        avg_bias = sum([t.get('breakdown', {}).get('bias_points', 0) for t in targets]) / len(targets) if targets else 0
        ref_dt = targets[0]['new_start'] if targets else None
        
        if ref_dt:
            tod = _get_time_of_day(ref_dt.hour)
            hits = PROFILE.time_hits.get(tod, 0)
            total = hits + PROFILE.time_misses.get(tod, 0)
            conf = int((hits / total) * 100) if total > 0 else 0
            
            if avg_bias < -100: # Strong preference
                reasons.append(f"• [bold]Pattern Match[/bold]: Choice anchored in your {conf}% preference for '{tod}' blocks.")
            elif avg_bias > 100:
                reasons.append(f"• [bold]Constraint Avoidance[/bold]: Navigated away from '{tod}' due to high historical rejection (Bias penalty applied).")

        return reasons

    @staticmethod
    def generate_options(service, new_event: EventDetails, conflicts: list, fixed_ids: Optional[List[str]] = None):
        options = []
        local_tz = tz.gettz(CONFIG.timezone)
        
        try:
            start_dt = parser.parse(new_event.start)
            if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=local_tz)
            start_dt = start_dt.replace(hour=0, minute=0, second=0)
            end_dt = start_dt + datetime.timedelta(days=1)
            all_day_events = list_upcoming_events(service, start_dt.isoformat(), end_dt.isoformat())
            all_day_events = [e for e in all_day_events if e.get('summary') != new_event.title]
            
            # --- PASS 1: The Diplomat (Balanced) ---
            p1 = OptimizationEngine.solve_scheduling_problem(service, new_event, conflicts, all_day_events, strategy="balanced", fixed_ids=fixed_ids)
            if p1 and p1.get('status') == "success": 
                p1['reason'] = "The Diplomat (Balanced)"
                options.append(p1)
            
            # --- PASS 2: The Juggernaut (Pin Request) ---
            p2 = OptimizationEngine.solve_scheduling_problem(service, new_event, conflicts, all_day_events, strategy="juggernaut", fixed_ids=fixed_ids)
            if p2 and p2.get('status') == "success": 
                p2['reason'] = "The Juggernaut (Pin Your Request)"
                options.append(p2)

            # --- PASS 3: The Guardian (Protect Calendar) ---
            p3 = OptimizationEngine.solve_scheduling_problem(service, new_event, conflicts, all_day_events, strategy="guardian", fixed_ids=fixed_ids)
            if p3 and p3.get('status') == "success": 
                p3['reason'] = "The Guardian (Protect Calendar)"
                options.append(p3)

            # --- PASS 4: The Swapper (Role Reversal) ---
            p4 = OptimizationEngine.solve_scheduling_problem(service, new_event, conflicts, all_day_events, strategy="swapper", fixed_ids=fixed_ids)
            if p4 and p4.get('status') == "success":
                p4['reason'] = "The Swapper (Dynamic Exchange)"
                options.append(p4)

            # --- PASS 5: The Evictor (Strategic Displacement) ---
            p5 = OptimizationEngine.solve_scheduling_problem(service, new_event, conflicts, all_day_events, strategy="evictor", fixed_ids=fixed_ids)
            if p5 and p5.get('status') == "success":
                p5['reason'] = "The Evictor (Strategic Displacement)"
                options.append(p5)

            # --- PASS 6: The Splitter (Fragment Task) ---
            p6 = OptimizationEngine.solve_split_problem(service, new_event, all_day_events)
            if p6 and p6.get('status') == "success": options.append(p6)

            # --- PASS 7: The Fragmentor (Reverse Split) ---
            p7 = DecisionEngine.solve_reverse_split_problem(service, new_event, conflicts)
            if p7 and p7.get('status') == "success": options.append(p7)

        except Exception as e:
            logger.error(f"Optimization strategies failed: {e}")

        # Final Scoring & Ranking
        unique_options = []
        seen_fingerprints = set()
        for opt in options:
            if not opt or opt.get('status') != "success": continue
            # Sort IDs to ensure stable fingerprinting
            fingerprint = "-".join([f"{t['id']}:{t['new_start'].isoformat()}" for t in sorted(opt['targets'], key=lambda x: str(x['id']))])
            if fingerprint not in seen_fingerprints:
                unique_options.append(opt)
                seen_fingerprints.add(fingerprint)
        
        if not unique_options:
            # --- THE RESCUE PIVOT ---
            # Create a diagnostic rescue proposal
            return [{
                "status": "failure",
                "reason": "Total Schedule Gridlock",
                "suggestions": [
                    "Manual Displacement: Move your 'Deep Work' or 'Focus' blocks to clear space.",
                    "Surgical Truncation: Try shortening your request to fit the existing gaps.",
                    "The Overflow: Your day is at 100% capacity; reschedule 'Review' to tomorrow."
                ],
                "targets": []
            }]

        # Final Sort and Score Calculation for winners
        health = StrategicPartner.calculate_day_health(conflicts)
        
        unique_options.sort(key=lambda x: x['cost'])
        top_options = unique_options[:3]
        
        for opt in top_options:
            for t in opt['targets']:
                if not t.get('breakdown'):
                    o_start = t.get('old_start') or parser.parse(new_event.start).replace(tzinfo=local_tz)
                    _, breakdown = _calculate_move_cost(t, o_start, t['new_start'])
                    t['breakdown'] = breakdown
            
            opt['health'] = health
            opt['tactical_advice'] = StrategicPartner.generate_advice(opt, fixed_ids, conflicts)
            opt['reasons'] = DecisionEngine.build_explanation(opt, new_event, conflicts)
            
        return top_options


def get_magic_fix_proposals(service, new_event: EventDetails, conflicts: list, fixed_ids: Optional[List[str]] = None):
    """Upgraded to return multiple ranked proposals."""
    if not conflicts: return []
    return DecisionEngine.generate_options(service, new_event, conflicts, fixed_ids=fixed_ids)


class LearningEngine:
    """Adaptive intelligence that learns from user scheduling feedback."""
    
    @staticmethod
    def update_time_preference(time_str: str, accepted: bool):
        """Updates persistent time preferences and saves immediately."""
        local_tz = tz.gettz(CONFIG.timezone)
        try:
            dt = parser.parse(time_str)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=local_tz)
        except Exception:
            # Fallback if string is not parseable
            return

        tod = _get_time_of_day(dt.hour)
        feedback = 1.0 if accepted else -1.0
        old_score = PROFILE.time_preferences.get(tod, 0.0)
        
        # preference_score = old_score * 0.9 + new_feedback
        new_score = (old_score * 0.9) + feedback
        PROFILE.time_preferences[tod] = new_score

        if accepted:
            PROFILE.time_hits[tod] += 1
        else:
            PROFILE.time_misses[tod] += 1
            
        logger.info(f"Learning Engine: Updated {tod} | Score: {new_score:.2f} | Hits: {PROFILE.time_hits[tod]}, Misses: {PROFILE.time_misses[tod]}")
        save_profile(PROFILE)

    @staticmethod
    def record_lock(title: str):
        """Records a user locking an event during negotiation. Influences future priority."""
        title_clean = title.lower()
        if title_clean not in PROFILE.locked_titles:
            PROFILE.locked_titles[title_clean] = 0
        PROFILE.locked_titles[title_clean] += 1
        logger.info(f"Learning Engine: Recorded lock for '{title_clean}' (Total: {PROFILE.locked_titles[title_clean]})")
        save_profile(PROFILE)

    @staticmethod
    def apply_feedback(accepted: bool, proposal: dict):
        """Processes feedback for a multi-event magic fix proposal."""
        if accepted:
            CONFIG.behavior.accepted_fixes += 1
        else:
            CONFIG.behavior.rejected_fixes += 1
        CONFIG.behavior.total_moves += 1
        CONFIG.behavior.last_interaction = datetime.datetime.now().isoformat()
        
        targets = proposal.get('targets', [])
        for t in targets:
            # Update preferences for each moved event's new slot
            new_start = t.get('new_start')
            if new_start:
                LearningEngine.update_time_preference(new_start.isoformat(), accepted)
        
        save_config(CONFIG)



def find_free_slots(service, start_search: str, min_duration_mins=None):
    try:
        local_tz = tz.gettz(CONFIG.timezone)
        search_dt = parser.parse(start_search)
        if search_dt.tzinfo is None: search_dt = search_dt.replace(tzinfo=local_tz)
        
        now = datetime.datetime.now(local_tz)
        if search_dt < now: search_dt = now
        
        # Query 2 weeks of busy data since the loop below checks up to 14 days
        max_search = (search_dt + datetime.timedelta(days=14)).replace(hour=23, minute=59)
        busy_data = service.freebusy().query(body={"timeMin": search_dt.isoformat(), "timeMax": max_search.isoformat(), "items": [{"id": "primary"}]}).execute()
        
        # Ensure all busy intervals are in local timezone
        busy = []
        for b in busy_data['calendars']['primary']['busy']:
            s_ext = parser.parse(b['start']).astimezone(local_tz)
            e_ext = parser.parse(b['end']).astimezone(local_tz)
            busy.append((s_ext, e_ext))
        busy.sort()
        
        free_blocks = []
        for i in range(14): # Search up to 2 weeks
            day_target = (search_dt + datetime.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            work_start = day_target.replace(hour=CONFIG.working_start).replace(tzinfo=local_tz)
            work_end = day_target.replace(hour=CONFIG.working_end).replace(tzinfo=local_tz)
            
            if work_end < now: continue
            
            # For the first day of search, we must start from search_dt if it's later than work_start
            effective_start = work_start
            if i == 0 and search_dt > work_start:
                effective_start = search_dt
            
            if effective_start < now: effective_start = now
            
            # Check if there are ANY events for this specific day
            day_busy = [b for b in busy if b[0].date() == day_target.date()]
            
            if not day_busy:
                # No events at all? Show whole working day remaining
                if work_end > effective_start:
                    free_blocks.append((effective_start, work_end))
                continue

            current_ptr = effective_start
            for b_s, b_e in day_busy:
                if b_e <= work_start: continue
                if b_s >= work_end: continue
                
                if b_s > current_ptr:
                    gap_dur = (b_s - current_ptr).total_seconds() / 60
                    if not min_duration_mins or gap_dur >= min_duration_mins:
                        free_blocks.append((current_ptr, b_s))
                current_ptr = max(current_ptr, b_e)

            if current_ptr < work_end:
                gap_dur = (work_end - current_ptr).total_seconds() / 60
                if not min_duration_mins or gap_dur >= min_duration_mins:
                    free_blocks.append((current_ptr, work_end))

                    
        return free_blocks[:10]
    except Exception:
        logger.exception("Unexpected error in find_free_slots")
        return []

