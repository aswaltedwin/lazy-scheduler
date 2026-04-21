import datetime
import core
from core import StrategicPartner
from models import UserProfile, EventDetails

# Mock environment
test_profile = UserProfile()
test_profile.time_misses["afternoon"] = 5
test_profile.time_hits["afternoon"] = 0
test_profile.dominance_threshold = 5

core.PROFILE = test_profile

def test_proactive_intelligence():
    print("Testing Proactive Intelligence...")
    
    # Context: Afternoon event requested
    event = EventDetails(
        title="Risk Test",
        start="2026-04-21T14:00:00",
        end="2026-04-21T15:00:00"
    )
    
    print(f"Targeting: {event.start} (Afternoon)")
    print(f"Profile Afternoon Misses: {test_profile.time_misses['afternoon']}")

    # 1. Assess Risk
    # Mocking service=None means it will try to catch internal errors gracefully
    risk = StrategicPartner.assess_proactive_risk(None, event)
    
    print(f"Risk Level: {risk['level']}")
    for r in risk['reasons']:
        print(f"Reason: {r}")
        
    if risk['level'] != "low":
        print("SUCCESS: Proactive risk detected!")
        if risk['suggest_alt']:
            print("SUCCESS: Alternative search triggered!")
    else:
        print("FAILURE: No proactive risk detected.")

if __name__ == "__main__":
    test_proactive_intelligence()
