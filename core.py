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
                    except:
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

    raise ValueError("Could not understand the command. No patterns matched and AI failed.")


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
        except:
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
    duration_mins = (parser.parse(event['end'].get('dateTime', event['end'].get('date'))) - 
                     parser.parse(event['start'].get('dateTime', event['start'].get('date')))).total_seconds() / 60
    
    shift_hours = abs((new_start - original_start).total_seconds()) / 3600
    w = CONFIG.cost_weights
    
    # Base Cost: Weight * Value
    cost = (priority * w.get('priority', 25.0)) + \
           (shift_hours * w.get('distance', 8.0)) + \
           (duration_mins * w.get('duration', 0.5))
    
    # Apply Time Bias (Behavioral Preference)
    pref = CONFIG.preferences
    bias = pref.get('time_bias', 'morning')
    strength = pref.get('bias_strength', 10.0)
    
    if bias == 'morning' and new_start.hour >= 12:
        cost += strength
    elif bias == 'evening' and new_start.hour < 12:
        cost += strength
        
    return cost


def get_magic_fix_proposal(service, new_event: EventDetails, conflicts: list):
    """Checks if an Elite Magic Fix (auto-reschedule) is possible."""
    if not conflicts: return None
    
    # Step 1: Identify movable events
    movable = []
    for c in conflicts:
        if _eval_priority(c.get('summary', '')) < new_event.priority:
            movable.append(c)
        else:
            return None
            
    if len(movable) < len(conflicts): return None
    
    proposals = []
    
    # Step 2: Try single-move solutions
    for target in movable:
        t_s = parser.parse(target['start'].get('dateTime', target['start'].get('date')))
        t_e = parser.parse(target['end'].get('dateTime', target['end'].get('date')))
        dur = int((t_e - t_s).total_seconds() / 60)
        
        if len(conflicts) == 1:
            suggestions = find_free_slots(service, new_event.end, min_duration_mins=dur)
            for slot_start, slot_end in suggestions[:3]:
                if check_conflicts(service, slot_start.isoformat(), slot_end.isoformat()): continue
                    
                cost = _calculate_move_cost(target, t_s, slot_start)
                if (slot_start - t_s).total_seconds() / 3600 > 6: continue
                    
                proposals.append({
                    "targets": [{
                        "id": target['id'], "summary": target['summary'],
                        "new_start": slot_start, "new_end": slot_end, "old_start": t_s
                    }],
                    "cost": cost,
                    "reason": f"Optimal single move (Cost: {int(cost)})"
                })

    # Step 3: Try multi-move solutions (Explore Top 3 Gap Combinations)
    if len(movable) > 1:
        total_dur = sum((parser.parse(c['end'].get('dateTime', c['end'].get('date'))) - 
                        parser.parse(c['start'].get('dateTime', c['start'].get('date')))).total_seconds() / 60 for c in movable)
        
        combined_suggestions = find_free_slots(service, new_event.end, min_duration_mins=int(total_dur))
        for s_start, _ in combined_suggestions[:3]: # Non-Linear: Explore different gaps
            current_ptr = s_start
            multi_targets = []
            valid_multi = True
            for target in movable:
                t_s = parser.parse(target['start'].get('dateTime', target['start'].get('date')))
                t_e = parser.parse(target['end'].get('dateTime', target['end'].get('date')))
                t_dur = t_e - t_s
                
                new_t_s = current_ptr
                new_t_e = current_ptr + t_dur
                
                if (new_t_s - t_s).total_seconds() / 3600 > 6:
                    valid_multi = False; break
                
                if check_conflicts(service, new_t_s.isoformat(), new_t_e.isoformat()):
                     valid_multi = False; break

                multi_targets.append({
                    "id": target['id'], "summary": target['summary'],
                    "new_start": new_t_s, "new_end": new_t_e, "old_start": t_s
                })
                current_ptr = new_t_e
            
            if valid_multi:
                total_cost = sum(_calculate_move_cost(c, parser.parse(c['start'].get('dateTime', c['start'].get('date'))), mt['new_start']) 
                               for c, mt in zip(movable, multi_targets))
                proposals.append({
                    "targets": multi_targets,
                    "cost": (total_cost / len(movable)) + 5, # Complexity penalty
                    "reason": f"Elite Chain Fix: Optimized across multiple gaps (Cost: {int(total_cost)})"
                })

    if not proposals: return None
    return min(proposals, key=lambda p: p['cost'])



def find_free_slots(service, start_search: str, min_duration_mins=None):
    try:
        local_tz = tz.gettz(CONFIG.timezone)
        search_dt = parser.parse(start_search)
        if search_dt.tzinfo is None: search_dt = search_dt.replace(tzinfo=local_tz)
        
        now = datetime.datetime.now(local_tz)
        if search_dt < now: search_dt = now
        
        max_search = (search_dt + datetime.timedelta(days=7)).replace(hour=23, minute=59)
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
            if work_start < now: work_start = now
            
            # Check if there are ANY events for this specific day
            day_busy = [b for b in busy if b[0].date() == day_target.date()]
            
            if not day_busy:
                # No events at all? Show full 24h block
                f_s = day_target.replace(hour=0, minute=0, second=0, tzinfo=local_tz)
                f_e = day_target.replace(hour=23, minute=59, second=59, tzinfo=local_tz)
                if f_e > now: # Only add if the day hasn't passed
                    free_blocks.append((max(now, f_s), f_e))
                continue

            current_ptr = work_start
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
    except Exception as e:
        logger.error(f"Free Block Error: {e}"); return []

