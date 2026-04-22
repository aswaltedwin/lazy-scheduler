import datetime
from typing import Dict, Any
from dateutil import parser, tz
from config import CONFIG, PROFILE
from services.scoring import PriorityScorer

class AdaptiveStrategicPartner:
    """The tactical brain behind human-aware scheduling and schedule health."""
    
    @staticmethod
    def evaluate(intent: Any, profile: Any) -> Dict[str, Any]:
        """Performs a strategic workload health check."""
        # Simulation of workload check
        # In real life, we would count hours for today/tomorrow
        return {
            "overload": False, 
            "burnout_risk": "low",
            "capacity_remaining": 4.5
        }

    @staticmethod
    def assess_workload(events: list) -> Dict[str, Any]:
        """Calculates total work hours and flags human limits."""
        total_mins = 0
        for e in events:
            s = parser.parse(e['start'].get('dateTime', e['start'].get('date')))
            end = parser.parse(e['end'].get('dateTime', e['end'].get('date')))
            total_mins += (end - s).total_seconds() / 60
        
        total_hours = total_mins / 60
        return {
            "total_hours": total_hours,
            "is_overloaded": total_hours > CONFIG.max_work_hours,
            "overload_ratio": total_hours / CONFIG.max_work_hours if CONFIG.max_work_hours > 0 else 0
        }
