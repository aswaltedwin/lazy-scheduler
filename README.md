# 🐢 LazyScheduler [Phase 1]

LazyScheduler is a **local-first, preference-aware AI scheduling engine** that bridges the gap between natural language intent and structured calendar management. This version (Phase 1) focuses on core engine stability, dual-engine parsing, and local intelligence.

---

## 🏗️ Core Innovations

### 🧠 Dual-Engine Intent Parsing
LazyScheduler doesn't just rely on probabilistic AI. It employs a **hybrid architecture**:
1.  **Rule-Based Engine**: A high-speed, deterministic regex layer that captures common commands (e.g., "List today", "Delete event") with zero latency and 100% reliability—even if your LLM is offline.
2.  **LLM Engine (Ollama)**: For complex, contextual, or conversational requests (e.g., *"Actually, make that meeting an hour long and invite edwin@example.com"*). It understands correction modes and intent refinements.

### 🧙 Elite Magic Fix
Unlike standard calendar apps that simply warn you about conflicts, LazyScheduler attempts to **solve** them. 
-   **Cost-Based Optimization**: If a high-priority event (like a "Sync Call") conflicts with a low-priority one (like "Gym" or "Lunch"), the **Magic Fix** engine calculates the "disruption cost" of moving the flexible event.
-   **Atomic Transactions**: It proposes a chain of moves (e.g., shifting Lunch down by 30 mins) to clear your path. You approve the strategy before any changes are committed.

### 🔓 Precision Availability Detection
Finding time isn't just about finding empty space; it's about finding **useful** space.
-   Scans your schedule for contiguous "gaps".
-   Filters by configurable "Working Hours".
-   Supports duration-aware searching (e.g., *"Find a 45-minute gap today"*).

---

## ⚖️ Honesty & Current Limitations

To provide an elite experience, we must be transparent about the current state of the engine:

*   **Setup Friction**: Because this is a professional-grade tool, setting up the **Google Cloud Console Credentials** is a required hurdle. It requires creating an OAuth2 Client ID, which may take 5–10 minutes for first-time users.
*   **Ollama Dependency**: The quality of natural language parsing is directly tied to your local hardware's ability to run `qwen2.5:7b` (or similar). Latency on older machines can range from 1 to 5 seconds per request.
*   **Privacy vs. Polish**: We prioritize local-first (no data goes to OpenAI/Anthropic). This means the "intelligence" is limited by the parameters of your local model, which may occasionally hallucinate ISO-8601 strings in very complex nested sentences.
*   **Deterministic Fallback**: The regex engine is powerful but strict. If you use highly unconventional phrasing (e.g., *"Yeet the meeting at 2"*), it will default to the slower LLM engine.

---

## 🛠️ Stack & Security

-   **Language**: Python 3.10+
-   **Intelligence Layer**: Ollama (Local LLM) + Pydantic (Schema Validation)
-   **Interface**: Rich (Structured Terminal UI)
-   **Auth**: Google OAuth2 (Strictly scoped to `calendar` only)
-   **Data Integrity**: 
    -   **Input Sanitization**: Basic prompt-injection prevention layers.
    -   **Context Awareness**: Remembers only the *last* proposed action to enable immediate corrections.

---

## 🚀 Setup Guide

### 1. External Requirements
1.  **Ollama**: Install from [ollama.com](https://ollama.com/).
2.  **Model**: Run `ollama pull qwen2.5:7b` (default).
3.  **Google API**: 
    -   Go to [Google Cloud Console](https://console.cloud.google.com/).
    -   Enable the **Google Calendar API**.
    -   Create **OAuth 2.0 Client IDs** (Desktop App).
    -   Download the JSON and save it as `credentials.json` in this directory.

### 2. Python Environment
```bash
pip install ollama google-api-python-client google-auth-oauthlib python-dateutil pydantic rich
```

### 3. Configuration
On the first run, the app generates a `config.json`. You can tune:
-   `working_start` / `working_end`: Boundaries for free-slot detection.
-   `cost_weights`: Tune how "expensive" it is to move events in the Magic Fix engine.

---

## 💻 Example Commands

-   **Create**: *"Team sync at 3 PM for 45 mins"*
-   **Correct**: *"Actually, add Sarah to that meeting"* (Immediate context memory)
-   **Free Time**: *"Find me a 1 hour gap tomorrow"*
-   **Batch Ops**: *"Cancel all meetings for tomorrow"* (Requires confirmation)
-   **Check**: *"What do I have on Friday?"*

---

## 📜 License
MIT
