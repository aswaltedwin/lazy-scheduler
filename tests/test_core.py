import datetime
from dateutil import tz
from parsing.rule_parser import RuleBasedParser, Validator
from services.scoring import PriorityScorer, ScoringEngine
from models import EventDetails, UserProfile
from core.optimization_engine import OptimizationEngine

def test_rule_based_parser_duration_and_title():
    text = "Gym tomorrow at 8am for 45 mins"
    parsed = RuleBasedParser.parse_input(text)
    assert parsed.title == "Gym"
    assert parsed.action == "create"
    assert Validator.validate_event(parsed) is True
    
    start_dt = datetime.datetime.fromisoformat(parsed.start)
    end_dt = datetime.datetime.fromisoformat(parsed.end)
    duration = int((end_dt - start_dt).total_seconds() / 60)
    assert duration == 45
    assert start_dt.hour == 8
    assert start_dt.minute == 0

def test_rule_based_parser_action_detection():
    delete_event = RuleBasedParser.parse_input("delete meeting with team")
    assert delete_event.action == "delete"

    find_event = RuleBasedParser.parse_input("find what is on my schedule today")
    assert find_event.action == "find"

def test_scoring_engine():
    p_urgent = PriorityScorer.calculate_priority("Urgent Client Meeting")
    assert p_urgent == 3

    p_low = PriorityScorer.calculate_priority("Chill and browse net")
    assert p_low == 1

    profile = UserProfile()
    event = EventDetails(
        title="Urgent Client Meeting",
        priority=p_urgent,
        start="2026-11-10T10:00:00+05:30",
        end="2026-11-10T11:00:00+05:30",
        energy_cost=4
    )
    score = ScoringEngine.calculate_momentum_score(event, profile)
    assert score > 30.0

def test_task_dependency_precedence():
    task = EventDetails(
        title="Review & Sign Contract",
        start="2026-11-10T10:00:00+05:30",
        end="2026-11-10T10:30:00+05:30",
        duration_mins=30,
        depends_on="Draft Contract"
    )
    conflicts = [{
        "id": "evt_draft",
        "summary": "Draft Contract",
        "start": {"dateTime": "2026-11-10T10:00:00+05:30"},
        "end": {"dateTime": "2026-11-10T11:00:00+05:30"}
    }]
    proposals = OptimizationEngine.get_magic_fix_proposals(None, task, conflicts, conflicts)
    assert len(proposals) > 0
    targets = {t["summary"]: t for t in proposals[0]["targets"]}
    assert "Draft Contract" in targets
    assert "Review & Sign Contract" in targets
    # Precedence constraint enforced by CP-SAT: Review must start after Draft ends
    assert targets["Review & Sign Contract"]["new_start"] >= targets["Draft Contract"]["new_end"]
    task = EventDetails(
        title="Urgent Sync",
        start="2026-11-10T10:00:00+05:30",
        end="2026-11-10T11:00:00+05:30",
        duration_mins=60,
        priority=3
    )
    conflicts = [{
        "id": "evt_conflicting_1",
        "summary": "Low Priority Gym",
        "start": {"dateTime": "2026-11-10T10:00:00+05:30"},
        "end": {"dateTime": "2026-11-10T10:45:00+05:30"}
    }]
    
    proposals = OptimizationEngine.get_magic_fix_proposals(None, task, conflicts, conflicts)
    assert len(proposals) > 0
    assert proposals[0]["status"] == "success"
    
    targets = proposals[0]["targets"]
    assert len(targets) == 2
    
    # Check that intervals do not overlap
    t1, t2 = targets[0], targets[1]
    assert t1["new_end"] <= t2["new_start"] or t2["new_end"] <= t1["new_start"]


def test_emergency_reshuffle_locks_past_and_shifts_future():
    now = datetime.datetime.now().astimezone()
    events = [
        {
            'id': 'evt_past',
            'summary': 'Past Meeting',
            'start': {'dateTime': (now - datetime.timedelta(hours=2)).isoformat()},
            'end': {'dateTime': (now - datetime.timedelta(hours=1)).isoformat()}
        },
        {
            'id': 'evt_active',
            'summary': 'Delayed Ongoing Meeting',
            'start': {'dateTime': (now - datetime.timedelta(minutes=20)).isoformat()},
            'end': {'dateTime': (now + datetime.timedelta(minutes=40)).isoformat()}
        },
        {
            'id': 'evt_colliding',
            'summary': 'Colliding Future Task',
            'start': {'dateTime': (now + datetime.timedelta(minutes=10)).isoformat()},
            'end': {'dateTime': (now + datetime.timedelta(minutes=50)).isoformat()}
        }
    ]
    import core.optimization_engine
    core.optimization_engine.list_events = lambda s, st, en: events
    res = OptimizationEngine.emergency_reshuffle_remaining_day(None, now)
    assert res['status'] == 'success'
    assert len(res['targets']) == 1
    target = res['targets'][0]
    assert target['summary'] == 'Colliding Future Task'
    from dateutil import parser as dparser
    active_end = dparser.parse(events[1]['end']['dateTime'])
    assert target['new_start'] >= active_end - datetime.timedelta(minutes=1)
