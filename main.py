import datetime
from dateutil import parser
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Import local modules
from config import CONFIG, STATE, EventDetails
from core import (
    get_calendar_service, 
    parse_natural_language, 
    check_conflicts, 
    find_free_slots, 
    create_event
)

console = Console()

def show_event_panel(event: EventDetails, title="Proposed Event"):
    content = f"[bold white]📅 Title   :[/bold white] {event.title}\n"
    content += f"[bold white]🕒 Time    :[/bold white] {event.start[:16].replace('T',' ')} [dim]→[/dim] {event.end[:16].replace('T',' ')}\n"
    if event.attendees: content += f"[bold white]👥 Invite  :[/bold white] {', '.join(event.attendees)}\n"
    if event.recurrence: content += f"[bold white]🔄 Repeat  :[/bold white] {event.recurrence[0]}\n"
    if event.add_meeting: content += f"[bold white]📹 Video   :[/bold white] Google Meet Link enabled\n"
    
    console.print(Panel(content, title=f"[bold cyan]{title}[/bold cyan]", border_style="cyan", expand=False))

def show_schedule_table(events):
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
    service = get_calendar_service()

    while True:
        user_input = console.input("\n[bold green]You:[/bold green] ").strip()
        if user_input.lower() in ['quit', 'exit', 'q']:
            console.print("[yellow]👋 Stopped.[/yellow]"); break
        if not user_input: continue

        try:
            # Pass STATE.last_event as context for natural corrections
            event = parse_natural_language(user_input, context=STATE.last_event)
            
            if event.action == "list":
                events_result = service.events().list(calendarId='primary', timeMin=event.start, timeMax=event.end, singleEvents=True, orderBy='startTime').execute()
                show_schedule_table(events_result.get('items', []))
                STATE.last_event = None
                
            elif event.action == "find_slot":
                slots = find_free_slots(service, event.start)
                if slots:
                    console.print("\n🆓 [bold]Available Free Slots:[/bold]")
                    for i, s in enumerate(slots): console.print(f"   {i+1}. {s.strftime('%Y-%m-%d %H:%M')}")
                else: console.print("[yellow]No free slots found.[/yellow]")
                STATE.last_event = None
                
            elif event.action == "delete" or event.action == "update":
                # Implementation for search and delete/update
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                events = service.events().list(calendarId='primary', q=event.search_query, timeMin=now, maxResults=1).execute().get('items', [])
                if not events:
                    console.print(f"[yellow]No match found for '{event.search_query}'[/yellow]")
                else:
                    target = events[0]
                    console.print(f"🎯 Found: '{target['summary']}' at {target['start'].get('dateTime')}")
                    if console.input(f"Confirm {event.action}? (y/n): ").lower() in ['y', 'yes']:
                        if event.action == "delete": service.events().delete(calendarId='primary', eventId=target['id']).execute()
                        else:
                            target['start'] = {'dateTime': event.start, 'timeZone': CONFIG.timezone}
                            target['end'] = {'dateTime': event.end, 'timeZone': CONFIG.timezone}
                            service.events().update(calendarId='primary', eventId=target['id'], body=target).execute()
                        console.print(f"[green]Success![/green]")
                STATE.last_event = None
                
            else: # create
                busy = check_conflicts(service, event.start, event.end)
                if busy:
                    console.print("\n[bold red]⚠️ CONFLICT:[/bold red] You are already busy!")
                    suggestions = find_free_slots(service, event.start)
                    if suggestions:
                        suggestion = suggestions[0]
                        console.print(f"👉 Next free slot: [bold]{suggestion.strftime('%Y-%m-%d %H:%M')}[/bold]")
                        if console.input("\nSwitch to this time? (y/n): ").strip().lower() in ['y', 'yes']:
                            dur = parser.parse(event.end) - parser.parse(event.start)
                            event.start, event.end = suggestion.isoformat(), (suggestion + dur).isoformat()

                show_event_panel(event)
                STATE.last_event = event

                choice = console.input("\n[bold white]Create?[/bold white] ([green]y[/green]/[red]n[/red]/correct): ").strip().lower()
                if choice in ['y', 'yes']:
                    res = create_event(service, event)
                    console.print(f"[green]✅ Created: [u]{res.get('htmlLink')}[/u][/green]")
                    STATE.last_event = None
                elif choice in ['n', 'no']:
                    console.print("[yellow]Cancelled.[/yellow]")
                    STATE.last_event = None
                else:
                    # User provided a correction, loop again with the new input and previous context
                    continue

        except Exception as e:
            console.print(f"[red]❌ Error: {e}[/red]")

if __name__ == "__main__":
    main()
