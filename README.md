# 🐢 LazyScheduler

**LazyScheduler** is a professional, secure, and minimal AI-powered calendar assistant. It transforms natural language into perfectly structured Google Calendar events, handling conflict checking, availability detection, and intent parsing with a premium terminal experience.

Designed for the "Productive Procrastinator," LazyScheduler handles the thinking so you don't have to.

---

## ✨ Key Features

- **🧠 Advanced Intent Parsing**: Powered by Ollama (`qwen2.5:7b`), it understands complex requests like *"Move my 4 PM sync to 5 PM"* or *"Make that meeting an hour long instead."*
- **🔓 Dynamic Availability Detection**: Scans your schedule to find all contiguous "gaps" (e.g., `09:00 → 13:45`) and calculates how much free time is in each window.
- **🛡️ Built-in Security**: Includes an input sanitation layer to protect against prompt injection and strict Pydantic validation for AI outputs.
- **💎 Premium CLI UI**: A beautiful terminal interface using the **Rich** library, featuring thinking spinners, vibrant color-coded cards, and professional schedule tables.
- **📜 Professional Logging**: Every action, API call, and internal event is tracked in `scheduler.log` for easy auditing and debugging.
- **🔁 Recurring & Video Support**: Full support for RFC5545 recurrence rules and automatic Google Meet link generation.

---

## 🏗️ Architecture

LazyScheduler follows a modular, hardened architecture:
- **`core.py`**: The engine. Handles modular parsing, sanitization, and Google Calendar API orchestration.
- **`main.py`**: The UI layer. Manages the CLI loop and user interaction.
- **`config.py`**: Auto-generates configuration if missing and manages environment settings.

---

## 🚀 Getting Started

### 1. Prerequisites
- **Python 3.10+**
- **Ollama**: [Download here](https://ollama.com/)
- **Google Cloud Account**: Enable the Google Calendar API and download your `credentials.json`.

### 2. Installation
```bash
# Clone the repo
git clone https://github.com/aswaltedwin/lazy-scheduler.git
cd lazy-scheduler

# Install dependencies
pip install ollama google-api-python-client google-auth-oauthlib python-dateutil pydantic rich
```

### 3. Model Setup
```bash
ollama pull qwen2.5:7b
```

### 4. Configuration
On first run, the app will generate a `config.json`. You can customize:
- `timezone`: Your local timezone (e.g., `Europe/London`).
- `working_start` / `working_end`: Defines your daily search window for free time.
- `model`: Change the LLM used for parsing (defaults to `qwen2.5:7b`).

---

## 🛠️ Usage

```bash
python main.py
```

### Example Commands:
- **Create**: *"Schedule a team sync with rahul@gmail.com tomorrow at 3 PM"*
- **Availability**: *"When am I free tomorrow?"* or *"Find a 2 hour free slot this week"*
- **Review**: *"List all my events this week"*
- **Manage**: *"Delete the gym session on Friday"*
- **Correct**: *"Actually, move that meeting to Monday morning"*

---

## 🔒 Security & Privacy
- **Local LLM**: Your natural language inputs are processed locally via Ollama.
- **Sanitization**: Input is truncated and filtered for malicious patterns before reaching the AI.
- **OAuth2**: Uses official Google OAuth2 flows for secure calendar access.
- **Log Rotation**: Tracks system state in `scheduler.log` (make sure to keep this file local).

---

## 📝 License
MIT
