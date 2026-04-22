import datetime
from typing import List, Optional
from dateutil import parser, tz
from config import CONFIG, PROFILE
from models import EventDetails
from services.scoring import ScoringEngine, PriorityScorer

class DecisionEngine:
    """Explains why the scheduler made a choice."""

    @staticmethod
    def get_task_explanation(task: dict, event: EventDetails) -> List[str]:
        """Provides simple reasons for scheduling decisions."""
        reasons = []
        
        if event.priority >= 4:
            reasons.append("This is a high priority task.")
        
        if event.deadline:
            try:
                local_tz = tz.gettz(CONFIG.timezone)
                deadline_dt = parser.parse(event.deadline).replace(tzinfo=local_tz)
                hours_left = (deadline_dt - datetime.datetime.now(local_tz)).total_seconds() / 3600
                if 0 <= hours_left <= 24:
                    reasons.append("The deadline is very soon.")
            except: pass
            
        title_lower = event.title.lower()
        if hasattr(PROFILE, "task_history"):
            for history_title, data in PROFILE.task_history.items():
                if history_title in title_lower:
                    if data.get("missed", 0) >= 1:
                        reasons.append("You missed this task before, so we are prioritizing it now.")
        
        return reasons

    @staticmethod
    def build_proposal_explanation(proposal: dict):
        """Simple reasoning for a set of changes."""
        return ["Trying to fit everything in with the least changes.", "Balancing your day."]
