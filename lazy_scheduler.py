import os
import json
import datetime
import re
import time
import uuid
import ollama
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dateutil import parser, tz
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.progress import SpinnerColumn, TextColumn, Progress

console = Console()

# ====================== CONFIG & STATE ======================
class UserConfig(BaseModel):
    model: str = "qwen2.5:7b"
    timezone: str = "Asia/Kolkata"
    working_start: int = 9
    working_end: int = 19
    default_duration: int = 45

def load_config():
    if os.path.exists('config.json'):
        with open('config.json', 'r') as f:
            data = json.load(f)
            return UserConfig(
                model=data.get('model', 'qwen2.5:7b'),
                timezone=data.get('timezone', 'Asia/Kolkata'),
                working_start=data.get('working_hours', {}).get('start', 9),
                working_end=data.get('working_hours', {}).get('end', 19),
                default_duration=data.get('default_duration', 45)
            )
    return UserConfig()

CONFIG = load_config()

class EventDetails(BaseModel):
    action: str = "create"      # create, list, delete, update, find_slot
    title: str = ""
    start: str = ""
    end: str = ""
    description: str = ""
    location: str = ""
    attendees: list[str] = []
    add_meeting: bool = False
    search_query: str = ""      # text to search for (for delete/update)
    recurrence: list[str] = []  # RRULE format

class SessionState:
    last_event: EventDetails = None
    last_raw_input: str = ""

STATE = SessionState()

# ====================== GOOGLE SERVICE ======================
SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_calendar_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    
    return build('calendar', 'v3', credentials=creds)

# ====================== UI HELPERS ======================

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

# ====================== IMPROVED PARSING ======================
def parse_natural_language(text: str, context: EventDetails = None) -> EventDetails:
    today = datetime.datetime.now().strftime('%Y-%m-%d %A')
    
    context_str = ""
    if context:
        context_str = f"\nUser previously proposed: {context.json()}\nNow the user says: \"{text}\". If this is a correction, update the JSON. If it's a new request, start fresh."

    prompt = f"""You are LazyScheduler, a smart and minimal calendar assistant.{context_str}
Determine the user's intent and return ONLY valid JSON.

Actions:
- "create": Schedule a new event.
- "list": Show upcoming events for a specific date or range.
- "delete": Remove an existing event by name.
- "update": Modify an existing event (e.g., move time).
- "find_slot": Find a free time slot for a certain duration.

JSON format:
{{
  "action": "create | list | delete | update | find_slot",
  "title": "{context.title if context else 'Short event title'}",
  "start": "2026-04-16T16:00:00",
  "end": "2026-04-16T16:45:00",
  "description": "",
  "location": "",
  "attendees": [],
  "add_meeting": true | false,
  "search_query": "name of event to find (for update/delete)",
  "recurrence": ["RRULE:FREQ..."]
}}

Strict Rules:
- Today's date is {today}
- Timezone is {CONFIG.timezone}
- For "list", show today if no range given.
- "add_meeting": true if video call/meet mentioned.
- 24-hour format (e.g., 4 PM -> 16:00:00).
- Keep titles short and natural.

User request: "{text}"
"""

    with console.status(f"[bold yellow]Thinking with {CONFIG.model}...", spinner="dots"):
        response = ollama.chat(model=CONFIG.model, messages=[{'role': 'user', 'content': prompt}], format='json')
    data = json.loads(response['message']['content'])

    # Robust datetime parsing
    start_dt = parser.parse(data['start'])
    local_tz = tz.gettz(CONFIG.timezone)
    if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=local_tz)

    if data.get('end'):
        end_dt = parser.parse(data['end'])
        if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=local_tz)
    else:
        end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)

    if end_dt <= start_dt:
        end_dt = start_dt + datetime.timedelta(minutes=CONFIG.default_duration)

    # Email validation
    valid_attendees = []
    invalid_notes = []
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    for entry in data.get('attendees', []):
        if re.match(email_regex, entry.strip()):
            valid_attendees.append(entry.strip())
        else:
            invalid_notes.append(entry.strip())

    description = data.get('description', '')
    if invalid_notes:
        description += f"\nNote: Mentioned attendees (no email): {', '.join(invalid_notes)}"

    return EventDetails(
        action=data.get('action', 'create'),
        title=data.get('title', 'Untitled Event'),
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        description=description,
        location=data.get('location', ''),
        attendees=valid_attendees,
        add_meeting=data.get('add_meeting', False),
        search_query=data.get('search_query', ''),
        recurrence=data.get('recurrence', [])
    )

