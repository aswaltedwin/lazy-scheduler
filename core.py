import os
import json
import datetime
import re
import time
import uuid
import logging
import ollama
from ortools.sat.python import cp_model
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

from config import CONFIG, STATE, save_config
_console = Console()
SCOPES = ['https://www.googleapis.com/auth/calendar']

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


        # Regex for "free slots"
        free_patterns = [r"when am i free", r"any free (slots|time)", r"find free time"]
        if any(re.search(p, text) for p in free_patterns):
            start_fs = now.replace(hour=CONFIG.working_start, minute=0, second=0)
            if "tomorrow" in text: start_fs += datetime.timedelta(days=1)
            # Default to 30m if not specified
            dur = 0
            dur_match = re.search(r"(\d+)\s*(?:min|minute)", text)
            if dur_match: dur = int(dur_match.group(1))
            
            return EventDetails(action="find_slot", start=start_fs.isoformat(), duration_mins=dur)

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
    """Orchestrates parsing by prioritizing Rule-Based patterns over the AI Engine."""
    text_sanitized = Sanitizer.sanitize_input(text)
    
    # 1. Try Rule-Based Parser first (Catch high-confidence common commands)
    rule_event = RuleBasedParser.parse(text_sanitized)
    if rule_event:
        logger.info(f"Rule-Based Match: {rule_event.action}")
        return rule_event

    # 2. Fallback to AI Engine for complex intent extraction
    try:
        now_dt, today_str, sat_str, calendar_str = _get_prompt_metadata()
        messages = _build_prompt(text_sanitized, context, today_str, now_dt, sat_str, calendar_str)
        with _console.status(f"[bold yellow]Analyzing intent...", spinner="dots"):
            raw_response = _execute_llm_call(messages)
            if raw_response:
                event = _validate_and_transform_response(raw_response, now_dt)
                if event: 
                    logger.info(f"AI Parsed: {event.action}")
                    return event
    except Exception as e:
        logger.debug(f"AI Path error: {e}")

    # If everything fails, try to generate a helpful suggestion
    suggestion = get_smart_suggestion(text_sanitized)
    raise ParsingError("Could not understand the command.", suggestion=suggestion)


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


def _eval_priority(summary: str) -> int:
    """Heuristic to evaluate priority of an existing event summary."""
    summary = summary.lower()
    if any(k in summary for k in ["urgent", "ceo", "meeting", "sync", "call", "interview"]): return 3
    if any(k in summary for k in ["gym", "lunch", "coffee", "break", "personal", "workout"]): return 1
    return 2

        
def _calculate_move_cost(event, original_start, new_start):
    """Calculates the 'pain' cost of moving an event with Elite weights and Time Bias."""
    priority = _eval_priority(event.get('summary', ''))
    
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
        e_s = original_start
        e_e = original_start

    duration_mins = (e_e - e_s).total_seconds() / 60
    shift_hours = abs((new_start - original_start).total_seconds()) / 3600
    w = CONFIG.cost_weights
    
    # Base Cost Components
    p_cost = (priority * w.priority)
    d_cost = (shift_hours * w.distance)
    dur_cost = (duration_mins * w.duration)
    
    # Apply Time Bias (Behavioral Preference)
    pref = CONFIG.preferences
    bias_cost = 0
    if pref.time_bias == 'morning' and new_start.hour >= 12:
        bias_cost = pref.bias_strength
    elif pref.time_bias == 'evening' and new_start.hour < 12:
        bias_cost = pref.bias_strength
        
    total = p_cost + d_cost + dur_cost + bias_cost
    
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


