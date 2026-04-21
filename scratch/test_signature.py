import datetime
from dateutil import parser
import core
from models import EventDetails

def test_negotiation_partner_logic():
    print("\n--- Signature Feature: Strategic Negotiation Verification ---")
    
    # 1. Setup a conflict
    now = datetime.datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    conflict_event = {
        'id': 'lunch_123',
        'summary': 'Team Lunch',
        'start': {'dateTime': now.replace(hour=12, minute=0).isoformat() + "Z"},
        'end': {'dateTime': now.replace(hour=13, minute=0).isoformat() + "Z"}
    }
    
    new_request = EventDetails(
        title="Emergency Sync",
        start=now.replace(hour=12, minute=0).isoformat(),
        end=now.replace(hour=13, minute=0).isoformat()
    )
    
    # 2. Mocking
    core.list_upcoming_events = lambda service, start, end: [conflict_event]
    core.find_free_slots = lambda service, start, min_duration_mins=30: []
    
    # 3. First Pass: General Magic Fix
    print("\n[Pass 1] Requesting fix for 'Emergency Sync' vs 'Team Lunch'...")
    proposals = core.get_magic_fix_proposals(None, new_request, [conflict_event])
    
    if proposals:
        prop = proposals[0]
        print(f"SUCCESS: Proposals generated.")
        print(f"Health: {prop.get('health', {}).get('status')} ({prop.get('health', {}).get('score')}%)")
        print(f"Tactical Advice: {prop.get('tactical_advice')}")
        
        # Check for expected fields
        assert "health" in prop
        assert "tactical_advice" in prop
    else:
        print("FAIL: No proposals generated.")
        return

    # 4. Negotiation Pass: Lock 'Team Lunch'
    print("\n[Pass 2] Negotiating: 'I can't move my Lunch'...")
    intent = core.StrategicPartner.extract_intent("I can't move my Lunch", [conflict_event])
    print(f"Extracted Intent: {intent}")
    
    assert intent['type'] == "lock_event"
    assert intent['id'] == "lunch_123"
    
    fixed_ids = [intent['id']]
    proposals_locked = core.get_magic_fix_proposals(None, new_request, [conflict_event], fixed_ids=fixed_ids)
    
    if proposals_locked:
        prop_locked = proposals_locked[0]
        # In a lock scenario, the AI should try to move the NEW event or split it (since the old one is fixed)
        print(f"SUCCESS: Re-solved with Lock.")
        print(f"Strategy: {prop_locked.get('reason')}")
        print(f"Advice: {prop_locked.get('tactical_advice')}")
        
        # Verify lock is respected (Lunch should not be in targets)
        is_lunch_moved = any(t['id'] == 'lunch_123' for t in prop_locked['targets'])
        print(f"Was Lunch moved? {'Yes (FAIL)' if is_lunch_moved else 'No (SUCCESS)'}")
        assert not is_lunch_moved
    else:
        print("FAIL: Solver failed to find alternative after lock.")

    print("\n--- Verification Complete ---")

if __name__ == "__main__":
    test_negotiation_partner_logic()
