import datetime
import json
import time
from dateutil import tz, parser
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from config import CONFIG, STATE, PROFILE, save_state
from utils.logger import logger
from integrations.calendar_service import (
    get_calendar_service, 
    list_events, 
    create_event, 
    update_event, 
    delete_event
)
from integrations.llm_service import llm_service
from parsing.rule_parser import RuleBasedParser, Validator
from services.scoring import ScoringEngine
from core.optimization_engine import OptimizationEngine
from core.adaptive_partner import AdaptiveStrategicPartner
from core.decision_engine import DecisionEngine

console = Console()

class Orchestrator:
    """
    The lazy-scheduler Level 2 Agent.
    """
    
    @staticmethod
    def run():
        """Main interaction loop with Predictive Intelligence."""
        logger.info("lazy-scheduler Level 2 agent started.")
        
        try:
            service = Orchestrator.get_service_safe()
        except Exception as e:
            console.print(f"[bold red]Error connecting to Google:[/bold red] {e}")
            return

        # --- LEVEL 2: PROACTIVE FAILURE PREDICTION ---
        Orchestrator.check_day_health(service)

        console.print(Panel(
            "[bold cyan]lazy-scheduler[/bold cyan]\n"
            "[dim]Task Scoring | Conflict Resolution | Calendar Sync[/dim]",
            border_style="blue",
            expand=False,
            padding=(1, 2)
        ))
        
        while True:
            try:
                user_input = console.input("\n[bold blue]>[/bold blue] ").strip()
                if not user_input: continue
                
                low_input = user_input.lower()
                if any(w in low_input for w in ['exit', 'quit', 'bye', 'goodbye']):
                    console.print("[cyan]Goodbye! Have a productive day.[/cyan]")
                    break

                # 1. Understand Intent (with Energy & Decomposition)
                intelligence = llm_service.understand_intent(user_input, f"Last question: {STATE.last_question}")
                
                if not intelligence:
                    task = RuleBasedParser.parse_input(user_input)
                else:
                    task = RuleBasedParser.parse_input(f"{intelligence.get('title')} at {intelligence.get('time_str')}")
                    task.action = intelligence.get('action')
                    task.search_query = intelligence.get('search_query') or task.title
                    task.energy_cost = intelligence.get('energy_cost', 3)
                    task.intelligence = intelligence
                    
                    duration = intelligence.get('duration_mins', 60)
                    start_dt = parser.parse(task.start)
                    
                    # Past-time check
                    local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
                    now = datetime.datetime.now(local_tz)
                    if start_dt < now and task.action == "create":
                        start_dt = start_dt + datetime.timedelta(days=1)
                        task.start = start_dt.isoformat()
                        console.print(f"[dim]Note: Moved to tomorrow as time has passed.[/dim]")

                    task.end = (start_dt + datetime.timedelta(minutes=duration)).isoformat()
                    
                    until = intelligence.get('repeat_until')
                    if until: task.recurrence = [f"RRULE:FREQ=DAILY;UNTIL={until}T235959Z"]

                # 2. Level 2: Task Decomposition
                if intelligence and intelligence.get('needs_decomposition'):
                    console.print(f"[bold yellow]Strategic Suggestion:[/bold yellow] '{task.title}' is a large goal. Should I break it down into these smaller steps?")
                    for sub in intelligence['sub_tasks']:
                        console.print(f" [dim]• {sub}[/dim]")
                    if console.input("[bold cyan]Split this task? (y/n): [/bold cyan]").lower() == 'y':
                        # In a full implementation, this would create multiple smaller tasks.
                        # For now, we update the title to show it's focused.
                        task.title = f"{task.title} (Part 1: {intelligence['sub_tasks'][0]})"
                        task.is_decomposed = True

                # 3. Actions
                if task.action in ["greet", "chat"]:
                    if intelligence and intelligence.get('reply'):
                        console.print(f"[cyan]{intelligence['reply']}[/cyan]")
                    else:
                        console.print("[cyan]How can I help?[/cyan]")
                    continue

                if task.action == "find":
                    Orchestrator.handle_search(service, task)
                    continue

                if task.action == "delete":
                    Orchestrator.handle_deletion(service, task)
                    continue

                if not Validator.validate_event(task):
                    console.print("[yellow]Could you be more specific? (e.g. 'Gym at 5pm')[/yellow]")
                    continue

                # 4. Energy-Aware Conflict Handling
                task.momentum_score = ScoringEngine.calculate_momentum_score(task, PROFILE)
                conflicts = list_events(service, task.start, task.end)

                if not conflicts:
                    event_body = {
                        'summary': task.title,
                        'start': {'dateTime': task.start, 'timeZone': CONFIG.timezone},
                        'end': {'dateTime': task.end, 'timeZone': CONFIG.timezone},
                    }
                    if task.recurrence: event_body['recurrence'] = task.recurrence
                    create_event(service, event_body)
                    Orchestrator.update_memory(task)
                    console.print(f"[green]Added {task.title} to your calendar.[/green]")
                    continue
                
                # --- NEGOTIATION ---
                console.print(f"[cyan]I found a conflict. Energy Fit Score: {round(task.momentum_score, 1)}[/cyan]")
                proposal_data = OptimizationEngine.get_magic_fix_proposals(service, task, conflicts, [])
                if not proposal_data: continue
                
                current_proposal = proposal_data[0]
                conflicting_names = ", ".join([c['summary'] for c in conflicts])
                
                while True:
                    Orchestrator.show_results(task, conflicts, [current_proposal])
                    user_choice = console.input(f"[bold cyan]Apply this fix, or suggest a time for {conflicting_names}? (y/n/your time): [/bold cyan]").strip()
                    
                    if user_choice.lower() == 'y':
                        for target in current_proposal['targets']:
                            body = {'summary': target['summary'], 'start': {'dateTime': target['new_start'].isoformat(), 'timeZone': CONFIG.timezone}, 'end': {'dateTime': target['new_end'].isoformat(), 'timeZone': CONFIG.timezone}}
                            if target['id'] == 'new_event':
                                if task.recurrence: body['recurrence'] = task.recurrence
                                create_event(service, body)
                            else:
                                update_event(service, target['id'], body)
                        console.print("[green]Your schedule has been updated.[/green]")
                        break
                    elif user_choice.lower() == 'n':
                        console.print("[yellow]Cancelled.[/yellow]")
                        break
                    else:
                        counter = llm_service.understand_intent(user_choice)
                        if counter and counter.get('time_str'):
                            new_time_str = counter.get('time_str')
                            for target in current_proposal['targets']:
                                if target['id'] != 'new_event':
                                    try:
                                        target['new_start'] = parser.parse(new_time_str, default=target['new_start'])
                                        target['new_end'] = target['new_start'] + datetime.timedelta(minutes=60)
                                    except Exception: pass
                        else:
                            console.print("[yellow]I didn't quite catch that.[/yellow]")

            except KeyboardInterrupt: break
            except Exception as e:
                logger.error(f"Error: {e}")
                if "SSL" in str(e) or "EOF" in str(e): service = Orchestrator.get_service_safe()
                console.print(f"[red]Sorry, I ran into an issue: {e}[/red]")

    @staticmethod
    def check_day_health(service):
        """Level 2: Check for burnout risk upon startup."""
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        now = datetime.datetime.now(local_tz)
        start = now.replace(hour=0, minute=0, second=0).isoformat()
        end = now.replace(hour=23, minute=59, second=59).isoformat()
        events = list_events(service, start, end)
        
        health = ScoringEngine.assess_workload_health(events, PROFILE)
        if health['status'] != 'healthy':
            panel = Panel(
                f"[bold red]Burnout Alert![/bold red]\n"
                f"Workload Probability: {int(health['failure_probability']*100)}%\n"
                f"Status: {health['status'].upper()}\n"
                f"You have a heavy load today. Should I help you prioritize?",
                border_style="red"
            )
            console.print(panel)

    @staticmethod
    def get_service_safe():
        for _ in range(3):
            try: return get_calendar_service()
            except Exception: time.sleep(1)
        return get_calendar_service()

    @staticmethod
    def handle_search(service, task):
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        now = datetime.datetime.now(local_tz)
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if hasattr(task, 'intelligence') and task.intelligence.get('search_range'):
            r = task.intelligence['search_range']
            try:
                if r.get('start'): start_dt = parser.parse(r['start'], default=start_dt).replace(tzinfo=local_tz)
                if r.get('end'): end_dt = parser.parse(r['end'], default=end_dt).replace(tzinfo=local_tz)
            except Exception: pass
        events = list_events(service, start_dt.isoformat(), end_dt.isoformat())
        query = (task.search_query or "").lower()
        is_all = query == "all" or any(w in query for w in ["today", "schedules", "agenda", "week", "everything"])
        matches = events if is_all else [e for e in events if query in e.get('summary', '').lower() or set(query.split()).intersection(set(e.get('summary', '').lower().split()))]
        if matches:
            console.print(f"[cyan]I found {len(matches)} events:[/cyan]")
            for m in matches:
                start = m['start'].get('dateTime', m['start'].get('date'))
                dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00')).astimezone(local_tz)
                time_str = dt.strftime("%I:%M %p") if dt.date() == now.date() else dt.strftime("%a, %b %d at %I:%M %p")
                console.print(f" [dim]•[/dim] [white]{m['summary']}[/white] ({time_str})")
        else:
            console.print(f"[yellow]I couldn't find any events for that range.[/yellow]")

    @staticmethod
    def handle_deletion(service, task):
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        now = datetime.datetime.now(local_tz)
        start_search = (now - datetime.timedelta(days=7)).isoformat()
        end_search = (now + datetime.timedelta(days=30)).isoformat()
        events = list_events(service, start_search, end_search)
        query = task.search_query.lower()
        target_event = None
        for e in events:
            if query in e.get('summary', '').lower():
                target_event = e
                break
        if target_event:
            if delete_event(service, target_event['id']):
                console.print(f"[green]Removed '{target_event['summary']}' from your calendar.[/green]")
        else:
            console.print(f"[yellow]Couldn't find any events matching '{task.title}'.[/yellow]")

    @staticmethod
    def update_memory(task):
        title = task.title.lower()
        if title not in PROFILE.task_history:
            PROFILE.task_history[title] = {"completed": 0, "missed": 0, "last_scheduled": None}
        PROFILE.task_history[title]["completed"] += 1
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        PROFILE.task_history[title]["last_scheduled"] = datetime.datetime.now(local_tz).isoformat()
        save_state(CONFIG, PROFILE)

    @staticmethod
    def show_results(task, conflicts, proposal):
        if proposal:
            table = Table(title="Proposed Realignment", box=None)
            table.add_column("Item", style="white")
            table.add_column("Action", style="cyan")
            table.add_column("New Time", style="green")
            for t in proposal[0]['targets']:
                act = "Add" if t['id'] == 'new_event' else "Move"
                time_str = t['new_start'].strftime("%I:%M %p")
                table.add_row(t['summary'], act, time_str)
            console.print(Panel.fit(table, border_style="yellow"))
