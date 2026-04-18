import os
import json
import logging
from models import UserConfig, SessionState

logger = logging.getLogger("LazyScheduler")

def load_config():
    """Loads configuration from config.json or returns defaults."""
    if os.path.exists('config.json'):
        try:
            with open('config.json', 'r') as f:
                data = json.load(f)
                
            # Map legacy 'working_hours' block if present
            working_hours = data.get('working_hours', {})
            if working_hours:
                if 'start' in working_hours: data['working_start'] = working_hours['start']
                if 'end' in working_hours: data['working_end'] = working_hours['end']
            
            return UserConfig(**data)
        except Exception as e:
            logger.error(f"Error loading config.json: {e}. Using defaults.")
            return UserConfig()
    return UserConfig()

def save_config(config: UserConfig):
    """Saves current configuration back to config.json."""
    try:
        with open('config.json', 'w') as f:
            json.dump(config.model_dump(), f, indent=4)
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")

CONFIG = load_config()
STATE = SessionState()
