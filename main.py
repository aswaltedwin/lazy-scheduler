import datetime
import logging
from dateutil import parser
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Import local modules
from config import CONFIG
from core import (
    STATE,
    EventDetails,
    get_calendar_service, 
    parse_natural_language, 
    check_conflicts, 
    find_free_slots, 
    create_event,
    list_upcoming_events,
    find_event,
    delete_event,
    update_event,
    get_magic_fix_proposal,
    logger

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
    if event.add_meeting: content += f"[bold white]📹 Video    :[/bold white] Google Meet Link will be generated\n"
    
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
    console.print(Panel(f"Model: [bold cyan]{CONFIG.model}[/bold cyan] | Timezone: [bold magenta]{CONFIG.timezone}[/bold magenta]", title="🐢 [bold]LazyScheduler[/bold]", border_style="green"))
    
    try:
        service = get_calendar_service()
    except Exception as e:
        logger.error(f"Initialization Failed: {e}")
        console.print(f"[bold red]Critical Error:[/bold red] Could not connect to Google Calendar. See logs.")
        return

    while True:
        try:
            prefix = "[bold yellow]🔨 (Correction Mode)[/bold yellow] " if STATE.last_event else ""
            user_input = console.input(f"\n{prefix}[bold green]You:[/bold green] ").strip()
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                console.print("[yellow]👋 Stopped.[/yellow]"); break
            if not user_input: continue

            # Core AI Parsing
            try:
                event = parse_natural_language(user_input, context=STATE.last_event)
            except ValueError as ve:
                console.print(f"[bold red]Parsing Error:[/bold red] {ve}")
                continue
            
            if event.action == "list":
                items = list_upcoming_events(service, event.start, event.end)
                show_schedule_table(items)
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
                matches = find_event(service, event.search_query)
                if not matches:
                    console.print(f"[yellow]No match found for '{event.search_query}'[/yellow]")
                else:
                    target = matches[0]
                    target_start = target['start'].get('dateTime', target['start'].get('date'))
                    console.print(f"🎯 Found: [bold]{target['summary']}[/bold] at {target_start[:16]}")
                    
                    prompt = "Update this event? (y/n): " if event.action == "update" else "Delete this event? (y/n): "
                    if console.input(prompt).lower() in ['y', 'yes']:
                        if event.action == "delete": 
                            delete_event(service, target['id'])
                            console.print(f"[green]🗑️ Deleted successfully.[/green]")
                        else:
                            update_event(service, target['id'], event)
                            console.print(f"[green]🔄 Updated successfully.[/green]")
                STATE.last_event = None
                
            else: # create action
                busy = check_conflicts(service, event.start, event.end)
                if busy:
                    console.print(f"\n[bold red]⚠️  CONFLICT:[/bold red] You have {len(busy)} event(s) during this time.")
                    
                    # Try Magic Fix
                    magic_proposal = get_magic_fix_proposal(service, event, busy)
                    if magic_proposal:
                        console.print(f"🧙 [bold cyan]Magic Fix Available:[/bold cyan] I can move [italic]'{magic_proposal['target_summary']}'[/italic] to [bold]{magic_proposal['new_start'].strftime('%H:%M')}[/bold] to make room.")
                        if console.input("   [white]Apply Magic Fix? (y/n): [/white]").strip().lower() in ['y', 'yes']:
                            # Execute the move
                            update_event(service, magic_proposal['target_id'], EventDetails(
                                action="update",
                                start=magic_proposal['new_start'].isoformat(),
                                end=magic_proposal['new_end'].isoformat()
                            ))
                            console.print(f"[green]✨ Shifted '{magic_proposal['target_summary']}' successfully.[/green]")
                    else:
                        suggestions = find_free_slots(service, event.start)
                        if suggestions:
                            s_start, s_end = suggestions[0]
                            console.print(f"👉 Next free block: [bold]{s_start.strftime('%a, %b %d at %H:%M')} [dim]→[/dim] {s_end.strftime('%H:%M')}[/bold]")
                            if console.input("\nSwitch to this time? (y/n): ").strip().lower() in ['y', 'yes']:
                                dur = parser.parse(event.end) - parser.parse(event.start)
                                event.start, event.end = s_start.isoformat(), (s_start + dur).isoformat()

                show_event_panel(event)
                STATE.last_event = event

                choice = console.input("\n[bold white]Proceed?[/bold white] ([green]y[/green]/[red]n[/red]/correct): ").strip().lower()
                if choice in ['y', 'yes']:
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
                else:
                    console.print("[dim italic]Processing correction...[/dim italic]")
                    continue

        except KeyboardInterrupt:
            console.print("\n[yellow]👋 Goodbye![/yellow]"); break
        except Exception as e:
            logger.error(f"Runtime Exception: {e}", exc_info=True)
            console.print(f"[bold red]Unexpected Error:[/bold red] Check logs for details.")
            STATE.last_event = None

if __name__ == "__main__":
    main()
