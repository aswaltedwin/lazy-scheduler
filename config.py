import os
import json
import logging
from dotenv import load_dotenv
from models import UserConfig, SessionState, UserProfile

# Load environment variables
load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", "lazy-scheduler-493410")

# Environment Validation (Skip if testing)
if not os.getenv("LAZY_TESTING"):
    if not GOOGLE_CLIENT_ID:
        raise Exception("Missing GOOGLE_CLIENT_ID in environment/.env")
    if not GOOGLE_CLIENT_SECRET:
        raise Exception("Missing GOOGLE_CLIENT_SECRET in environment/.env")

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

def load_profile():
    """Loads behavior profile from user_profile.json or returns defaults."""
    if os.path.exists('user_profile.json'):
        try:
            with open('user_profile.json', 'r') as f:
                data = json.load(f)
            return UserProfile(**data)
        except Exception as e:
            logger.error(f"Error loading user_profile.json: {e}. Using defaults.")
            return UserProfile()
    return UserProfile()

def save_profile(profile: UserProfile):
    """Saves current profile behavior back to user_profile.json."""
    try:
        with open('user_profile.json', 'w') as f:
            json.dump(profile.model_dump(), f, indent=4)
    except Exception as e:
        logger.error(f"Error saving user_profile.json: {e}")

CONFIG = load_config()
PROFILE = load_profile()
STATE = SessionState()
