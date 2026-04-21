import datetime
from core import OptimizationEngine, PriorityScorer
from models import UserProfile, EventDetails
import core

# Mock PROFILE for testing
test_profile = UserProfile()
test_profile.time_misses["morning"] = 12  # Hard dominance trigger (>10)
test_profile.time_hits["morning"] = 0
test_profile.hard_dominance_threshold = 10

# Swap the global PROFILE in core with our test one
original_profile = core.PROFILE
core.PROFILE = test_profile

def test_instinct_avoidance():
    print("Testing Instinct-Level Avoidance...")
    
    # 1. Create a "Morning" request
    # Requested: 08:00 AM (Hour 8 - Morning)
    new_event = EventDetails(
        title="Instinct Test Event",
        start="2026-04-21T08:00:00",
        end="2026-04-21T09:00:00",
        priority=3 # High Priority - should still be blocked!
    )
    
    # Context: No conflicts, wide open day
    all_day_events = []
    
    print(f"Targeting: {new_event.start} (Priority: {new_event.priority})")
    print(f"Profile Morning Misses: {test_profile.time_misses['morning']}")
    
    # 2. Solve
    result = OptimizationEngine.solve_scheduling_problem(
        service=None, # Not needed for pure math solving
        new_event=new_event,
        conflicts=[],
        all_day_events=all_day_events,
        strategy="balanced"
    )
    
    if result and result["status"] == "success":
        for t in result["targets"]:
            if t["id"] == "new_event":
                start_hour = t["new_start"].hour
                print(f"Solver Result: Moved to Hour {start_hour}")
                if start_hour >= 12:
                    print("SUCCESS: Morning was completely eliminated from consideration!")
                else:
                    print("FAILURE: Event was still placed in the morning.")
    else:
        print("Solver failed to find a solution.")

if __name__ == "__main__":
    try:
        test_instinct_avoidance()
    finally:
        # Restore original profile
        core.PROFILE = original_profile
