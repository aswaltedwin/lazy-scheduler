import re
import datetime
from dateutil import parser, tz
from models import EventDetails
from services.scoring import PriorityScorer
from config import CONFIG

class Sanitizer:
    """Cleans and prepares raw user input for parsing."""
    @staticmethod
    def sanitize_text(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r'\s+', ' ', text)
        return text

class RuleBasedParser:
    """Extracts task data using semantic rules and regex."""
    @staticmethod
    def parse_input(text: str, context=None) -> EventDetails:
        raw_text = text
        text = Sanitizer.sanitize_text(text)
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        
        # 1. Action Detection
        action = "create"
        greetings = ["hi", "hello", "hey", "sup", "how are you", "good morning", "good afternoon", "good evening", "good night"]
        if any(w in text for w in greetings):
            action = "greet"
        elif any(w in text for w in ["when is", "what is", "is there", "find", "check my", "list"]):
            action = "find"
        elif any(w in text for w in ["optimize", "day check", "fix my day", "clean my day"]):
            action = "optimize_day"
        elif any(w in text for w in ["delete", "remove", "cancel", "clear"]):
            action = "delete"
        elif any(w in text for w in ["update", "move", "change", "shift"]):
            action = "update"

        # 2. Date/Time Detection
        now = datetime.datetime.now(local_tz)
        target_date = now.replace(minute=0, second=0, microsecond=0)
        
        is_tomorrow = "tomorrow" in text
        if is_tomorrow:
            target_date = target_date + datetime.timedelta(days=1)
        elif "next week" in text:
            target_date = target_date + datetime.timedelta(days=7)
            
        # 3. Duration Extraction (e.g. 'for 45 mins', '45 mins', '1 hour')
        duration_mins = 60
        duration_pattern = re.search(r'\b(?:for\s+)?(\d+)\s*(mins?|minutes?|hrs?|hours?)\b', text, flags=re.IGNORECASE)
        matched_duration_str = ""
        if duration_pattern:
            matched_duration_str = duration_pattern.group(0)
            val = int(duration_pattern.group(1))
            unit = duration_pattern.group(2).lower()
            if "h" in unit:
                duration_mins = val * 60
            else:
                duration_mins = val

        # 4. Time Extraction
        start = target_date + datetime.timedelta(hours=1)
        time_match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b', text, flags=re.IGNORECASE)
        if not time_match:
            time_match = re.search(r'(?:at\s+)(\d{1,2}(?::\d{2})?)', text, flags=re.IGNORECASE)

        if time_match:
            try:
                # Parse the time found in text
                start = parser.parse(time_match.group(1), default=target_date)
                
                # Timezone correction: If time has passed today and user didn't say 'tomorrow',
                # assume they mean tomorrow.
                if not is_tomorrow and action == "create" and start < now:
                    start = start + datetime.timedelta(days=1)
            except Exception: pass
            
        end = start + datetime.timedelta(minutes=duration_mins)

        # 5. Title Extraction
        title = "New Task"
        clean_title = raw_text
        fillers = [
            "delete", "remove", "cancel", "clear", "update", "move", "change", 
            "at", "on", "tomorrow", "today", "my", "fully", "all", "everything", 
            "session", "sessions", "when", "is", "there", "what", "find", "check", 
            "good", "morning", "afternoon", "evening", "for"
        ]
        if matched_duration_str:
            clean_title = re.sub(re.escape(matched_duration_str), '', clean_title, flags=re.IGNORECASE)
        if time_match:
            clean_title = re.sub(re.escape(time_match.group(0)), '', clean_title, flags=re.IGNORECASE)

        for word in fillers:
            clean_title = re.sub(rf'\b{word}\b', '', clean_title, flags=re.IGNORECASE)
            
        clean_title = re.sub(r'\s+', ' ', clean_title).strip()
        title = clean_title.title()
        if not title: title = "Untitled Task"

        if action == "delete" and any(w in text for w in ["everything", "all", "every"]):
            title = "ALL_EVENTS"

        priority = PriorityScorer.calculate_priority(title)

        return EventDetails(
            title=title,
            action=action,
            start=start.isoformat(),
            end=end.isoformat(),
            priority=priority,
            search_query=title 
        )

    @staticmethod
    def fallback_parser(text: str) -> EventDetails:
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        now = datetime.datetime.now(local_tz).replace(minute=0, second=0, microsecond=0)
        start = now + datetime.timedelta(hours=1)
        return EventDetails(
            title=text[:30].strip().title() if text else "Manual Task",
            action="create",
            start=start.isoformat(),
            end=(start + datetime.timedelta(hours=1)).isoformat(),
            priority=2
        )

class Validator:
    """Ensures task data meets system constraints."""
    @staticmethod
    def validate_event(event: EventDetails) -> bool:
        if event.action in ["greet", "find", "optimize_day"]:
            return True
        if event.title == "ALL_EVENTS" and event.action == "delete":
            return True
        if not event.title or event.title == "Untitled Task":
            return False
        try:
            s = parser.parse(event.start)
            e = parser.parse(event.end)
            if s >= e: return False
        except: return False
        return True
