import datetime
import logging
from dateutil import parser
from rich.console import Console
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
    get_magic_fix_proposal,
    logger,
    _calculate_move_cost,
    LearningEngine
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
    """Displays user events in a clean ASCII table."""
    if not events:
        console.print("[yellow]No upcoming events found.[/yellow]")
        return
    table = Table(title="Your Schedule", show_header=True, header_style="bold magenta", box=None)
    table.add_column("Date/Time", style="dim", width=22)
    table.add_column("Event")
    
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

    
    console.print(table)

def main():
    """Main CLI interaction loop."""
    console.print(Panel(f"Model: [bold cyan]{CONFIG.model}[/bold cyan] | Timezone: [bold magenta]{CONFIG.timezone}[/bold magenta]", title="🐢 [bold]LazyScheduler Phase 1[/bold]", border_style="green"))
    
    try:
        service = get_calendar_service()
    except Exception as e:
        logger.error(f"Initialization Failed: {e}")
        console.print(f"[bold red]Critical Error:[/bold red] Could not connect to Google Calendar. See logs.")
        return

    while True:
        try:
            prefix = "[bold yellow]🔨 (Edit Mode)[/bold yellow] " if STATE.last_event else ""
            user_input = console.input(f"\n{prefix}[bold green]You:[/bold green] ").strip()
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                console.print("[yellow]👋 Stopped.[/yellow]"); break
            if not user_input: continue

            # Core AI Parsing
            try:
                event = parse_natural_language(user_input, context=STATE.last_event)
            except ParsingError as pe:
                console.print(f"\n[bold red]Parsing Error:[/bold red] {pe}")
                if pe.suggestion:
                    console.print(Panel(
                        f"I'm not sure I got that. Did you mean:\n[bold cyan]'{pe.suggestion}'[/bold cyan]\n\n[dim](Press Enter to use this, or type something else)[/dim]",
                        title="💡 Suggestion", border_style="cyan", expand=False
                    ))
                    followup = console.input("[bold yellow]Correction > [/bold yellow]").strip()
                    if followup == "":
                        # Use the suggestion
                        event = parse_natural_language(pe.suggestion, context=STATE.last_event)
                    else:
                        # Start over with new input
                        user_input = followup
                        # We need to loop back manually or handle this input immediately.
                        # For simplicity, we just process 'followup' as the next command.
                        event = parse_natural_language(followup, context=STATE.last_event)
                else:
                    continue
            except ValueError as ve:
                console.print(f"[bold red]Parsing Error:[/bold red] {ve}")
                continue
            
            if event.action == "list":
                try:
                    items = list_upcoming_events(service, event.start, event.end)
                    if event.search_query:
                        # Filter items by search_query if present
                        items = [it for it in items if event.search_query.lower() in it.get('summary', '').lower()]
                    
                    show_schedule_table(items)
                except Exception as e:
                    console.print(f"[bold red]Fetch Error:[/bold red] Could not retrieve schedule. ({e})")
                STATE.last_event = None

                
            elif event.action == "find_slot":
                # Find all free blocks, using the duration explicitly requested (if any)
                blocks = find_free_slots(service, event.start, min_duration_mins=event.duration_mins)

                if blocks:
                    console.print(f"\n🆓 [bold]Your Availability:[/bold]")
                    for i, (s, e) in enumerate(blocks):
                        dur = e - s
                        hours, remainder = divmod(int(dur.total_seconds() / 60), 60)
                        dur_str = f"{hours}h {remainder}m" if hours > 0 else f"{remainder}m"
                        console.print(f"   {i+1}. {s.strftime('%a, %b %d')}: {s.strftime('%H:%M')} [dim]→[/dim] {e.strftime('%H:%M')} [bold cyan]({dur_str} free)[/bold cyan]")
                else:
                    console.print("[yellow]No free blocks found.[/yellow]")
                STATE.last_event = None

                
            elif event.action in ["delete", "update"]:
                # Optimization: If correcting a proposal, handle "update" action as refinement
                if STATE.last_event and STATE.last_event.action == "create" and event.action == "update":
                    if not event.search_query or event.search_query.lower() in STATE.last_event.title.lower():
                        event.action = "create"
                        show_event_panel(event)
                        STATE.last_event = event
                        choice = console.input("\n[bold white]Proceed?[/bold white] ([green]y[/green]- yes, [red]n[/red]- no, [yellow]e[/yellow]- edit): ").strip().lower()
                        if choice in ['y', 'yes', '']:
                            res = create_event(service, event)
                            if res:
                                msg = "[green]✅ Event Created![/green]\n"
                                meet_link = next((ep.get('uri') for ep in res.get('conferenceData', {}).get('entryPoints', []) if ep.get('entryPointType') == 'video'), None)
                                if meet_link: msg += f"📹 [bold cyan]Google Meet:[/bold cyan] [u]{meet_link}[/u]\n"
                                if res.get('htmlLink'): msg += f"📅 [dim]Calendar Link: {res.get('htmlLink')}[/dim]"
                                console.print(Panel(msg, border_style="green", expand=False))
                            else:
                                console.print("[red]❌ Failed to create event.[/red]")
                            STATE.last_event = None
                        elif choice in ['n', 'no']:
                            console.print("[yellow]Cancelled.[/yellow]")
                            STATE.last_event = None
                        elif choice in ['e', 'edit']:
                            console.print("[dim italic]Processing edit...[/dim italic]")
                        continue

                matches = find_event(service, event.search_query)
                
                # If no specific match by name, check if it's a range delete (e.g., "all tomorrow")
                if not matches and event.action == "delete" and event.start and event.end:
                    matches = list_upcoming_events(service, event.start, event.end)

                if not matches:
                    console.print(f"[yellow]No match found for '{event.search_query}'[/yellow]")
                elif len(matches) > 1 and event.action == "delete":
                    # Batch Delete Flow
                    console.print(f"\n[bold red]🗑️  Batch Delete:[/bold red] Found {len(matches)} events in this range:")
                    for m in matches:
                        m_s = m['start'].get('dateTime', m['start'].get('date'))[:16].replace('T', ' ')
                        console.print(f"   - [bold]{m['summary']}[/bold] at {m_s}")
                    
                    if console.input(f"\n[bold red]Delete ALL {len(matches)} events?[/bold red] (y/n): ").lower() in ['y', 'yes']:
                        for m in matches:
                            delete_event(service, m['id'])
                        console.print(f"[green]💥 Cleared {len(matches)} events successfully.[/green]")
                else:
                    # Single Match Flow
                    target = matches[0]
                    target_start = target['start'].get('dateTime', target['start'].get('date'))
                    console.print(f"🎯 Found: [bold]{target['summary']}[/bold] at {target_start[:16]}")
                    
                    prompt = "Update this event? (y/n): " if event.action == "update" else "Delete this event? (y/n): "
                    if console.input(prompt).lower() in ['y', 'yes']:
                        try:
                            if event.action == "delete": 
                                delete_event(service, target['id'])
                                console.print(f"[green]🗑️ Deleted successfully: [bold]{target['summary']}[/bold][/green]")
                            else:
                                update_event(service, target['id'], event)
                                console.print(f"[green]🔄 Updated successfully: [bold]{event.title or target['summary']}[/bold][/green]")
                        except Exception as e:
                            console.print(f"[bold red]Operation Failed:[/bold red] {e}")
                STATE.last_event = None

                
            else: # create action
                try:
                    busy = check_conflicts(service, event.start, event.end)
                    if busy:
                        console.print(f"\n[bold red]⚠️  CONFLICT:[/bold red] You have {len(busy)} event(s) during this time.")
                except Exception as e:
                    console.print(f"[bold yellow]⚠️  Warning:[/bold yellow] Could not check for conflicts. ({e})")
                    busy = []
                    
                    # Try Magic Fix
                    magic_proposal = get_magic_fix_proposal(service, event, busy)
                    applied_fix = False
                    if magic_proposal:
                        from rich.table import Table
                        targets = magic_proposal['targets']
                        
                        table = Table(title="🧙 Magic Fix: Adaptive Schedule Impact", title_style="bold cyan", border_style="dim")
                        table.add_column("Event", style="bold white")
                        table.add_column("Priority", justify="center")
                        table.add_column("Proposed New Time", style="green")
                        table.add_column("Shift", justify="right")
                        table.add_column("Pain Score", justify="right", style="bold")
                        
                        for t in targets:
                            shift_mins = int((t['new_start'] - t['old_start']).total_seconds() / 60)
                            b = t.get('breakdown', {})
                            
                            # Build a mini breakdown string for the cost column
                            score_details = f"{int(t.get('breakdown', {}).get('priority', 0) + t.get('breakdown', {}).get('distance', 0) + t.get('breakdown', {}).get('duration', 0) + t.get('breakdown', {}).get('bias', 0))}"
                            
                            table.add_row(
                                t['summary'],
                                f"P{int(b.get('priority_val', 2))}",
                                t['new_start'].strftime("%H:%M"),
                                f"{shift_mins}m",
                                score_details
                            )
                        
                        console.print(table)
                        console.print(f"   [dim]Strategy: {magic_proposal['reason']}[/dim]")
                        
                        if CONFIG.behavior.accepted_fixes > 0:
                            console.print(f"   [italic dim]System has learned from {CONFIG.behavior.accepted_fixes} previous successes.[/italic dim]")

                        choice = console.input("\n   [white]Apply these shifts to make room? (y/n): [/white]").strip().lower()
                        if choice in ['y', 'yes', '']:
                            console.print("   [dim]Applying atomic transaction...[/dim]")
                            for t in targets:
                                update_event(service, t['id'], EventDetails(
                                    action="update",
                                    start=t['new_start'].isoformat(),
                                    end=t['new_end'].isoformat()
                                ))
                            console.print(f"   [green]✨ Successfully shifted {len(targets)} event(s).[/green]")
                            applied_fix = True
                            LearningEngine.apply_feedback(True, magic_proposal)
                        else:
                            console.print("   [dim]Feedback captured. Adjusting weights...[/dim]")
                            LearningEngine.apply_feedback(False, magic_proposal)



                    
                    if not applied_fix:
                        suggestions = find_free_slots(service, event.start)
                        if suggestions:
                            s_start, s_end = suggestions[0]
                            console.print(f"👉 Next free block: [bold]{s_start.strftime('%a, %b %d at %H:%M')} [dim]→[/dim] {s_end.strftime('%H:%M')}[/bold]")
                            if console.input("\nSwitch to this time? (y/n): ").strip().lower() in ['y', 'yes']:
                                dur = parser.parse(event.end) - parser.parse(event.start)
                                event.start, event.end = s_start.isoformat(), (s_start + dur).isoformat()


                show_event_panel(event)
                STATE.last_event = event

                # Smart Intent: Don't ask for location if it's a task/reminder or 0-duration
                non_physical_keywords = ["deadline", "reminder", "due", "task", "check", "pay", "buy", "bill", "ship"]
                is_task = any(k in event.title.lower() for k in non_physical_keywords) or (event.start == event.end)

                if not event.location.strip() and not event.add_meeting and event.action == "create" and not is_task:
                    prompt = "\n📍 [cyan]Where is this happening?[/cyan] (a- add location, y- skip, n- cancel, e- edit): "
                else:
                    prompt = "\n[bold white]Proceed?[/bold white] ([green]y[/green]- yes, [red]n[/red]- no, [yellow]e[/yellow]- edit): "

                choice = console.input(prompt).strip().lower()
                
                if choice in ['y', 'yes', '', 'y- yes']:
                    res = create_event(service, event)
                    if res:
                        msg = "[green]✅ Event Created![/green]\n"
                        meet_link = next((ep.get('uri') for ep in res.get('conferenceData', {}).get('entryPoints', []) if ep.get('entryPointType') == 'video'), None)
                        if meet_link: msg += f"📹 [bold cyan]Google Meet:[/bold cyan] [u]{meet_link}[/u]\n"
                        if res.get('htmlLink'): msg += f"📅 [dim]Calendar Link: {res.get('htmlLink')}[/dim]"
                        console.print(Panel(msg, border_style="green", expand=False))
                    else:
                        console.print("[red]❌ Failed to create event.[/red]")
                    STATE.last_event = None
                elif choice in ['n', 'no', 'n- no']:
                    console.print("[yellow]Cancelled.[/yellow]")
                    STATE.last_event = None
                elif choice in ['e', 'edit', 'e- edit']:
                    console.print("[dim italic]Processing edit...[/dim italic]")
                    continue
                elif choice in ['a', 'a- add location']:
                    loc = console.input("   📍 [cyan]Location:[/cyan] ").strip()
                    if loc:
                        event.location = loc
                        console.print(f"[dim]Location set to: {loc}[/dim]")
                    continue
                else:
                    # If location was missing and user typed text directly, treat it as location
                    if not event.location.strip() and not event.add_meeting:
                        event.location = choice
                        console.print(f"[dim]Setting location to: {choice}[/dim]")
                        continue
                    
                    console.print("[dim italic]Processing edit...[/dim italic]")
                    continue

        except KeyboardInterrupt:
            console.print("\n[yellow]👋 Goodbye![/yellow]"); break
        except Exception as e:
            logger.error(f"Runtime Exception: {e}", exc_info=True)
            console.print(f"[bold red]Unexpected Error:[/bold red] Check logs for details.")
            STATE.last_event = None

if __name__ == "__main__":
    main()
