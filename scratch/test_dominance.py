import datetime
from dateutil import parser
import core
from models import EventDetails

def test_dominance_logic():
    print("\n--- Feature: Dominant Adaptive Intelligence Verification ---")
    
    # 1. Setup a "Stubborn" Profile (Banned Morning)
    core.PROFILE.time_misses['morning'] = 10
    core.PROFILE.time_hits['morning'] = 0
    core.PROFILE.time_preferences['morning'] = -20
    core.PROFILE.dominance_threshold = 5
    
    # Target time: 10:00 AM (Morning)
    now = datetime.datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    conflict_event = {
        'id': 'dummy_conflict',
        'summary': 'Conflicting Meeting',
        'start': {'dateTime': now.replace(hour=10, minute=0).isoformat() + "Z"},
        'end': {'dateTime': now.replace(hour=11, minute=0).isoformat() + "Z"}
    }
    
    # CASE A: Low Priority (P1) - Should be HARD BANNED from morning
    print("\n[Case A] Scheduling P1 'Low Task' vs 'Conflict' at 10:00 AM (Morning Forbidden Zone)...")
    p1_request = EventDetails(
        title="Low Task",
        start=now.replace(hour=10, minute=0).isoformat(),
        end=now.replace(hour=11, minute=0).isoformat(),
        priority=1
    )
    
    # Mock return
    core.list_upcoming_events = lambda service, start, end: [conflict_event]
    
    proposals_p1 = core.get_magic_fix_proposals(None, p1_request, [conflict_event])
    
    if proposals_p1:
        # Check if any proposal landed in the morning (Hard ban check)
        for idx, prop in enumerate(proposals_p1):
            t = prop['targets'][0]
            new_hour = t['new_start'].hour
            is_morning = 9 <= new_hour < 12
            print(f" Proposal {idx+1}: Scheduled at {new_hour}:00. Morning? {is_morning}")
            
            # P1 should have been pushed out of 9-12 range
            if is_morning:
                print("FAIL: P1 event scheduled in a forbidden morning slot!")
            else:
                print("SUCCESS: P1 event was pushed out of forbidden zone.")
    else:
        print("FAIL: No proposals generated for P1 task.")

    # CASE B: High Priority (P3) - Should be allowed but with EXTREME PENALTY advice
    print("\n[Case B] Scheduling P3 'Urgent Meeting' at 10:00 AM...")
    p3_request = EventDetails(
        title="Urgent Meeting",
        start=now.replace(hour=10, minute=0).isoformat(),
        end=now.replace(hour=11, minute=0).isoformat(),
        priority=3
    )
    
    proposals_p3 = core.get_magic_fix_proposals(None, p3_request, [conflict_event])
    
    if proposals_p3:
        prop = proposals_p3[0]
        t = prop['targets'][0]
        print(f" SUCCESS: P3 allowed at {t['new_start'].hour}:00")
        print(f" Advice: {prop.get('tactical_advice')}")
        
        # Check for dominance briefing in advice
        if "Deep Adaptation" in prop.get('tactical_advice', ''):
            print("SUCCESS: Intelligence briefing detected.")
        else:
            print("FAIL: Expected intelligence briefing in advice.")
    else:
        print("FAIL: No proposals generated for P3 task.")

    print("\n--- Verification Complete ---")

if __name__ == "__main__":
    test_dominance_logic()
