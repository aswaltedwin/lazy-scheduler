# 🐢 LazyScheduler

An intelligent, local-first calendar assistant that turns natural language into structured Google Calendar events, scores your tasks, and automatically solves scheduling conflicts using constraint optimization.

---

## ✨ Features

- **🗣️ Natural Language Intent Parsing**: Powered by Groq (llama-3.3-70b-versatile) and fast regex fallback for instant scheduling commands (e.g., * Meeting with Team tomorrow at 10am*).
- **🧩 Automatic Task Decomposition**: Automatically breaks large or vague tasks (e.g., *Study OS*, *Project work*) into manageable sub-tasks.
- **📊 Smart Scoring Engine**: Prioritizes events based on category keywords, energy levels, preferred working hours, and historical adherence.
- **⚡ Magic Fix Conflict Resolution**: Uses **Google OR-Tools CP-SAT Solver** to detect overlaps and compute the optimal sequence of shifts/reschedules instead of simply warning you.
- **🔄 Google Calendar Integration**: Syncs directly with your Google Calendar via OAuth 2.0 with atomic updates.
- **📈 Habit & Progress Tracking**: Remembers completed vs. missed tasks to help you catch up on what matters most.

---

## 🚀 Quick Start

### 1. Prerequisites
- **Python 3.10+**
- A **Groq API Key** (free at [console.groq.com](https://console.groq.com/keys))
- A **Google Cloud Project** with Google Calendar API enabled

### 2. Google OAuth Setup
1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Google Calendar API**.
3. Create an **OAuth 2.0 Client ID** (Application type: *Desktop App*).
4. Download the JSON credentials file and save it as credentials.json in the root folder of this repository.

### 3. Installation & Environment Configuration
Clone the repository and install the dependencies:

`ash
git clone https://github.com/aswaltedwin/lazy-scheduler.git
cd lazy-scheduler
pip install -r requirements.txt
`

Copy the .env.template to .env and insert your credentials:

`ash
cp .env.template .env
`

Edit .env:
`ini
GROQ_API_KEY=your_groq_api_key_here
`

### 4. Run LazyScheduler

`ash
python main.py
`

*On your first run, a browser window will open asking you to authenticate with your Google account. This saves your session token locally to 	oken.json for future seamless runs.*

---

## 💬 Example Commands

- **Schedule a Task**: *Gym tomorrow at 8am for 45 mins*
- **Work Session**: *Deep work on project report Thursday at 2pm*
- **Conflict Handling**: If an event overlaps with an existing schedule, LazyScheduler will calculate a **Magic Fix** proposal showing the recommended shifts and prompt you to approve or deny.
- **List / Query**: *What do I have tomorrow?*

---

## 📁 Project Structure

`	ext
lazy-scheduler/
├── core/
│   ├── adaptive_partner.py     # Habit learning & user preference tracking
│   ├── decision_engine.py      # Strategic decision & scoring evaluation
│   ├── optimization_engine.py  # Google OR-Tools CP-SAT solver for conflict resolution
│   └── orchestrator.py         # Central coordinator & interactive CLI
├── integrations/
│   ├── calendar_service.py     # Google Calendar API OAuth & event operations
│   └── llm_service.py          # Groq LLM client & intent parsing
├── parsing/
│   └── rule_parser.py          # Fast deterministic regex parser fallback
├── services/
│   └── scoring.py              # Priority & energy scoring algorithms
├── models.py                   # Pydantic data schemas
├── config.py                   # System configuration & persistent state management
├── main.py                     # Application entry point
├── requirements.txt            # Project dependencies
└── SYSTEM_ARCHITECTURE.md      # Detailed technical architecture document
`

---

## 📜 License
MIT License.
