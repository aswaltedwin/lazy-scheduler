import datetime
from typing import List, Dict, Any, Optional
from dateutil import parser, tz
from ortools.sat.python import cp_model
from config import CONFIG, PROFILE
from models import EventDetails
from integrations.calendar_service import list_events
from services.scoring import PriorityScorer, ScoringEngine

class OptimizationEngine:
    """
    CP-SAT Optimization Engine using Google OR-Tools.
    Solves calendar scheduling conflicts by finding non-overlapping intervals
    while minimizing displacement/friction and respecting priority & momentum scores.
    """
    
    @staticmethod
    def get_magic_fix_proposals(service, new_event: EventDetails, conflicts: list, all_day_events: list, fixed_ids: list = None) -> List[dict]:
        """
        Generates genuine CP-SAT optimization proposals to place new_event and re-align
        conflicting events with minimal displacement friction and preserved momentum.
        """
        if fixed_ids is None:
            fixed_ids = []

        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        new_start_dt = parser.parse(new_event.start)
        new_end_dt = parser.parse(new_event.end)
        new_dur_mins = max(15, int((new_end_dt - new_start_dt).total_seconds() / 60))

        # Planning window: 8:00 AM to 9:00 PM on the day of the event
        day_anchor = new_start_dt.replace(hour=CONFIG.working_start, minute=0, second=0, microsecond=0)
        window_start = day_anchor
        window_end = new_start_dt.replace(hour=CONFIG.working_end, minute=0, second=0, microsecond=0)
        if window_end <= window_start:
            window_end = window_start + datetime.timedelta(hours=12)

        def dt_to_min(dt: datetime.datetime) -> int:
            return int((dt - window_start).total_seconds() / 60)

        def min_to_dt(m: int) -> datetime.datetime:
            return window_start + datetime.timedelta(minutes=m)

        max_window_mins = dt_to_min(window_end)
        if max_window_mins < 60:
            max_window_mins = 720  # 12 hours fallback

        # Collect all items to schedule
        items = []
        new_momentum = int(ScoringEngine.calculate_momentum_score(new_event, PROFILE) * 10)
        items.append({
            "id": "new_event",
            "summary": new_event.title,
            "orig_start_min": max(0, dt_to_min(new_start_dt)),
            "duration": new_dur_mins,
            "momentum": new_momentum,
            "is_new": True,
            "fixed": False
        })

        for c in conflicts:
            c_summary = c.get('summary', 'Busy')
            c_start = parser.parse(c['start'].get('dateTime', c['start'].get('date')))
            c_end = parser.parse(c['end'].get('dateTime', c['end'].get('date')))
            dur = max(15, int((c_end - c_start).total_seconds() / 60))
            
            c_details = EventDetails(title=c_summary, priority=PriorityScorer.calculate_priority(c_summary))
            c_momentum = int(ScoringEngine.calculate_momentum_score(c_details, PROFILE) * 10)
            
            items.append({
                "id": c.get('id'),
                "summary": c_summary,
                "orig_start_min": max(0, dt_to_min(c_start)),
                "duration": dur,
                "momentum": c_momentum,
                "is_new": False,
                "fixed": c.get('id') in fixed_ids
            })

        # OR-Tools CP-SAT formulation
        model = cp_model.CpModel()
        intervals = []
        displacement_vars = []

        horizon = max_window_mins + 360  # Allow slight overflow past working hours if necessary

        for item in items:
            # We add break_duration buffer to each interval size in the solver
            # so events naturally have rest / prep buffers between them
            buffer_mins = CONFIG.break_duration if CONFIG.break_duration > 0 else 0
            dur_with_buffer = item["duration"] + buffer_mins
            orig_s = item["orig_start_min"]
            
            if item["fixed"]:
                start_var = model.NewConstant(orig_s)
                end_var = model.NewConstant(orig_s + dur_with_buffer)
                interval_var = model.NewIntervalVar(start_var, dur_with_buffer, end_var, f"fixed_{item['id']}")
            else:
                start_var = model.NewIntVar(0, horizon, f"start_{item['id']}")
                end_var = model.NewIntVar(0, horizon + dur_with_buffer, f"end_{item['id']}")
                interval_var = model.NewIntervalVar(start_var, dur_with_buffer, end_var, f"interval_{item['id']}")
                
                # Model absolute displacement: |start_var - orig_s|
                disp_var = model.NewIntVar(0, horizon, f"disp_{item['id']}")
                diff_var = model.NewIntVar(-horizon, horizon, f"diff_{item['id']}")
                model.Add(diff_var == start_var - orig_s)
                model.AddAbsEquality(disp_var, diff_var)
                
                # Weight displacement inversely to momentum / priority
                # Higher momentum tasks should move less
                weight = max(1, 100 - item["momentum"])
                displacement_vars.append(disp_var * weight)
                
            intervals.append(interval_var)
            item["start_var"] = start_var
            item["end_var"] = end_var

        # Constraint: No overlapping intervals
        model.AddNoOverlap(intervals)

        # Constraint: Task Precedence / DAG Dependencies (e.g. Task A depends on Task B)
        # If new_event depends on any conflict, or any conflict depends on another
        item_by_id = {it["id"]: it for it in items}
        item_by_summary = {it["summary"].lower(): it for it in items}

        dep_name = getattr(new_event, "depends_on", None)
        if dep_name:
            dep_key = dep_name.lower()
            matched_item = None
            for s_name, it in item_by_summary.items():
                if dep_key in s_name or s_name in dep_key:
                    matched_item = it
                    break
            if matched_item:
                # new_event must start AFTER matched_item ends (+ buffer)
                model.Add(items[0]["start_var"] >= matched_item["end_var"])

        # Objective: Minimize weighted displacement (schedule friction)
        if displacement_vars:
            model.Minimize(sum(displacement_vars))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 2.0
        status = solver.Solve(model)

        targets = []
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for item in items:
                assigned_start_min = solver.Value(item["start_var"])
                assigned_start_dt = min_to_dt(assigned_start_min)
                assigned_end_dt = assigned_start_dt + datetime.timedelta(minutes=item["duration"])
                
                targets.append({
                    "id": item["id"],
                    "summary": item["summary"],
                    "new_start": assigned_start_dt,
                    "new_end": assigned_end_dt,
                    "momentum_score": item["momentum"] / 10.0,
                    "breakdown": {
                        "reason": f"Displacement Minimized by CP-SAT (Shifted {assigned_start_min - item['orig_start_min']} min)"
                    }
                })
        else:
            # Fallback heuristic if horizon is heavily congested
            curr_start = new_start_dt
            for item in items:
                targets.append({
                    "id": item["id"],
                    "summary": item["summary"],
                    "new_start": curr_start,
                    "new_end": curr_start + datetime.timedelta(minutes=item["duration"]),
                    "momentum_score": item["momentum"] / 10.0,
                    "breakdown": {"reason": "Fallback sequential alignment"}
                })
                curr_start = curr_start + datetime.timedelta(minutes=item["duration"])

        return [{
            "status": "success",
            "targets": targets,
            "strategy": "Google OR-Tools CP-SAT Optimal Realignment",
            "cost": float(solver.ObjectiveValue()) if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0.0
        }]

    @staticmethod
    def emergency_reshuffle_remaining_day(service, current_time: Optional[datetime.datetime] = None) -> dict:
        """
        1-Click Emergency Reshuffle:
        Locks all past/ongoing events up to current_time,
        and realigns all remaining tasks into non-overlapping slots with minimal displacement.
        """
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        now = current_time or datetime.datetime.now(local_tz)
        start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

        events = list_events(service, start_day.isoformat(), end_day.isoformat())
        if not events:
            return {"status": "no_events", "targets": []}

        def dt_to_min(dt: datetime.datetime) -> int:
            return int((dt - start_day).total_seconds() / 60)

        def min_to_dt(m: int) -> datetime.datetime:
            return start_day + datetime.timedelta(minutes=m)

        current_min = dt_to_min(now)
        buffer_mins = CONFIG.break_duration if CONFIG.break_duration > 0 else 10
        horizon = 1440  # 24 hours

        model = cp_model.CpModel()
        intervals = []
        displacement_vars = []
        parsed_items = []

        for e in events:
            summary = e.get('summary', 'Event')
            s_dt = parser.parse(e['start'].get('dateTime', e['start'].get('date'))).astimezone(local_tz)
            e_dt = parser.parse(e['end'].get('dateTime', e['end'].get('date'))).astimezone(local_tz)
            dur = max(15, int((e_dt - s_dt).total_seconds() / 60))
            orig_min = dt_to_min(s_dt)
            end_min = dt_to_min(e_dt)

            p = PriorityScorer.calculate_priority(summary)
            details = EventDetails(title=summary, priority=p)
            momentum = int(ScoringEngine.calculate_momentum_score(details, PROFILE) * 10)

            # Event is in past or currently happening: LOCK IT IN PLACE
            # An event is active if its end time is after now AND start is before now
            is_past_or_current = (e_dt <= now) or (s_dt <= now and e_dt > now)
            if is_past_or_current:
                start_var = model.NewConstant(orig_min)
                end_var = model.NewConstant(orig_min + dur)
                interval_var = model.NewIntervalVar(start_var, dur, end_var, f"fixed_{e['id']}")
            else:
                # Remaining future task: Can be shifted, must start after NOW + buffer
                earliest_start = max(current_min + 5, CONFIG.working_start * 60)
                dur_with_buf = dur + buffer_mins
                start_var = model.NewIntVar(earliest_start, horizon, f"start_{e['id']}")
                end_var = model.NewIntVar(earliest_start + dur_with_buf, horizon + dur_with_buf, f"end_{e['id']}")
                interval_var = model.NewIntervalVar(start_var, dur_with_buf, end_var, f"interval_{e['id']}")

                disp_var = model.NewIntVar(0, horizon, f"disp_{e['id']}")
                diff_var = model.NewIntVar(-horizon, horizon, f"diff_{e['id']}")
                model.Add(diff_var == start_var - orig_min)
                model.AddAbsEquality(disp_var, diff_var)

                # Weight displacement inversely to priority
                weight = max(1, 100 - momentum)
                displacement_vars.append(disp_var * weight)

            intervals.append(interval_var)
            parsed_items.append({
                "id": e['id'],
                "summary": summary,
                "orig_start": s_dt,
                "duration": dur,
                "start_var": start_var,
                "momentum": momentum,
                "is_locked": is_past_or_current
            })

        model.AddNoOverlap(intervals)
        if displacement_vars:
            model.Minimize(sum(displacement_vars))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 2.0
        status = solver.Solve(model)

        targets = []
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for item in parsed_items:
                assigned_min = solver.Value(item["start_var"])
                new_start = min_to_dt(assigned_min)
                new_end = new_start + datetime.timedelta(minutes=item["duration"])

                # Only include items that actually moved or needed reshuffling
                has_moved = abs((new_start - item["orig_start"]).total_seconds()) > 60
                if has_moved and not item["is_locked"]:
                    targets.append({
                        "id": item["id"],
                        "summary": item["summary"],
                        "new_start": new_start,
                        "new_end": new_end,
                        "momentum_score": item["momentum"] / 10.0,
                        "breakdown": {
                            "reason": f"Emergency Reshuffle: Shifted by {int((new_start - item['orig_start']).total_seconds()/60)} min"
                        }
                    })

        return {
            "status": "success",
            "targets": targets,
            "strategy": "CP-SAT Emergency Reshuffle"
        }

    @staticmethod
    def optimize_day_transaction(service, date_str: str) -> dict:
        """
        Re-evaluates the entire day with CP-SAT to resolve all overlaps and optimize momentum.
        """
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        target_date = parser.parse(date_str).astimezone(local_tz) if date_str else datetime.datetime.now(local_tz)
        start_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = target_date.replace(hour=23, minute=59, second=59, microsecond=0)
        
        events = list_events(service, start_day.isoformat(), end_day.isoformat())
        if not events:
            return {"original_events": [], "targets": []}
            
        model = cp_model.CpModel()
        work_start = start_day.replace(hour=CONFIG.working_start, minute=0, second=0)
        
        def dt_to_min(dt: datetime.datetime) -> int:
            return int((dt - work_start).total_seconds() / 60)
            
        intervals = []
        displacement_vars = []
        parsed_items = []
        horizon = 1440  # 24 hours in minutes
        
        for e in events:
            summary = e.get('summary', 'Event')
            s_dt = parser.parse(e['start'].get('dateTime', e['start'].get('date')))
            e_dt = parser.parse(e['end'].get('dateTime', e['end'].get('date')))
            dur = max(15, int((e_dt - s_dt).total_seconds() / 60))
            orig_min = dt_to_min(s_dt)
            
            p = PriorityScorer.calculate_priority(summary)
            details = EventDetails(title=summary, priority=p)
            momentum = int(ScoringEngine.calculate_momentum_score(details, PROFILE) * 10)
            
            start_var = model.NewIntVar(-horizon, horizon * 2, f"start_{e['id']}")
            end_var = model.NewIntVar(-horizon, horizon * 2, f"end_{e['id']}")
            interval_var = model.NewIntervalVar(start_var, dur, end_var, f"interval_{e['id']}")
            intervals.append(interval_var)
            
            disp_var = model.NewIntVar(0, horizon * 2, f"disp_{e['id']}")
            diff_var = model.NewIntVar(-horizon * 2, horizon * 2, f"diff_{e['id']}")
            model.Add(diff_var == start_var - orig_min)
            model.AddAbsEquality(disp_var, diff_var)
            
            displacement_vars.append(disp_var)
            parsed_items.append({
                "id": e['id'],
                "summary": summary,
                "orig_start": s_dt,
                "duration": dur,
                "start_var": start_var,
                "momentum": momentum
            })
            
        model.AddNoOverlap(intervals)
        model.Minimize(sum(displacement_vars))
        
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 2.0
        status = solver.Solve(model)
        
        targets = []
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for item in parsed_items:
                assigned_min = solver.Value(item["start_var"])
                new_start = work_start + datetime.timedelta(minutes=assigned_min)
                new_end = new_start + datetime.timedelta(minutes=item["duration"])
                targets.append({
                    "id": item["id"],
                    "summary": item["summary"],
                    "new_start": new_start,
                    "new_end": new_end,
                    "momentum_score": item["momentum"] / 10.0
                })
                
        return {"original_events": events, "targets": targets}
