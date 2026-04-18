import pytest
from unittest.mock import MagicMock, patch
from core import (
    list_upcoming_events, 
    _validate_and_transform_response, 
    parse_natural_language,
    get_magic_fix_proposal
)
from googleapiclient.errors import HttpError

def test_api_failure_handling(mock_service):
    # Simulate a 503 Service Unavailable from Google
    mock_service.events().list.side_effect = Exception("503 Service Unavailable")
    
    with pytest.raises(Exception) as excinfo:
        list_upcoming_events(mock_service, "2026-04-18T10:00:00", "2026-04-18T11:00:00")
    
    assert "503" in str(excinfo.value)

def test_malformed_llm_json():
    # Test how _validate_and_transform_response handles garbage input
    import datetime
    now = datetime.datetime.now()
    
    # 1. Not even JSON
    result = _validate_and_transform_response("This is not JSON", now)
    assert result is None
    
    # 2. JSON but missing required fields (should use defaults or handle gracefully)
    result = _validate_and_transform_response('{"something": "else"}', now)
    assert result is not None
    assert result.title == "New Event" # Default applied

@patch("ollama.chat")
def test_ollama_runtime_error(mock_chat):
    # Simulate Ollama being down or unreachable
    mock_chat.side_effect = Exception("Connection refused")
    
    # parse_natural_language should catch this and potentially retry or raise if it fails
    # Based on core.py, it logs and raises ValueError if all paths fail
    with pytest.raises(ValueError) as excinfo:
        parse_natural_language("Create a meeting")
    
    assert "Could not understand the command" in str(excinfo.value)

def test_magic_fix_no_solution(mock_service):
    # Setup a scenario where no solution is possible (high priority conflict)
    from models import EventDetails
    new_event = EventDetails(title="CEO Sync", priority=3, start="2026-04-18T10:00:00", end="2026-04-18T11:00:00")
    
    # Existing event also high priority
    conflicts = [{
        "id": "1", "summary": "Urgent Board Meeting",
        "start": {"dateTime": "2026-04-18T10:00:00"},
        "end": {"dateTime": "2026-04-18T11:00:00"}
    }]
    
    # This should return None because the solver won't move P3 vs P3 by default 
    # (unless we allow it, but current implementation treats P3 as anchors unless in conflict list)
    # Actually, in OptimizationEngine.solve_scheduling_problem, we collect candidates.
    
    proposal = get_magic_fix_proposal(mock_service, new_event, conflicts)
    assert proposal is None