# ====================== API ACTIONS ======================

def check_conflicts(service, start_time: str, end_time: str):
    try:
        body = {"timeMin": start_time, "timeMax": end_time, "items": [{"id": "primary"}]}
        result = service.freebusy().query(body=body).execute()
        return result['calendars']['primary']['busy']
    except: return []

def find_free_slots(service, start_search: str, duration_mins=None):
    if duration_mins is None: duration_mins = CONFIG.default_duration
    try:
        search_dt = parser.parse(start_search)
        max_search = search_dt + datetime.timedelta(days=7)
        body = {"timeMin": search_dt.isoformat(), "timeMax": max_search.isoformat(), "items": [{"id": "primary"}]}
        busy = service.freebusy().query(body=body).execute()['calendars']['primary']['busy']
        
        free_slots = []
        current_dt = search_dt
        while len(free_slots) < 3 and current_dt < max_search:
            if current_dt.hour < CONFIG.working_start: current_dt = current_dt.replace(hour=CONFIG.working_start, minute=0)
            if current_dt.hour >= CONFIG.working_end: current_dt = (current_dt + datetime.timedelta(days=1)).replace(hour=CONFIG.working_start, minute=0)
            
            end_dt = current_dt + datetime.timedelta(minutes=duration_mins)
            conflict = False
            for b in busy:
                b_start, b_end = parser.parse(b['start']), parser.parse(b['end'])
                if (current_dt < b_end) and (end_dt > b_start):
                    current_dt = b_end
                    conflict = True
                    break
            if not conflict:
                free_slots.append(current_dt)
                current_dt = end_dt + datetime.timedelta(minutes=15)
        return free_slots
    except Exception as e:
        console.print(f"[red]❌ Error finding free slots: {e}[/red]")
        return []

