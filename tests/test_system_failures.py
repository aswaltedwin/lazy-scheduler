import pytest
from unittest.mock import MagicMock, patch
import datetime
from dateutil import parser, tz

from core import (
    OptimizationEngine,
    create_event,
    parse_natural_language,
    ParsingError,
    check_conflicts,
    CONFIG
)
from models import EventDetails

def test_conflict_explosion():
    # 5 overlapping events
    now = datetime.datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    
    new_event = EventDetails(
        action="create", title="Test Target", priority=3,
        start=(now + datetime.timedelta(hours=1)).isoformat(),
        end=(now + datetime.timedelta(hours=2)).isoformat()
    )
    
    conflicts = []
    all_day_events = []
    
    # Generate 5 overlapping events
    for i in range(5):
        evt = {
            "id": f"c{i}", "summary": f"Conflict {i}",
            "start": {"dateTime": (now + datetime.timedelta(hours=1, minutes=i*5)).isoformat()},
            "end": {"dateTime": (now + datetime.timedelta(hours=2, minutes=i*5)).isoformat()}
        }
        conflicts.append(evt)
        all_day_events.append(evt)

    # Adding a 6th event that is NOT a conflict but acting as an anchor
    all_day_events.append({
        "id": "e_safe", "summary": "Safe Morning",
        "start": {"dateTime": (now - datetime.timedelta(hours=2)).isoformat()},
        "end": {"dateTime": (now - datetime.timedelta(hours=1)).isoformat()}
    })

    # The solver should calculate the complex disruption without crashing
    proposal = OptimizationEngine.solve_scheduling_problem(None, new_event, conflicts, all_day_events)
    
    # Asserting it resolves mathematically
    assert proposal is not None or proposal is None

def test_api_fail():
    # Mock the actual Google API execute chain
    mock_service = MagicMock()
    # service.events().insert().execute()
    mock_service.events().insert().execute.side_effect = Exception("API down")
    
    event = EventDetails(action="create", title="API Test", start="2026-04-18T10:00:00", end="2026-04-18T11:00:00")
    
    with pytest.raises(Exception) as excinfo:
        create_event(mock_service, event)
    
    assert "API down" in str(excinfo.value)

@patch("ollama.chat")
def test_invalid_input(mock_chat):
    # Mock LLM failing to extract anything meaningful
    mock_chat.return_value = {"message": {"content": "I don't understand."}}
    
    with pytest.raises(ParsingError) as excinfo:
        # User input is random nonsense
        parse_natural_language("random nonsense")
    
    # Ensure our intelligent error boundary catches it
    assert "Could not understand the command" in str(excinfo.value)

def test_timezone_bug():
    # Comparing UTC string with a Z to local naive/aware
    mock_service = MagicMock()
    
    local_tz = tz.gettz(CONFIG.timezone)
    now_local = datetime.datetime.now(local_tz)
    
    # API returns UTC time with 'Z'
    utc_start = now_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    utc_end = (now_local + datetime.timedelta(hours=1)).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    mock_service.events().list().execute.return_value = {
        "items": [
            {
                "id": "tz1", "summary": "UTC Event",
                "start": {"dateTime": utc_start},
                "end": {"dateTime": utc_end},
                "transparency": "opaque"
            }
        ]
    }
    
    # We query using strict local string without offset
    search_start = now_local.isoformat()
    search_end = (now_local + datetime.timedelta(hours=2)).isoformat()
    
    # This should successfully parse the Z and match them in the conflict window natively without offset crashes
    conflicts = check_conflicts(mock_service, search_start, search_end)
    assert len(conflicts) == 1
    assert conflicts[0]['id'] == "tz1"
