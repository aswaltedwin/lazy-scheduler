# System Architecture: lazy-scheduler

The **lazy-scheduler** is a tool designed to help you manage your Google Calendar by automatically scoring tasks and resolving schedule conflicts.

## 1. How it Works
The system follows a simple pipeline to process your input and update your calendar:

### Stage A: Scoring
Every task is assigned a score to help prioritize it:
- **Base Priority**: Set based on keywords in the task title (e.g., "meeting" is high, "gym" is low).
- **History**: Tasks you missed before get a small boost so you prioritize them.
- **Energy Fit**: Tries to match high-effort tasks with your preferred working hours.

### Stage B: Conflict Detection
The tool checks your Google Calendar for any existing events that overlap with the new task.

### Stage C: Conflict Resolution (OR-Tools)
If a conflict is found, the tool uses the **OR-Tools CP-SAT Solver** to:
- Compare the scores of conflicting tasks.
- Suggest a way to move or shift events to make everything fit.
- Present you with a "Magic Fix" proposal to approve or deny.

### Stage D: Update
Once you approve a change, the tool uses the Google Calendar API to create, move, or delete events as needed.

---

## 2. Key Features
- **Missed Task Recovery**: Automatically increases the importance of tasks you haven't completed.
- **Simple Conflict Fixing**: Gives you a clear suggestion on how to fix overlaps.
- **Google Calendar Integration**: Syncs directly with your primary calendar.
- **Natural Language Parsing**: Understands basic commands like "Meeting tomorrow at 2pm".
