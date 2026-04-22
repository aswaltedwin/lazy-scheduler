import os
import json
import logging
from models import UserConfig, SessionState, UserProfile

logger = logging.getLogger("lazy-scheduler")

# Path to the consolidated state file
STATE_PATH = os.path.join('data', 'state.json')

def load_state():
    """Loads the entire system state from data/state.json."""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, 'r') as f:
                data = json.load(f)
            
            config = UserConfig(**data.get('config', {}))
            profile = UserProfile(**data.get('profile', {}))
            return config, profile
        except Exception as e:
            logger.error(f"Error loading state: {e}. Using defaults.")
            return UserConfig(), UserProfile()
    
    # Ensure data directory exists
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    return UserConfig(), UserProfile()

def save_state(config: UserConfig, profile: UserProfile):
    """Saves the entire system state to data/state.json."""
    try:
        data = {
            "config": config.model_dump(),
            "profile": profile.model_dump()
        }
        with open(STATE_PATH, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving state: {e}")

# Global Instances
CONFIG, PROFILE = load_state()
STATE = SessionState()
