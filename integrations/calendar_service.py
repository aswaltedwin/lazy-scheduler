import os
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from utils.logger import logger

SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_calendar_service():
    """Authenticates and returns a Google Calendar service instance."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('credentials.json'):
                raise RuntimeError("credentials.json not found. Please provide Google API credentials.")
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    try:
        service = build('calendar', 'v3', credentials=creds)
        return service
    except HttpError as error:
        logger.error(f"Google Calendar API Error: {error}")
        raise

def list_events(service, start_time, end_time, max_results=50):
    """Lists events within a given time range."""
    try:
        # Debug: console.print(f"[dim]Searching from {start_time} to {end_time}[/dim]")
        events_result = service.events().list(
            calendarId='primary', 
            timeMin=start_time,
            timeMax=end_time,
            maxResults=max_results, 
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        items = events_result.get('items', [])
        return items
    except Exception as e:
        logger.error(f"Error listing events: {e}")
        return []

def create_event(service, event_data):
    """Creates a new event on the primary calendar."""
    try:
        # Check if recurrence is present and is a list
        if 'recurrence' in event_data and not isinstance(event_data['recurrence'], list):
            event_data['recurrence'] = [event_data['recurrence']]
            
        event = service.events().insert(calendarId='primary', body=event_data).execute()
        logger.info(f"Event created: {event.get('htmlLink')}")
        return event
    except Exception as e:
        logger.error(f"Error creating event: {e}")
        return None

def update_event(service, event_id, event_data):
    """Updates an existing event."""
    try:
        event = service.events().patch(calendarId='primary', eventId=event_id, body=event_data).execute()
        logger.info(f"Event updated: {event.get('htmlLink')}")
        return event
    except Exception as e:
        logger.error(f"Error updating event {event_id}: {e}")
        return None

def delete_event(service, event_id):
    """Deletes an event."""
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        logger.info(f"Event deleted: {event_id}")
        return True
    except Exception as e:
        logger.error(f"Error deleting event {event_id}: {e}")
        return False
