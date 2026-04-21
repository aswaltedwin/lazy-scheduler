import datetime
from core import OptimizationEngine, DecisionEngine, EventDetails
import core
from dateutil import tz, parser
from config import CONFIG
import logging

def test_bulletproof_gridlock():
    """
    Verify that the system returns a Rescue Proposal when the day is 100% full.
    """
    local_tz = tz.gettz(CONFIG.timezone)
    now = datetime.datetime.now(local_tz).replace(hour=8, minute=0, second=0, microsecond=0)
    
    # 1. Setup a Gridlocked Calendar (9-5 back-to-back P3 Anchors)
    gridlock_events = []
    for h in range(9, 17):
        gridlock_events.append({
            "id": f"p3_{h}", "summary": f"Anchor P3 {h}:00",
            "start": {"dateTime": now.replace(hour=h).isoformat()},
            "end":   {"dateTime": now.replace(hour=h+1).isoformat()},
            "description": "FORCE_PIN" # The solver should treat as immovable
        })
        
    # 2. Mocking
    # Set priority to 3 for all existing events to ensure no displacement
    old_scorer = core.PriorityScorer.calculate_priority
    core.PriorityScorer.calculate_priority = lambda e: 3
    core.list_upcoming_events = lambda service, start, end: gridlock_events
    core.find_free_slots = lambda service, start, min_duration_mins=30: []
    
    # 3. Request a physically IMPOSSIBLE 24-hour meeting
    impossible_request = EventDetails(
        action="create", title="Impossible 24h Sync",
        start=now.replace(hour=0).isoformat(),
        end=(now + datetime.timedelta(days=1)).replace(hour=0).isoformat(),
        priority=1
    )
    
    print("\n--- Bulletproof Gridlock Test ---")
    print(f"Request: {impossible_request.title} (2h) in a 100% full P3 day.")
    
    # Run Pass
    options = DecisionEngine.generate_options(None, impossible_request, gridlock_events)
    
    # Validation
    if options and len(options) == 1:
        res = options[0]
        print(f"Result Status: {res.get('status')}")
        print(f"Result Reason: {res.get('reason')}")
        
        if res.get('status') == "failure":
            print("SUCCESS: System successfully pivoted to Rescue Logic.")
            print("AI Suggestions:")
            for s in res.get('suggestions', []):
                print(f"  - {s}")
        else:
            print(f"FAIL: Expected status 'failure', got '{res.get('status')}'")
    else:
        print(f"FAIL: Expected one rescue proposal, got {len(options) if options else 0}")

    # Log Check
    import os
    if os.path.exists("scheduler_failures.log"):
        print("SUCCESS: Persistent failure log created.")
    else:
        print("FAIL: No failure log found.")

if __name__ == "__main__":
    test_bulletproof_gridlock()
