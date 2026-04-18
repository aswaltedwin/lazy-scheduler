import datetime
from dateutil import tz
from unittest.mock import MagicMock
from core import find_free_slots
from config import CONFIG

def test_find_free_slots_no_busy_data(mock_service):
    CONFIG.timezone = "Asia/Kolkata"
    CONFIG.working_start = 9
    CONFIG.working_end = 19
    local_tz = tz.gettz(CONFIG.timezone)
    
    # Mock freebusy response with no busy intervals
    mock_service.freebusy().query().execute.return_value = {
        'calendars': {
            'primary': {
                'busy': []
            }
        }
    }
    now = datetime.datetime.now(local_tz)
    day_target = now + datetime.timedelta(days=1)
    # Search starting tomorrow at 10 AM
    start_search = day_target.replace(hour=10, minute=0, second=0, microsecond=0).isoformat()
    
    slots = find_free_slots(mock_service, start_search)
    
    assert len(slots) > 0
    # First slot should start at the search time and go to end of working day
    s_start, s_end = slots[0]
    assert s_start.hour == 10
    assert s_end.hour == 19

def test_find_free_slots_with_busy_interval(mock_service):
    CONFIG.timezone = "Asia/Kolkata"
    CONFIG.working_start = 9
    CONFIG.working_end = 19
    local_tz = tz.gettz(CONFIG.timezone)
    
    now = datetime.datetime.now(local_tz)
    day_target = now + datetime.timedelta(days=1)
    
    # Busy from 12:00 to 14:00 tomorrow
    b_s = day_target.replace(hour=12, minute=0, second=0, microsecond=0)
    b_e = day_target.replace(hour=14, minute=0, second=0, microsecond=0)
    
    mock_service.freebusy().query().execute.return_value = {
        'calendars': {
            'primary': {
                'busy': [{'start': b_s.isoformat(), 'end': b_e.isoformat()}]
            }
        }
    }
    
    # Search starting tomorrow at 9 AM
    start_search = day_target.replace(hour=9, minute=0, second=0, microsecond=0).isoformat()
    slots = find_free_slots(mock_service, start_search)
    
    # Should find slots 9-12 and 14-19 (among others)
    # Filter for tomorrow's slots
    tomorrow_slots = [s for s in slots if s[0].date() == day_target.date()]
    
    # 9-12 gap
    assert any(s[0].hour == 9 and s[1].hour == 12 for s in tomorrow_slots)
    # 14-19 gap
    assert any(s[0].hour == 14 and s[1].hour == 19 for s in tomorrow_slots)
