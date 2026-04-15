# 🐢 LazyScheduler

**LazyScheduler** is a smart, minimal, and premium AI-powered calendar assistant. It transforms your natural language sentences into perfectly structured Google Calendar events without the friction of manual data entry.

Designed for the "Productive Procrastinator," LazyScheduler handles the thinking, the conflict checking, and the organizing so you don't have to.

---

## ✨ Key Features

- **🧠 Deep Intelligence**: Powered by Ollama (`qwen2.5:7b`), it understands natural intents like *"Move my 4 PM sync to 5 PM"* or *"Find a 30 min slot tomorrow morning."*
- **🛡️ Conflict Aware**: Automatically checks your availability using the Google Calendar Free/Busy API. If you're double-booked, it suggests the next best time.
- **💎 Premium CLI UI**: A beautiful, terminal-based experience using the **Rich** library, featuring thinking spinners, color-coded event cards, and professional tables.
- **🔁 Recurring Support**: Full support for recurring events (e.g., *"Weekly sync every Monday at 11 AM"*).
- **📹 Video-Ready**: Automatically generates Google Meet links for online meetings.
- **🔄 Intent Correction**: Not happy with a proposal? Just tell the AI what to fix (e.g., *"Actually, make it an hour long"*), and it will update the event contextually.

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
Ensure you have the default model pulled:
```bash
ollama pull qwen2.5:7b
```

### 4. Configuration
You can customize your experience in `config.json`:
- `timezone`: Your local timezone (default: `Asia/Kolkata`).
- `working_hours`: For finding free slots (default: `09:00 - 19:00`).
- `model`: Change the LLM used for parsing.

---

## 🛠️ Usage

Simply run:
```bash
python main.py
```


### Example Commands:
- *"Schedule a team sync with rahul@gmail.com tomorrow at 3 PM"*
- *"Show my schedule for this Friday"*
- *"Cancel the doctor's appointment"*
- *"Find a 45 min free slot next Tuesday"*

---

## 🔒 Security Note
**Never** commit your `credentials.json` or `token.json` files. LazyScheduler includes a `.gitignore` to prevent this.

---

## 📝 License
MIT
