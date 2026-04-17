import datetime
from dateutil import tz
from core import RuleBasedParser
from config import CONFIG
from models import EventDetails

def test_rule_based_parser_list_tomorrow():
    CONFIG.timezone = "Asia/Kolkata"
    local_tz = tz.gettz(CONFIG.timezone)
    now = datetime.datetime.now(local_tz)
    
    result = RuleBasedParser.parse("show my schedule tomorrow")
    
    assert result is not None
    assert result.action == "list"
    
    # Verify the date range is tomorrow
    tomorrow_start = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert result.start.startswith(tomorrow_start.isoformat()[:10])

def test_rule_based_parser_find_free_time():
    result = RuleBasedParser.parse("any free slots today?")
    assert result is not None
    assert result.action == "find_slot"
    assert result.duration_mins == 0

def test_rule_based_parser_delete_simple():
    result = RuleBasedParser.parse("delete lunch")
    assert result is not None
    assert result.action == "delete"
    assert result.search_query == "lunch"

def test_rule_based_parser_complex_intent_falls_through():
    # This should return None to let the AI handle it
    result = RuleBasedParser.parse("i want to clear everything from my calendar")
    assert result is None
