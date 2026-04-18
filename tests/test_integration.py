import pytest
import datetime
from models import EventDetails
from core import OptimizationEngine, get_magic_fix_proposal, CONFIG

def test_or_tools_ripple_shift(mock_service):
    # Setup a target day
    from dateutil import tz
    local_tz = tz.gettz(CONFIG.timezone)
    ref_date = datetime.datetime(2026, 4, 18, 0, 0, 0, tzinfo=local_tz)
    
    # New high priority event
    new_event = EventDetails(
        title="Urgent Board Meeting",
        start=ref_date.replace(hour=10, minute=15).isoformat(),
        end=ref_date.replace(hour=11, minute=15).isoformat(),
        priority=3
    )
    
    # Existing low priority events that overlap with each other and the new event
    all_day_events = [
        {
            "id": "A", "summary": "Gym",
            "start": {"dateTime": ref_date.replace(hour=10, minute=0).isoformat()},
            "end": {"dateTime": ref_date.replace(hour=11, minute=0).isoformat()}
        },
        {
            "id": "B", "summary": "Lunch",
            "start": {"dateTime": ref_date.replace(hour=10, minute=30).isoformat()},
            "end": {"dateTime": ref_date.replace(hour=11, minute=30).isoformat()}
        }
    ]
    
    # Conflict List (detected by check_conflicts in real flow)
    conflicts = all_day_events
    
    # Mock list_upcoming_events to return the day context
    with unittest_patch("core.list_upcoming_events") as mock_list:
        mock_list.return_value = all_day_events
        
        proposal = get_magic_fix_proposal(mock_service, new_event, conflicts)
        
        assert proposal is not None
        assert proposal["reason"] == "Globally Optimized (OR-Tools)"
        assert len(proposal["targets"]) >= 2
        
        # Verify no overlaps in the proposed slots
        proposed_slots = [(t["new_start"], t["new_end"]) for t in proposal["targets"]]
        # Add the new event itself
        proposed_slots.append((datetime.datetime.fromisoformat(new_event.start), datetime.datetime.fromisoformat(new_event.end)))
        
        proposed_slots.sort()
        for i in range(len(proposed_slots) - 1):
            assert proposed_slots[i][1] <= proposed_slots[i+1][0]

def test_full_workflow_simulation(mock_service, mock_ollama):
    # Simulate: User says "Move my gym to 10am" but there is a conflict
    # 1. Parse NL
    from core import parse_natural_language, check_conflicts
    mock_ollama.return_value = {
        "message": {
            "content": '{"action": "create", "title": "Gym", "start": "2026-04-18T10:00:00", "end": "2026-04-18T11:00:00", "priority": 1}'
        }
    }
    
    event = parse_natural_language("Gym at 10am")
    assert event.title == "Gym"
    
    # 2. Check Conflicts
    mock_service.events().list().execute.return_value = {
        "items": [{
            "id": "work", "summary": "Important Work",
            "start": {"dateTime": "2026-04-18T10:30:00"},
            "end": {"dateTime": "2026-04-18T11:30:00"}
        }]
    }
    
    busy = check_conflicts(mock_service, event.start, event.end)
    assert len(busy) == 1
    
    # 3. get_magic_fix_proposal should suggest moving 'Important Work' since Gym is now competing
    # Actually if Gym is P1 and Work is P2 (default), Gym won't move Work.
    # Let's say Gym is P3
    event.priority = 3
    
    with unittest_patch("core.list_upcoming_events") as mock_list:
        mock_list.return_value = busy
        proposal = get_magic_fix_proposal(mock_service, event, busy)
        assert proposal is not None

from unittest.mock import patch as unittest_patch
import unittest.mock
