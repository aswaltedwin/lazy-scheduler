import os
import json
import logging
from models import UserConfig, SessionState

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
            print(f"Error loading config.json: {e}. Using defaults.")
            return UserConfig()
    return UserConfig()

CONFIG = load_config()
STATE = SessionState()
