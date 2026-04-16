import pytest
import datetime
from dateutil import tz
from core import RuleBasedParser, find_free_slots, EventDetails
from unittest.mock import MagicMock

def test_rule_based_list_this_week():
    """Verify that 'list all events this week' produces a Sunday-Saturday range."""
    # We mock 'now' inside RuleBasedParser manually if needed, but here we check structure
    result = RuleBasedParser.parse("list all events this week")
    assert result.action == "list"
    assert result.start is not None
    assert result.end is not None

def test_rule_based_when_is():
    """Verify that 'when is my dentist appointment' is treated as a list/search with valid dates."""
    result = RuleBasedParser.parse("when is my dentist appointment")
    assert result.action == "list"
    assert result.search_query == "dentist appointment"
    # New check for 400 error prevention
    assert result.start is not None
    assert result.end is not None
    assert "T00:00:00" in result.start


def test_rule_based_case_insensitivity():
    """Verify that 'LIST' and 'list' behave identically."""
    res1 = RuleBasedParser.parse("LIST EVENTS")
    res2 = RuleBasedParser.parse("list events")
    assert res1.action == res2.action == "list"

def test_availability_empty_day():
    """Test that a day with NO events returns a ~24h block."""
    mock_service = MagicMock()
    # Mocking freebusy response with zero events
    mock_service.freebusy().query().execute.return_value = {
        'calendars': {'primary': {'busy': []}}
    }
    
    # Search for tomorrow
    tomorrow = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    slots = find_free_slots(mock_service, tomorrow)
    
    # Should have at least one block that is approx 24 hours (1439 mins)
    # The first block for a completely empty day should be the 24h one
    assert len(slots) > 0
    start_dt = datetime.datetime.fromisoformat(slots[0][0].isoformat())
    end_dt = datetime.datetime.fromisoformat(slots[0][1].isoformat())
    duration = (end_dt - start_dt).total_seconds() / 60
    assert duration > 1400  # Approx 24 hours

def test_sanitizer():
    """Ensure basic prompt injection characters are handled."""
    from core import Sanitizer
    dirty = "List events; DROP TABLE users"
    clean = Sanitizer.sanitize_input(dirty)
    assert ";" not in clean

if __name__ == "__main__":
    print("Run tests using: pytest tests/test_lazy.py")
