# lazy-scheduler

A simple Python tool to help you score and organize your Google Calendar.

## What it does
- **Scoring**: Automatically scores tasks based on priority and history.
- **Conflict Fixing**: Suggests ways to move events when they overlap.
- **Progress Tracking**: Remembers which tasks you complete or miss.
- **Google Integration**: Connects directly to your Google Calendar.

## How to use
1. Add your `credentials.json` to the root folder.
2. Install dependencies: `pip install -r requirements.txt`.
3. Run the tool: `python main.py`.
4. Type your request (e.g., "Gym tomorrow at 8am").

## Project Structure
- `core/`: Main logic for scheduling and optimization.
- `parsing/`: Text processing and command understanding.
- `services/`: Scoring logic and historical data.
- `integrations/`: Google Calendar and LLM (Groq) connections.
- `models.py`: Data structures for the project.
- `config.py`: System settings and state loading.