class OptimizationEngine:
    """Formal constraint optimization engine powered by Google OR-Tools."""
    
    @staticmethod
    def solve_scheduling_problem(service, new_event: EventDetails, conflicts: list, all_day_events: list):
        """Finds the globally optimal arrangement using CP-SAT solver."""
        model = cp_model.CpModel()
        local_tz = tz.gettz(CONFIG.timezone)
        
        # 1. Horizon & Granularity
        # We model the day in minutes from 00:00
        ref_date = parser.parse(new_event.start).replace(hour=0, minute=0, second=0, microsecond=0)
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
            p = _eval_priority(e.get('summary', ''))
            if p < 3 or e.get('id') in [c.get('id') for c in conflicts]:
                 movable_events.append(e)

        # 3. Define Variables
        vars = {}
        intervals = []
        
        # New Event (Target)
        new_start_min = to_min(new_event.start)
        new_end_min = to_min(new_event.end)
        new_dur = new_end_min - new_start_min
        
        # The new event is FIXED in its proposed slot for this optimization attempt
        new_interval = model.NewIntervalVar(new_start_min, new_dur, new_end_min, "new_event_interval")
        intervals.append(new_interval)

        # Other events
        for e in all_day_events:
            e_id = e.get('id')
            e_summary = e.get('summary', 'Unknown')
            e_start = to_min(e['start'].get('dateTime', e['start'].get('date')))
            e_end = to_min(e['end'].get('dateTime', e['end'].get('date')))
            e_dur = e_end - e_start
            
            p = _eval_priority(e_summary)
            
            if e in movable_events:
                # Movable Variable
                # Constraints: must stay within working hours and within max_shift
                lb = max(work_start_min, e_start - max_shift_min)
                ub = min(work_end_min, e_end + max_shift_min) - e_dur
                
                s_var = model.NewIntVar(lb, ub, f"start_{e_id}")
                end_var = model.NewIntVar(lb + e_dur, ub + e_dur, f"end_{e_id}")
                i_var = model.NewIntervalVar(s_var, e_dur, end_var, f"interval_{e_id}")
                
                # Distance cost helper: abs(start_var - original_start)
                diff = model.NewIntVar(-max_shift_min, max_shift_min, f"diff_{e_id}")
                abs_diff = model.NewIntVar(0, max_shift_min, f"abs_diff_{e_id}")
                model.Add(diff == s_var - e_start)
                model.AddAbsEquality(abs_diff, diff)
                
                vars[e_id] = {
                    "start": s_var, "abs_diff": abs_diff, "original_start": e_start,
                    "priority": p, "summary": e_summary, "duration": e_dur
                }
                intervals.append(i_var)
            else:
                # Fixed Anchor
                intervals.append(model.NewIntervalVar(e_start, e_dur, e_end, f"fixed_{e_id}"))

        # 4. Constraints
        model.AddNoOverlap(intervals)

        # 5. Objective: Minimize weighted disruption
        # cost = sum(priority_weight * P + distance_weight * shift + ...)
        w = CONFIG.cost_weights
        total_cost_vars = []
        for e_id, v in vars.items():
            # weight_p = v['priority'] * w.priority
            # weight_d = w.distance
            # Simplified cost for OR-Tools (must be integers)
            # We scale weights by 10 to preserve some precision
            item_cost = model.NewIntVar(0, 1000000, f"cost_{e_id}")
            model.Add(item_cost == int(v['priority'] * w.priority) * 10 + int(w.distance) * v['abs_diff'])
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
                if final_start != v['original_start']:
                    proposal_targets.append({
                        "id": e_id,
                        "summary": v['summary'],
                        "new_start": ref_date + datetime.timedelta(minutes=final_start),
                        "new_end": ref_date + datetime.timedelta(minutes=final_start + v['duration']),
                        "old_start": ref_date + datetime.timedelta(minutes=v['original_start']),
                        "breakdown": {} # Breakdown can be re-calculated if needed
                    })
            
            if not proposal_targets: return None
            
            return {
                "targets": proposal_targets,
                "cost": solver.ObjectiveValue() / 10.0,
                "reason": "Globally Optimized (OR-Tools)"
            }

        return None

def get_magic_fix_proposal(service, new_event: EventDetails, conflicts: list):
    """Refactored to use formal OptimizationEngine instead of brute heuristic."""
    if not conflicts: return None
    
    # 1. Fetch search context (all events for that day)
    local_tz = tz.gettz(CONFIG.timezone)
    start_dt = parser.parse(new_event.start).replace(hour=0, minute=0, second=0)
    end_dt = start_dt + datetime.timedelta(days=1)
    
    try:
        all_day_events = list_upcoming_events(service, start_dt.isoformat(), end_dt.isoformat())
        # Filter out the 'new_event' if it was somehow already there (though unlikely here)
        all_day_events = [e for e in all_day_events if e.get('summary') != new_event.title]
    except Exception as e:
        logger.error(f"Failed to fetch day context for OR-Tools: {e}")
        return None

    # 2. Invoke Solver
    proposal = OptimizationEngine.solve_scheduling_problem(service, new_event, conflicts, all_day_events)
    
    if proposal:
        # Re-calculate breakdowns for each target for UI transparency
        for t in proposal['targets']:
            _, breakdown = _calculate_move_cost(t, t['old_start'], t['new_start'])
            t['breakdown'] = breakdown
        return proposal

    return None


class LearningEngine:
    """Adaptive intelligence that learns from user scheduling feedback."""
    
    @staticmethod
    def apply_feedback(accepted: bool, proposal: dict):
        lr = CONFIG.learning_rate
        w = CONFIG.cost_weights
        
        # Track statistics in BehaviorState
        if accepted:
            CONFIG.behavior.accepted_fixes += 1
        else:
            CONFIG.behavior.rejected_fixes += 1
        CONFIG.behavior.total_moves += 1
        CONFIG.behavior.last_interaction = datetime.datetime.now().isoformat()
        
        targets = proposal.get('targets', [])
        if not targets: return

        # Simple Reinforcement: Adjust weights based on component contribution
        for t in targets:
            b = t.get('breakdown', {})
            if not b: continue
            
            if not accepted:
                # User rejected this move -> Increase 'pain' perception for what we moved
                # Normalizing values to avoid explosive growth
                w.priority += lr * (b['priority_val'] / 2.0)
                w.distance += lr * min(b['distance_val'], 4.0)
                w.duration += lr * (b['duration_val'] / 45.0)
                if b['bias'] > 0:
                    CONFIG.preferences.bias_strength += lr * 2
                logger.info("Adaptive Engine: User rejected proposal. Increasing avoidance weights.")
            else:
                # User accepted -> Validate current weights or slightly nudge down for efficiency
                w.priority = max(10.0, w.priority - (lr * 0.2))
                w.distance = max(2.0, w.distance - (lr * 0.2))
                w.duration = max(0.1, w.duration - (lr * 0.05))
                logger.info("Adaptive Engine: User accepted proposal. Refining scheduling efficiency.")

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

