import os
import json
from pydantic import BaseModel

class UserConfig(BaseModel):
    model: str = "qwen2.5:7b"
    timezone: str = "Asia/Kolkata"
    working_start: int = 9
    working_end: int = 19
    default_duration: int = 45
    cost_weights: dict = {"priority": 25.0, "distance": 8.0, "duration": 0.5}
    preferences: dict = {"time_bias": "morning", "bias_strength": 10.0}

class EventDetails(BaseModel):
    action: str = "create"
    title: str = ""
    start: str = ""
    end: str = ""
    description: str = ""
    location: str = ""
    attendees: list[str] = []
    add_meeting: bool = False
    search_query: str = ""
    recurrence: list[str] = []
    priority: int = 2

class SessionState:
    last_event: EventDetails = None
    last_raw_input: str = ""

def load_config():
    if os.path.exists('config.json'):
        with open('config.json', 'r') as f:
            data = json.load(f)
            return UserConfig(
                model=data.get('model', 'qwen2.5:7b'),
                timezone=data.get('timezone', 'Asia/Kolkata'),
                working_start=data.get('working_hours', {}).get('start', 9),
                working_end=data.get('working_hours', {}).get('end', 19),
                default_duration=data.get('default_duration', 45),
                cost_weights=data.get('cost_weights', {"priority": 25.0, "distance": 8.0, "duration": 0.5}),
                preferences=data.get('preferences', {"time_bias": "morning", "bias_strength": 10.0})
            )
    return UserConfig()

CONFIG = load_config()
STATE = SessionState()