def list_upcoming_events(service, start_time: str, end_time: str = None):
    try:
        if not end_time:
            end_time = (parser.parse(start_time) + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0).isoformat()
        events_result = service.events().list(
            calendarId='primary', timeMin=start_time, timeMax=end_time, singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        if not events:
            console.print("\n📭 [italic]Your calendar is clear for this period![/italic]")
            return
        show_schedule_table(events)
    except Exception as e:
        console.print(f"[red]❌ Error listing events: {e}[/red]")

def find_and_manage_event(service, search_query: str, action: str, update_data: EventDetails = None):
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        events_result = service.events().list(
            calendarId='primary', q=search_query, timeMin=now, maxResults=5, singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        if not events:
            console.print(f"🔎 [yellow]Couldn't find any events matching '{search_query}'.[/yellow]")
            return
        event = events[0]
        start = event['start'].get('dateTime', event['start'].get('date'))
        console.print(f"\n🎯 [bold]Found event:[/bold] '{event['summary']}' on {start[:10]} at {start[11:16]}")
        confirm = console.input(f"Confirm {action}? (y/n): ").strip().lower()
        if confirm not in ['y', 'yes']: return

        if action == "delete":
            service.events().delete(calendarId='primary', eventId=event['id']).execute()
            console.print("[green]🗑️ Event deleted.[/green]")
        elif action == "update" and update_data:
            event['start'] = {'dateTime': update_data.start, 'timeZone': CONFIG.timezone}
            event['end'] = {'dateTime': update_data.end, 'timeZone': CONFIG.timezone}
            service.events().update(calendarId='primary', eventId=event['id'], body=event).execute()
            console.print("[green]✏️ Event updated.[/green]")
    except Exception as e:
        console.print(f"[red]❌ Error managing event: {e}[/red]")

def create_event(service, event_details: EventDetails):
    event_body = {
        'summary': event_details.title,
        'location': event_details.location,
        'description': event_details.description,
        'start': {'dateTime': event_details.start, 'timeZone': CONFIG.timezone},
        'end':   {'dateTime': event_details.end,   'timeZone': CONFIG.timezone},
        'attendees': [{'email': e} for e in event_details.attendees],
        'reminders': {'useDefault': True}
    }
    if event_details.add_meeting:
        event_body['conferenceData'] = {
            'createRequest': {'requestId': str(uuid.uuid4()), 'conferenceSolutionKey': {'type': 'hangoutsMeet'}}
        }
    if event_details.recurrence:
        event_body['recurrence'] = event_details.recurrence

    for attempt in range(3):
        try:
            created = service.events().insert(
                calendarId='primary', body=event_body, sendUpdates='all', conferenceDataVersion=1
            ).execute()
            console.print(f"\n[bold green]✅ Event successfully created![/bold green]")
            if created.get('hangoutLink'): console.print(f"📹 [bold]Meet Link:[/bold] {created.get('hangoutLink')}")
            console.print(f"🔗 [blue][u]{created.get('htmlLink')}[/u][/blue]")
            return
        except HttpError as error:
            console.print(f"[red]❌ Google Calendar Error: {error}[/red]")
            break
        except Exception as e:
            if attempt < 2 and any(k in str(e).lower() for k in ["eof", "protocol", "timeout"]):
                time.sleep(1); continue
            console.print(f"[red]❌ Unexpected error: {e}[/red]")
            break

# ====================== MAIN ======================
def main():
    console.print(Panel(f"Model: [bold cyan]{CONFIG.model}[/bold cyan] | Timezone: [bold magenta]{CONFIG.timezone}[/bold magenta]", title="🐢 [bold]LazyScheduler[/bold]", border_style="green"))
    service = get_calendar_service()

    while True:
        user_input = console.input("\n[bold green]You:[/bold green] ").strip()
        if user_input.lower() in ['quit', 'exit', 'q']:
            console.print("[yellow]👋 Stopped.[/yellow]"); break
        if not user_input: continue

        try:
            event = parse_natural_language(user_input, context=STATE.last_event)
            
            if event.action == "list":
                list_upcoming_events(service, event.start, event.end)
                STATE.last_event = None
            elif event.action == "find_slot":
                slots = find_free_slots(service, event.start)
                if slots:
                    console.print("\n🆓 [bold]Available Free Slots:[/bold]")
                    for i, s in enumerate(slots): console.print(f"   {i+1}. {s.strftime('%Y-%m-%d %H:%M')}")
                else: console.print("[yellow]No free slots found.[/yellow]")
                STATE.last_event = None
            elif event.action == "delete":
                find_and_manage_event(service, event.search_query, "delete")
                STATE.last_event = None
            elif event.action == "update":
                find_and_manage_event(service, event.search_query, "update", event)
                STATE.last_event = None
            else: # create
                busy = check_conflicts(service, event.start, event.end)
                if busy:
                    console.print("\n[bold red]⚠️ CONFLICT:[/bold red] You are already busy!")
                    suggestions = find_free_slots(service, event.start)
                    if suggestions:
                        suggestion = suggestions[0]
                        console.print(f"👉 Suggesting next free slot: [bold]{suggestion.strftime('%Y-%m-%d %H:%M')}[/bold]")
                        if console.input("\nUse this time? (y/n): ").strip().lower() in ['y', 'yes']:
                            dur = parser.parse(event.end) - parser.parse(event.start)
                            event.start, event.end = suggestion.isoformat(), (suggestion + dur).isoformat()

                show_event_panel(event)
                STATE.last_event = event

                choice = console.input("\n[bold white]Create?[/bold white] ([green]y[/green]/[red]n[/red]/correct): ").strip().lower()
                if choice in ['y', 'yes']:
                    create_event(service, event)
                    STATE.last_event = None
                elif choice in ['n', 'no']:
                    console.print("[yellow]Cancelled.[/yellow]")
                    STATE.last_event = None
                else: # It's a correction
                    # Re-run the loop with the correction text
                    # The next iteration will pick up the context from STATE.last_event
                    continue 

        except Exception as e:
            console.print(f"[red]❌ Processing failed: {e}[/red]")

if __name__ == "__main__":
    main()