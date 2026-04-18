from pydantic import BaseModel, Field
from typing import List, Dict, Optional

class CostWeights(BaseModel):
    priority: float = 25.0
    distance: float = 8.0
    duration: float = 0.5

class UserPreferences(BaseModel):
    time_bias: str = "morning"
    bias_strength: float = 10.0

class BehaviorState(BaseModel):
    accepted_fixes: int = 0
    rejected_fixes: int = 0
    total_moves: int = 0
    last_interaction: Optional[str] = None

class UserConfig(BaseModel):
    model: str = "qwen2.5:7b"
    timezone: str = "Asia/Kolkata"
    working_start: int = 9
    working_end: int = 19
    default_duration: int = 45
    max_shift_hours: int = 6
    log_level: str = "INFO"
    learning_rate: float = 0.5
    cost_weights: CostWeights = Field(default_factory=CostWeights)
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    behavior: BehaviorState = Field(default_factory=BehaviorState)

class EventDetails(BaseModel):
    action: str = Field("create", description="Action to perform: create, list, delete, update, find_slot")
    title: str = Field("", description="Summarized title of the event")
    start: str = Field("", description="ISO format start time")
    end: str = Field("", description="ISO format end time")
    description: str = Field("", description="Detailed notes or description")
    location: str = Field("", description="Physical or virtual location")
    attendees: List[str] = Field(default_factory=list, description="List of email addresses")
    add_meeting: bool = Field(False, description="Whether to add a Google Meet link")
    search_query: str = Field("", description="Query string for finding events to delete or update")
    recurrence: List[str] = Field(default_factory=list, description="RFC5545 RRULE string")
    reminders_minutes: List[int] = Field(default_factory=lambda: [15], description="Minutes before event for popup reminders")
    duration_mins: int = Field(0, description="Minimum duration in minutes for finding a free slot")
    priority: int = Field(2, description="Priority: 1=Low, 2=Normal, 3=High")

class SessionState(BaseModel):
    last_event: Optional[EventDetails] = None
    last_raw_input: str = ""
