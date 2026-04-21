import datetime
import core
from core import StrategicPartner, DecisionEngine
from models import UserProfile, EventDetails

# Mock environment
test_profile = UserProfile()
core.PROFILE = test_profile

def test_assistant_persona():
    print("Testing Assistant Persona (Keyword Awareness)...")
    
    # 1. Mock a "Gym" conflict
    # Creating a dummy conflict list
    conflicts = [
        {"id": "c1", "summary": "Morning Gym Session", "start": {"dateTime": "2026-04-21T08:00:00"}, "end": {"dateTime": "2026-04-21T09:00:00"}}
    ]
    
    # 2. Request something in the same slot
    event = EventDetails(
        title="Quick Sync",
        start="2026-04-21T08:00:00",
        end="2026-04-21T08:30:00"
    )

    print(f"Requested: {event.title} at {event.start}")
    print(f"Existing Blocker: {conflicts[0]['summary']}")

    # 3. Assess Proactive Risk (should mention gym)
    # Mocking service=None
    risk = StrategicPartner.assess_proactive_risk(None, event)
    
    print("\nProactive Briefing Reasons:")
    for r in risk['reasons']:
        print(f" • {r}")
        if "gym time" in r:
            print("🌟 SUCCESS: Keyword 'gym time' detected in risk assessment!")

    # 4. Check Tactical Advice
    # Mocking a proposal
    dummy_proposal = {
        "strategy": "guardian",
        "targets": [{"id": "new_event", "summary": "Quick Sync", "new_start": datetime.datetime(2026, 4, 21, 10, 0)}]
    }
    advice = StrategicPartner.generate_advice(dummy_proposal, [], conflicts)
    print(f"\nTactical Advice: {advice}")
    if "Quick Sync" in advice:
        print("🌟 SUCCESS: Advice is context-aware!")

if __name__ == "__main__":
    test_assistant_persona()
