import os
import datetime
import logging
import time
from dateutil import parser
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table

# Import local modules
from config import CONFIG, STATE
from models import EventDetails
from core import (
    get_calendar_service, 
    parse_natural_language,
    ParsingError, 
    check_conflicts, 
    find_free_slots, 
    create_event,
    list_upcoming_events,
    find_event,
    delete_event,
    update_event,
    get_magic_fix_proposals,
    logger,
    _calculate_move_cost,
    LearningEngine,
    OptimizationEngine,
    PriorityScorer,
    StrategicPartner
)

console = Console()

def show_event_panel(event: EventDetails, title="Proposed Event"):
    """Displays a formatted panel of the proposed event details."""
    content = f"[bold white]📅 Title    :[/bold white] {event.title}\n"
    content += f"[bold white]🕒 Time     :[/bold white] {event.start[:16].replace('T',' ')} [dim]→[/dim] {event.end[:16].replace('T',' ')}\n"
    if event.description: content += f"[bold white]📝 Notes    :[/bold white] {event.description}\n"
    
    loc = event.location.strip()
    if loc and loc.lower() not in ["online", "google meet", "virtual"]:
        content += f"[bold white]📍 Location :[/bold white] {loc}\n"
        
    if event.attendees:   content += f"[bold white]👥 Invite   :[/bold white] {', '.join(event.attendees)}\n"
    if event.reminders_minutes: 
        rem_str = ", ".join([f"{m}m" for m in event.reminders_minutes])
        content += f"[bold white]🔔 Alarms   :[/bold white] {rem_str} before\n"
    if event.recurrence:  content += f"[bold white]🔄 Repeat   :[/bold white] {event.recurrence[0]}\n"
    if event.add_meeting: 
        content += f"[bold white]📹 Video    :[/bold white] Google Meet Link will be generated\n"
    
    console.print(Panel(content, title=f"[bold cyan]{title}[/bold cyan]", border_style="cyan", expand=False))

def show_schedule_table(events):
    """Displays user events in a clean panel-wrapped table."""
    if not events:
        console.print("[yellow]No upcoming events found.[/yellow]")
        return
    table = Table(show_header=True, header_style="bold magenta", box=None, expand=True)
    table.add_column("Date/Time", style="dim", width=22)
    table.add_column("Event", style="bold white")
    
    for event in events:
        start_raw = event['start'].get('dateTime', event['start'].get('date'))
        end_raw = event['end'].get('dateTime', event['end'].get('date'))
        s_dt = parser.parse(start_raw)
        e_dt = parser.parse(end_raw)
        
        date_part = s_dt.strftime('%a, %b %d')
        if 'T' in start_raw:
            time_part = f"{s_dt.strftime('%H:%M')} [dim]→[/dim] {e_dt.strftime('%H:%M')}"
        else:
            time_part = "All Day"
            
        time_str = f"{date_part} [bold]|[/bold] {time_part}"
        table.add_row(time_str, event['summary'])
    
    console.print(Panel(table, title="📅 [bold cyan]Your Schedule[/bold cyan]", border_style="cyan", padding=(1, 2)))

def show_optimization_comparison(original, targets):
    """Displays a side-by-side comparison of the optimization moves."""
    table = Table(title="🚀 Optimization Impact Assessment", title_style="bold magenta", show_header=True, header_style="bold cyan", box=None)
    table.add_column("Event", style="bold white")
    table.add_column("Original Time", style="dim")
    table.add_column("Optimization Move", style="bold green")
    table.add_column("Shift", justify="right")
    
    # Create a lookup for original times
    orig_map = {e['id']: e for e in original}
    
    for t in targets:
        orig = orig_map.get(t['id'], {})
        o_start_raw = orig.get('start', {}).get('dateTime', orig.get('start', {}).get('date', ''))
        o_start = parser.parse(o_start_raw).strftime('%H:%M') if o_start_raw else "N/A"
        
        n_start = t['new_start'].strftime('%H:%M')
        shift_mins = int((t['new_start'] - parser.parse(o_start_raw)).total_seconds() / 60) if o_start_raw else 0
        
        table.add_row(
            t['summary'],
            o_start,
            f"{o_start} [dim]→[/dim] [bold green]{n_start}[/bold green]",
            f"{shift_mins:+d}m"
        )
    
    console.print(Panel(table, border_style="magenta", padding=(1, 2)))

