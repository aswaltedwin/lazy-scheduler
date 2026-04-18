import pytest
import datetime
from unittest.mock import MagicMock, patch
from models import UserConfig
from config import CONFIG

@pytest.fixture
def mock_service():
    """Mock Google Calendar API service."""
    service = MagicMock()
    
    # Setup common return values to avoid errors
    service.events().list().execute.return_value = {"items": []}
    service.freebusy().query().execute.return_value = {"calendars": {"primary": {"busy": []}}}
    
    return service

@pytest.fixture
def mock_ollama():
    """Mock Ollama chat responses."""
    with patch("ollama.chat") as mocked:
        mocked.return_value = {
            "message": {
                "content": '{"action": "create", "title": "Test Event", "start": "2026-04-18T10:00:00", "end": "2026-04-18T11:00:00"}'
            }
        }
        yield mocked

@pytest.fixture(autouse=True)
def reset_config():
    """Ensure CONFIG is reset to defaults before each test."""
    original_weights = CONFIG.cost_weights.model_copy()
    original_prefs = CONFIG.preferences.model_copy()
    
    yield
    
    CONFIG.cost_weights = original_weights
    CONFIG.preferences = original_prefs
    CONFIG.working_start = 9
    CONFIG.working_end = 19
    CONFIG.max_shift_hours = 6
