import datetime
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
    update_event
)

console = Console()

def show_event_panel(event: EventDetails, title="Proposed Event"):
    content = f"[bold white]📅 Title    :[/bold white] {event.title}\n"
    content += f"[bold white]🕒 Time     :[/bold white] {event.start[:16].replace('T',' ')} [dim]→[/dim] {event.end[:16].replace('T',' ')}\n"
    if event.description: content += f"[bold white]📝 Notes    :[/bold white] {event.description}\n"
    if event.location:    content += f"[bold white]📍 Location :[/bold white] {event.location}\n"
    if event.attendees:   content += f"[bold white]👥 Invite   :[/bold white] {', '.join(event.attendees)}\n"
    if event.reminders_minutes: 
        rem_str = ", ".join([f"{m}m" for m in event.reminders_minutes])
        content += f"[bold white]🔔 Alarms   :[/bold white] {rem_str} before\n"
    if event.recurrence:  content += f"[bold white]🔄 Repeat   :[/bold white] {event.recurrence[0]}\n"
    if event.add_meeting: content += f"[bold white]📹 Video    :[/bold white] Google Meet Link enabled\n"
    
    console.print(Panel(content, title=f"[bold cyan]{title}[/bold cyan]", border_style="cyan", expand=False))

def show_schedule_table(events):
    if not events:
        console.print("[yellow]No upcoming events found.[/yellow]")
        return
    table = Table(title="Your Schedule", show_header=True, header_style="bold magenta", box=None)
    table.add_column("Time", style="dim", width=12)
    table.add_column("Event")
    
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        time_str = parser.parse(start).strftime('%H:%M') if 'T' in start else "All Day"
        table.add_row(time_str, event['summary'])
    
    console.print(table)

def main():
    console.print(Panel(f"Model: [bold cyan]{CONFIG.model}[/bold cyan] | Timezone: [bold magenta]{CONFIG.timezone}[/bold magenta]", title="🐢 [bold]LazyScheduler[/bold]", border_style="green"))
    
    try:
        service = get_calendar_service()
    except Exception as e:
        console.print(f"[bold red]Failed to connect to Google Calendar:[/bold red] {e}")
        return

    while True:
        try:
            # Visual feedback if we're in the middle of a correction flow
            prefix = "[bold yellow](Correction)[/bold yellow] " if STATE.last_event else ""
            prompt_text = f"\n{prefix}[bold green]You:[/bold green] "
            
            user_input = console.input(prompt_text).strip()
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                console.print("[yellow]👋 Stopped.[/yellow]"); break
            if not user_input: continue

            # Core AI Parsing (passes context for corrections)
            event = parse_natural_language(user_input, context=STATE.last_event)
            
            if event.action == "list":
                items = list_upcoming_events(service, event.start, event.end)
                show_schedule_table(items)
                STATE.last_event = None
                
            elif event.action == "find_slot":
                slots = find_free_slots(service, event.start)
                if slots:
                    console.print("\n🆓 [bold]Available Free Slots:[/bold]")
                    for i, s in enumerate(slots): console.print(f"   {i+1}. {s.strftime('%Y-%m-%d %H:%M')}")
                else: console.print("[yellow]No free slots found.[/yellow]")
                STATE.last_event = None
                
            elif event.action in ["delete", "update"]:
                matches = find_event(service, event.search_query)
                if not matches:
                    console.print(f"[yellow]No match found for '{event.search_query}'[/yellow]")
                else:
                    target = matches[0]
                    target_start = target['start'].get('dateTime', target['start'].get('date'))
                    console.print(f"🎯 Found: [bold]{target['summary']}[/bold] at {target_start[:16]}")
                    
                    confirm_prompt = "Update this event? (y/n): " if event.action == "update" else "Delete this event? (y/n): "
                    choice = console.input(confirm_prompt).lower()
                    if choice in ['y', 'yes']:
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
                    console.print("\n[bold red]⚠️ CONFLICT:[/bold red] You are already busy during this time!")
                    suggestions = find_free_slots(service, event.start)
                    if suggestions:
                        suggestion = suggestions[0]
                        console.print(f"👉 Next free slot: [bold]{suggestion.strftime('%Y-%m-%d %H:%M')}[/bold]")
                        if console.input("\nSwitch to this time? (y/n): ").strip().lower() in ['y', 'yes']:
                            dur = parser.parse(event.end) - parser.parse(event.start)
                            event.start, event.end = suggestion.isoformat(), (suggestion + dur).isoformat()

                show_event_panel(event)
                STATE.last_event = event

                choice = console.input("\n[bold white]Proceed?[/bold white] ([green]y[/green]/[red]n[/red]/correct): ").strip().lower()
                if choice in ['y', 'yes']:
                    res = create_event(service, event)
                    if res and 'htmlLink' in res:
                        console.print(f"[green]✅ Created: [u]{res.get('htmlLink')}[/u][/green]")
                    else:
                        console.print("[green]✅ Created, but could not retrieve link.[/green]")
                    STATE.last_event = None
                elif choice in ['n', 'no']:
                    console.print("[yellow]Cancelled.[/yellow]")
                    STATE.last_event = None
                else:
                    # Anything else is a refinement/correction.
                    # We 'continue' the loop, and the next 'parse_natural_language' call 
                    # will use STATE.last_event as context.
                    console.print("[dim italic]Processing correction...[/dim italic]")
                    continue

        except KeyboardInterrupt:
            console.print("\n[yellow]👋 Goodbye![/yellow]"); break
        except Exception as e:
            console.print(f"[red]❌ Error: {e}[/red]")
            # Important: Clear state on error to prevent broken context loops
            STATE.last_event = None

if __name__ == "__main__":
    main()
