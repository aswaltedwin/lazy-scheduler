import datetime
from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Any

class CostWeights(BaseModel):
    priority: float = 25.0
    distance: float = 8.0
    duration: float = 0.5
    # Scoring Weights
    priority_weight: float = 2.0
    urgency_weight: float = 1.5
    effort_weight: float = 1.0
    overload_penalty: float = 50.0 # Penalty for exceeding human limits

class UserPreferences(BaseModel):
    time_bias: str = "morning"
    bias_strength: float = 10.0

class BehaviorState(BaseModel):
    accepted_fixes: int = 0
    rejected_fixes: int = 0
    total_moves: int = 0
    last_interaction: Optional[str] = None
    last_reevaluation: Optional[str] = None

class UserProfile(BaseModel):
    preferred_hours: List[int] = Field(default_factory=lambda: [9, 10, 11])
    avoid_hours: List[int] = Field(default_factory=lambda: [13])
    priority_bias: float = 1.2
    accepted_moves: List[str] = Field(default_factory=list)
    rejected_moves: List[str] = Field(default_factory=list)
    time_preferences: Dict[str, float] = Field(default_factory=lambda: {
        "morning": 0.0, "afternoon": 0.0, "evening": 0.0, "night": 0.0
    })
    time_hits: Dict[str, int] = Field(default_factory=lambda: {
        "morning": 0, "afternoon": 0, "evening": 0, "night": 0
    })
    time_misses: Dict[str, int] = Field(default_factory=lambda: {
        "morning": 0, "afternoon": 0, "evening": 0, "night": 0
    })
    energy_profile: Dict[str, float] = Field(default_factory=lambda: {
        "morning": 1.0, "afternoon": 0.7, "evening": 0.5, "night": 0.3
    })
    locked_titles: Dict[str, int] = Field(default_factory=dict)
    task_history: Dict[str, Dict[str, Any]] = Field(default_factory=dict) # {title: {completed, missed, last_scheduled}}
    dominance_threshold: int = 5
    hard_dominance_threshold: int = 10

class UserConfig(BaseModel):
    model: str = "qwen2.5:7b"
    timezone: str = "Asia/Kolkata"
    working_start: int = 9
    working_end: int = 19
    default_duration: int = 45
    max_work_hours: int = 6 # Total active task time per day
    break_interval: int = 2 # Hours of work before a break is needed
    break_duration: int = 15 # Minutes
    reevaluation_interval: int = 2 # Hours
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
    momentum_score: float = Field(0.0, description="Internal score for task prioritization")
    constraint: str = Field("soft", description="Constraint level: hard (non-negotiable) or soft (flexible)")
    deadline: Optional[str] = Field(None, description="ISO format deadline date or time")
    effort: int = Field(3, description="Estimated effort: 1=Low, 3=Normal, 5=High")
    energy_cost: int = Field(3, description="Cognitive load: 1=Low, 3=Normal, 5=High")
    actual_duration: Optional[int] = Field(None, description="Actual time taken in minutes")
    is_decomposed: bool = Field(False, description="Whether this task was split from a larger one")
    status: str = Field("pending", description="Task state: pending, completed, missed")
    intelligence: Optional[Dict[str, Any]] = Field(None, description="Extra LLM-extracted metadata")

class SessionState(BaseModel):
    last_event: Optional[EventDetails] = None
    last_reevaluation: Optional[datetime.datetime] = None
    last_raw_input: str = ""
    last_question: Optional[str] = None