def main():
    """Main CLI interaction loop with basic session hygiene."""
    console.print(Panel(f"Model: [bold cyan]{CONFIG.model}[/bold cyan] | Timezone: [bold magenta]{CONFIG.timezone}[/bold magenta]", title="🐢 [bold]LazyScheduler V1.0[/bold]", border_style="green"))
    
    service = None
    try:
        try:
            service = get_calendar_service()
        except Exception as e:
            logger.error(f"Initialization Failed: {e}")
            console.print(f"[bold red]Critical Error:[/bold red] Could not connect to Google Calendar. See logs.")
            return

        while True:
            try:
                prefix = f"[bold yellow]🔨 ({STATE.last_event.title})[/bold yellow] " if STATE.last_event else ""
                user_input = console.input(f"\n{prefix}[bold green]You:[/bold green] ").strip()
                
                if user_input.lower() in ['quit', 'exit', 'q']:
                    console.print("[yellow]👋 Stopped.[/yellow]"); break
                if not user_input: continue

                try:
                    event = parse_natural_language(user_input, context=STATE.last_event)
                except ParsingError as pe:
                    console.print(f"\n[bold red]Parsing Error:[/bold red] {pe}")
                    if pe.suggestion:
                        console.print(Panel(f"I'm not sure I got that. Did you mean:\n[bold cyan]'{pe.suggestion}'[/bold cyan]", title="💡 Suggestion", border_style="cyan", expand=False))
                        followup = console.input("[bold yellow]Correction > [/bold yellow]").strip()
                        if followup == "": event = parse_natural_language(pe.suggestion, context=STATE.last_event)
                        else: event = parse_natural_language(followup, context=STATE.last_event)
                    else: continue
                except ValueError as ve:
                    console.print(f"[bold red]Parsing Error:[/bold red] {ve}"); continue
                
                if event.action == "optimize_day":
                    with console.status("[bold magenta]Re-evaluating entire day...", spinner="bouncingBar"):
                         result = OptimizationEngine.optimize_day_transaction(service, "")
                    if "error" in result: console.print(f"[bold yellow]Optimization Engine:[/bold yellow] {result['error']}"); continue
                    
                    show_optimization_comparison(result['original_events'], result['targets'])
                    
                    if console.input(f"\n[bold green]Apply all {len(result['targets'])} changes?[/bold green] (y/n): ").lower() in ['y', 'yes']:
                        for t in result['targets']:
                            update_event(service, t['id'], EventDetails(action="update", start=t['new_start'].isoformat(), end=t['new_end'].isoformat()))
                        console.print(f"[bold green]✨ Day Optimized.[/bold green]")
                    continue

                elif event.action == "list":
                    items = list_upcoming_events(service, event.start, event.end)
                    if event.search_query: items = [it for it in items if event.search_query.lower() in it.get('summary', '').lower()]
                    show_schedule_table(items)
                    
                    # Schedule Health Score
                    if items:
                        count = len(items)
                        p3_count = len([it for it in items if PriorityScorer.calculate_priority(it.get('summary','')) >= 3])
                        score = max(0, 100 - (count * 5) - (p3_count * 10))
                        color = "green" if score > 70 else "yellow" if score > 40 else "red"
                        console.print(f"[dim]Schedule Density Score:[/dim] [{color}]{score}%[/{color}] [dim]({count} events, {p3_count} anchors)[/dim]")
                    
                    STATE.last_event = None
                    
                elif event.action == "find_slot":
                    blocks = find_free_slots(service, event.start, min_duration_mins=event.duration_mins)
                    if blocks:
                        for i, (s, e) in enumerate(blocks):
                            console.print(f"{i+1}. {s.strftime('%H:%M')} [dim]→[/dim] {e.strftime('%H:%M')}")
                    else: console.print("[yellow]No free blocks found.[/yellow]")
                    STATE.last_event = None
                    
                elif event.action in ["delete", "update"]:
                    matches = find_event(service, event.search_query)
                    if not matches: console.print(f"[yellow]No match found for '{event.search_query}'[/yellow]")
                    else:
                        target = matches[0]
                        prompt = "Update this event? (y/n): " if event.action == "update" else "Delete this event? (y/n): "
                        if console.input(f"🎯 Found: [bold]{target['summary']}[/bold]\n{prompt}").lower() in ['y', 'yes', '']:
                            if event.action == "delete": delete_event(service, target['id'])
                            else: update_event(service, target['id'], event)
                            console.print("[green]✅ Done.[/green]")
                    STATE.last_event = None
                
                else: # create action
                    # 🔍 PROACTIVE RISK ASSESSMENT
                    with console.status("[bold blue]Assessing proactive risks...", spinner="point"):
                        risk = StrategicPartner.assess_proactive_risk(service, event)
                    
                    if risk['level'] != "low":
                        color = "red" if risk['level'] == "high" else "yellow"
                        status_msg = "🚨 [bold red]High-Risk Detection[/bold red]" if risk['level'] == "high" else "🧠 [bold yellow]Proactive Insight[/bold yellow]"
                        reasons_str = "\n".join([f" [bold {color}]•[/bold {color}] {r}" for r in risk['reasons']])
                        console.print(Panel(Group(f"{status_msg}", "", reasons_str), title="✨ [bold]Assistant Briefing[/bold]", border_style=color))
                        
                        if risk['alternative']:
                            s_start, s_end = risk['alternative']
                            console.print(f"\n[bold cyan]💡 PREVENTATIVE SUGGESTION:[/bold cyan] Switch to [bold green]{s_start.strftime('%H:%M')}[/bold green] to avoid friction?")
                            if console.input("   [bold yellow]Accept Recommendation? (y/n) > [/bold yellow]").strip().lower() in ['y', 'yes']:
                                dur = parser.parse(event.end) - parser.parse(event.start)
                                event.start, event.end = s_start.isoformat(), (s_start + dur).isoformat()
                                console.print("[green]✨ Proactive Pivot Applied.[/green]")

                    pending_updates = []; applied_fix = False; event_parts = []
                    
                    with console.status("[bold cyan]Checking conflicts...", spinner="simpleDots"):
                        busy = check_conflicts(service, event.start, event.end)
                    
                    if busy:
                        console.print(f"\n[bold red]⚠️  CONFLICT:[/bold red] You have {len(busy)} event(s) during this time.")
                        fixed_ids = []
                        while True:
                            proposals = get_magic_fix_proposals(service, event, busy, fixed_ids=fixed_ids)
                            if not proposals: break

                            # 🚨 STRATEGIC PARTNER BRIEFING
                            with console.status("[bold cyan]Analyzing tactical options...", spinner="dots"): 
                                time.sleep(0.5)

                            # 1. HEADER: Day Health & Autopsy (if failure)
                            if proposals[0].get('status') == "failure":
                                diag = proposals[0]
                                console.print(Panel(Group(f"[bold red]AUTOPSY:[/bold red] {diag['reason']}", "", "[bold cyan]RESCUE STEPS:[/bold cyan]", *[f" {idx+1}. {s}" for idx, s in enumerate(diag['suggestions'])]), title="🚨 Schedule Gridlock Detection", border_style="red"))
                                break
                            
                            health = proposals[0].get('health', {})
                            health_color = "green" if health['status'] == "Stable" else "yellow" if health['status'] == "Fragile" else "red"
                            console.print(Panel(f"Status: [{health_color}]{health['status']}[/{health_color}] | Health Score: [{health_color}]{health['score']}%[/{health_color}] | Anchors: {health['anchors']}", title="📊 [bold]Daily Vitality Monitor[/bold]", border_style="slate_blue1"))

                            # 2. PROPOSALS: War Room Dashboard
                            console.print(f"\n[bold cyan]🧙 Strategic Alternatives Found ({len(proposals)}):[/bold cyan]")
                            for idx, prop in enumerate(proposals):
                                table = Table(box=None, header_style="bold dim")
                                table.add_column("Event", style="bold white", width=25)
                                table.add_column("New Time", style="green")
                                table.add_column("Cost", justify="right", style="dim")
                                
                                for t in prop['targets']:
                                    b = t.get('breakdown', {})
                                    table.add_row(t['summary'], t['new_start'].strftime("%H:%M"), f"{b.get('shift_mins',0)}m")
                                
                                advice_panel = Panel(
                                    Group(
                                        f"[bold magenta]Strategy:[/bold magenta] {prop['reason']}",
                                        f"[italic dim]Partner Advice:[/italic dim] {prop.get('tactical_advice', '')}",
                                        "",
                                        table
                                    ),
                                    title=f"Option {idx+1}",
                                    border_style="magenta" if idx == 0 else "dim"
                                )
                                console.print(advice_panel)
                            
                            # 3. NEGOTIATION INPUT
                            console.print("\n[bold slate_blue1]💡 NEGOTIATION:[/bold slate_blue1] [dim]Select (1-3) or tell me what to protect (e.g. 'Keep my Gym class')[/dim]")
                            interaction = console.input(f"   [bold yellow]Collaboration > [/bold yellow]").strip()
                            
                            if interaction.isdigit() and 1 <= int(interaction) <= len(proposals):
                                selected = proposals[int(interaction)-1]; applied_fix = True
                                for t in selected['targets']:
                                    if t['id'] == "new_event": event.start, event.end = t['new_start'].isoformat(), t['new_end'].isoformat()
                                    elif t['id'].startswith("split"):
                                        part = event.model_copy(); part.title = t['summary']; part.start, part.end = t['new_start'].isoformat(), t['new_end'].isoformat()
                                        event_parts.append(part)
                                    else: pending_updates.append(t)
                                LearningEngine.apply_feedback(True, selected)
                                break
                            elif interaction:
                                # Enhanced Intent Extraction via StrategicPartner
                                intent = StrategicPartner.extract_intent(interaction, busy)
                                if intent['type'] == "lock_event":
                                    fixed_ids.append(intent['id'])
                                    LearningEngine.record_lock(intent['summary'])
                                    console.print(f"   [cyan]🔒 Strategy Adjusted: Protecting '{intent['summary']}'. Re-solving...[/cyan]"); continue
                                elif intent['type'] == "constrain_time":
                                    console.print(f"   [cyan]⏳ Strategy Adjusted: Avoiding {intent['constraint']} slots. Re-solving...[/cyan]"); continue
                                break
                            else: break
                            
                        if not applied_fix:
                            suggestions = find_free_slots(service, event.start)
                            if suggestions:
                                s_start, s_end = suggestions[0]
                                if console.input(f"👉 Next free: {s_start.strftime('%H:%M')}. Switch? (y/n): ").strip().lower() in ['y', 'yes', '']:
                                    dur = parser.parse(event.end) - parser.parse(event.start)
                                    event.start, event.end = s_start.isoformat(), (s_start + dur).isoformat()

                    show_event_panel(event)
                    STATE.last_event = event
                    choice = console.input("\n[bold white]Proceed?[/bold white] (y/n/e): ").strip().lower()
                    if choice in ['y', 'yes', '']:
                        for t in pending_updates: update_event(service, t['id'], EventDetails(action="update", start=t['new_start'].isoformat(), end=t['new_end'].isoformat()))
                        if event_parts:
                            for p in event_parts: create_event(service, p)
                        else: create_event(service, event)
                        console.print("[green]✅ Success.[/green]"); STATE.last_event = None
                    elif choice in ['n', 'no']: STATE.last_event = None

            except KeyboardInterrupt: break
            except Exception as e:
                logger.error(f"Runtime Error: {e}", exc_info=True)
                STATE.last_event = None
    finally:
        if os.path.exists("token.json"): os.remove("token.json")

if __name__ == "__main__":
    main()
