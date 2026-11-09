import os
import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dateutil import parser, tz

from config import CONFIG, PROFILE, STATE, save_state
from integrations.calendar_service import (
    get_calendar_service,
    list_events,
    create_event,
    delete_event
)
from integrations.llm_service import llm_service
from parsing.rule_parser import RuleBasedParser, Validator
from services.scoring import ScoringEngine, PriorityScorer
from core.optimization_engine import OptimizationEngine

app = FastAPI(title="LazyScheduler API", version="2.0.0")

class CommandRequest(BaseModel):
    command: str

class ProposalApplyRequest(BaseModel):
    targets: List[Dict[str, Any]]

@app.get("/api/health")
def health_check():
    return {"status": "ok", "timestamp": datetime.datetime.now().isoformat()}

@app.get("/api/events")
def get_today_events(date_str: Optional[str] = None):
    """Fetch calendar events for the selected day or today."""
    try:
        service = get_calendar_service()
        local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
        target_date = parser.parse(date_str).astimezone(local_tz) if date_str else datetime.datetime.now(local_tz)
        
        start_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end_day = target_date.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        
        events = list_events(service, start_day, end_day)
        health = ScoringEngine.assess_workload_health(events, PROFILE)
        
        return {
            "date": target_date.strftime("%Y-%m-%d"),
            "events": events,
            "workload_health": health
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/schedule")
def schedule_natural_language(req: CommandRequest):
    """Processes natural language scheduling commands, with conflict detection & OR-Tools resolution."""
    try:
        service = get_calendar_service()
        user_input = req.command.strip()
        if not user_input:
            raise HTTPException(status_code=400, detail="Empty command")

        # Check if user entered a multi-event brain-dump (e.g. contains 'and', 'then', commas)
        if any(sep in user_input.lower() for sep in [', then', ' and then ', ' at ', ' after ']):
            batch = llm_service.extract_multi_tasks(user_input)
            if len(batch) > 1:
                batch_created = []
                for item in batch:
                    title = item.get('title', 'Task')
                    time_s = item.get('time_str', '10am')
                    dur = item.get('duration_mins', 60)
                    dep = item.get('depends_on')
                    sub_parsed = RuleBasedParser.parse_input(f"{title} at {time_s}")
                    s_dt = parser.parse(sub_parsed.start)
                    e_dt = s_dt + datetime.timedelta(minutes=dur)
                    body = {
                        'summary': title,
                        'start': {'dateTime': s_dt.isoformat(), 'timeZone': CONFIG.timezone},
                        'end': {'dateTime': e_dt.isoformat(), 'timeZone': CONFIG.timezone}
                    }
                    create_event(service, body)
                    batch_created.append(body)
                return {"action": "batch_created", "events": batch_created}

        # 1. Groq LLM Intent Extraction with Rule fallback
        intelligence = llm_service.understand_intent(user_input)
        if not intelligence:
            task = RuleBasedParser.parse_input(user_input)
        else:
            task = RuleBasedParser.parse_input(f"{intelligence.get('title')} at {intelligence.get('time_str')}")
            task.action = intelligence.get('action', 'create')
            task.search_query = intelligence.get('search_query') or task.title
            task.energy_cost = intelligence.get('energy_cost', 3)
            task.intelligence = intelligence

            duration = intelligence.get('duration_mins', 60)
            start_dt = parser.parse(task.start)
            local_tz = tz.gettz(CONFIG.timezone) or tz.tzlocal()
            now = datetime.datetime.now(local_tz)
            if start_dt < now and task.action == "create":
                start_dt = start_dt + datetime.timedelta(days=1)
                task.start = start_dt.isoformat()

            task.end = (start_dt + datetime.timedelta(minutes=duration)).isoformat()
            until = intelligence.get('repeat_until')
            if until:
                task.recurrence = [f"RRULE:FREQ=DAILY;UNTIL={until}T235959Z"]

        task.momentum_score = ScoringEngine.calculate_momentum_score(task, PROFILE)

        # Handle simple actions
        if task.action == "delete":
            search_start = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
            search_end = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
            evts = list_events(service, search_start, search_end)
            found = [e for e in evts if task.search_query.lower() in e.get('summary', '').lower()]
            if found:
                delete_event(service, found[0]['id'])
                return {"action": "delete", "status": "deleted", "event": found[0]}
            return {"action": "delete", "status": "not_found"}

        if not Validator.validate_event(task):
            return {"action": "error", "message": "Could not understand scheduling details. Please specify a time."}

        # Check for conflicts
        conflicts = list_events(service, task.start, task.end)
        if not conflicts:
            # If large task decomposed
            if intelligence and intelligence.get('needs_decomposition'):
                sub_tasks = intelligence.get('sub_tasks', [])
                created = []
                curr_start = parser.parse(task.start)
                dur = max(20, int(60 / len(sub_tasks)))
                for sub in sub_tasks:
                    curr_end = curr_start + datetime.timedelta(minutes=dur)
                    body = {
                        'summary': f"{task.title}: {sub}",
                        'start': {'dateTime': curr_start.isoformat(), 'timeZone': CONFIG.timezone},
                        'end': {'dateTime': curr_end.isoformat(), 'timeZone': CONFIG.timezone},
                    }
                    create_event(service, body)
                    created.append(body)
                    curr_start = curr_end
                return {"action": "created_decomposed", "events": created}

            body = {
                'summary': task.title,
                'start': {'dateTime': task.start, 'timeZone': CONFIG.timezone},
                'end': {'dateTime': task.end, 'timeZone': CONFIG.timezone},
            }
            if task.recurrence:
                body['recurrence'] = task.recurrence
            create_event(service, body)
            return {"action": "created", "event": body}

        # Conflict detected -> run real Google OR-Tools CP-SAT solver
        proposals = OptimizationEngine.get_magic_fix_proposals(service, task, conflicts, conflicts)
        return {
            "action": "conflict",
            "conflicts": conflicts,
            "proposal": proposals[0] if proposals else None
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/emergency-reshuffle")
def trigger_emergency_reshuffle():
    """1-Click Emergency Reshuffle: Fixes and realigns all remaining tasks for today."""
    try:
        service = get_calendar_service()
        res = OptimizationEngine.emergency_reshuffle_remaining_day(service)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/apply-proposal")
def apply_proposal(req: ProposalApplyRequest):
    """Applies the OR-Tools optimized schedule proposal directly to Google Calendar."""
    try:
        service = get_calendar_service()
        applied = []
        for target in req.targets:
            body = {
                'summary': target['summary'],
                'start': {'dateTime': target['new_start'], 'timeZone': CONFIG.timezone},
                'end': {'dateTime': target['new_end'], 'timeZone': CONFIG.timezone}
            }
            if target['id'] == 'new_event':
                create_event(service, body)
            else:
                from integrations.calendar_service import update_event
                update_event(service, target['id'], body)
            applied.append(body)
        return {"status": "applied", "events": applied}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/voice-schedule")
async def voice_schedule(audio: UploadFile = File(...)):
    """Transcribes user voice input with Groq Whisper and schedules the intent."""
    temp_path = f"scratch_voice_{audio.filename}"
    try:
        with open(temp_path, "wb") as f:
            f.write(await audio.read())
        transcription = llm_service.transcribe_audio(temp_path)
        if not transcription:
            raise HTTPException(status_code=400, detail="Could not transcribe audio")
        # Run through regular pipeline
        return schedule_natural_language(CommandRequest(command=transcription))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    """Serves the modern, sleek LazyScheduler Web Dashboard."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>🐢 LazyScheduler Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Inter', sans-serif; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">
  <div class="max-w-5xl mx-auto px-4 py-8">
    <!-- Header -->
    <header class="flex items-center justify-between border-b border-slate-800 pb-6 mb-8">
      <div>
        <div class="flex items-center space-x-3">
          <span class="text-3xl">🐢</span>
          <h1 class="text-2xl font-bold bg-gradient-to-r from-blue-400 via-indigo-400 to-purple-400 bg-clip-text text-transparent">LazyScheduler</h1>
        </div>
        <p class="text-xs text-slate-400 mt-1">Constraint-Optimized • Local-First • Predictive Calendar</p>
      </div>
      <div id="healthBadge" class="px-3 py-1.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
        System Active
      </div>
    </header>

    <!-- Command Input Box -->
    <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 mb-8 shadow-xl">
      <div class="flex items-center justify-between mb-2">
        <label class="block text-sm font-medium text-slate-300">Schedule with Natural Language</label>
        <button onclick="emergencyReshuffle()" class="px-3 py-1 text-xs font-semibold rounded-lg bg-rose-500/20 text-rose-400 border border-rose-500/30 hover:bg-rose-500/30 transition flex items-center gap-1.5">
          <span>🚨 Emergency Reshuffle</span>
        </button>
      </div>
      <div class="flex gap-3">
        <input id="commandInput" type="text" placeholder="e.g. Gym tomorrow at 8am for 45 mins, or Deep work Thursday at 2pm"
          class="flex-1 bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-sm focus:outline-none focus:border-indigo-500 transition">
        <button id="sendBtn" onclick="sendScheduleCommand()" class="bg-indigo-600 hover:bg-indigo-500 text-white font-medium px-6 py-3 rounded-xl transition flex items-center gap-2">
          <span>Schedule</span>
        </button>
      </div>
      <div id="statusAlert" class="mt-4 hidden p-3 rounded-xl text-sm"></div>
    </section>

    <!-- Conflict Resolution Modal / Card -->
    <section id="conflictSection" class="hidden bg-amber-950/30 border border-amber-600/40 rounded-2xl p-6 mb-8">
      <div class="flex items-center gap-2 text-amber-400 font-semibold mb-2">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
        <span>Scheduling Conflict Detected! Magic Fix Ready (OR-Tools CP-SAT)</span>
      </div>
      <p class="text-sm text-slate-300 mb-4">Our mathematical solver computed the optimal minimal-friction shift below:</p>
      <div id="proposalList" class="space-y-2 mb-4"></div>
      <div class="flex gap-3">
        <button onclick="applyProposal()" class="bg-amber-600 hover:bg-amber-500 text-white text-sm font-semibold px-4 py-2 rounded-lg transition">Apply Magic Fix</button>
        <button onclick="dismissConflict()" class="bg-slate-800 hover:bg-slate-700 text-slate-300 text-sm font-semibold px-4 py-2 rounded-lg transition">Dismiss</button>
      </div>
    </section>

    <!-- Agenda View -->
    <section class="grid grid-cols-1 md:grid-cols-3 gap-6">
      <div class="md:col-span-2 bg-slate-900 border border-slate-800 rounded-2xl p-6">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-lg font-semibold text-slate-200">Today's Schedule</h2>
          <button onclick="loadEvents()" class="text-xs text-indigo-400 hover:text-indigo-300 transition">↻ Refresh</button>
        </div>
        <div id="eventsContainer" class="space-y-3">
          <div class="text-sm text-slate-500">Loading events...</div>
        </div>
      </div>

      <!-- Health & Energy Summary -->
      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6">
        <h2 class="text-lg font-semibold text-slate-200 mb-4">Day Health & Energy</h2>
        <div class="space-y-4">
          <div>
            <div class="flex justify-between text-xs text-slate-400 mb-1">
              <span>Failure / Burnout Risk</span>
              <span id="burnoutVal">0%</span>
            </div>
            <div class="w-full bg-slate-800 h-2 rounded-full overflow-hidden">
              <div id="burnoutBar" class="bg-indigo-500 h-full w-0 transition-all duration-500"></div>
            </div>
          </div>
          <div class="p-3 bg-slate-950/60 rounded-xl border border-slate-800/80">
            <span class="text-xs text-slate-400 block mb-1">Energy Category</span>
            <span id="statusCategory" class="text-sm font-semibold text-emerald-400">HEALTHY</span>
          </div>
        </div>
      </div>
    </section>
  </div>

  <script>
    let currentProposal = null;

    async function loadEvents() {
      try {
        const res = await fetch('/api/events');
        const data = await res.json();
        const container = document.getElementById('eventsContainer');
        container.innerHTML = '';

        if (!data.events || data.events.length === 0) {
          container.innerHTML = '<div class="text-sm text-slate-500">No events found for today. Free day!</div>';
        } else {
          data.events.forEach(e => {
            const start = e.start.dateTime || e.start.date;
            const end = e.end.dateTime || e.end.date;
            const dStart = new Date(start);
            const dEnd = new Date(end);
            const timeStr = dStart.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ' - ' + dEnd.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            
            const div = document.createElement('div');
            div.className = 'p-3 rounded-xl bg-slate-950/70 border border-slate-800 flex justify-between items-center';
            div.innerHTML = `
              <div>
                <p class="font-medium text-sm text-slate-100">${e.summary || 'Busy'}</p>
                <p class="text-xs text-slate-400">${timeStr}</p>
              </div>
              <span class="text-xs px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">Synced</span>
            `;
            container.appendChild(div);
          });
        }

        if (data.workload_health) {
          const prob = Math.round((data.workload_health.failure_probability || 0) * 100);
          document.getElementById('burnoutVal').innerText = prob + '%';
          const bar = document.getElementById('burnoutBar');
          bar.style.width = prob + '%';
          if (prob > 70) bar.className = 'bg-rose-500 h-full transition-all duration-500';
          else if (prob > 40) bar.className = 'bg-amber-500 h-full transition-all duration-500';
          else bar.className = 'bg-indigo-500 h-full transition-all duration-500';

          const cat = document.getElementById('statusCategory');
          cat.innerText = (data.workload_health.status || 'HEALTHY').toUpperCase();
        }
      } catch (err) {
        console.error(err);
      }
    }

    async function sendScheduleCommand() {
      const input = document.getElementById('commandInput');
      const text = input.value.trim();
      if (!text) return;

      const btn = document.getElementById('sendBtn');
      btn.disabled = true;
      btn.innerText = 'Processing...';

      const alert = document.getElementById('statusAlert');
      alert.className = 'mt-4 hidden p-3 rounded-xl text-sm';

      try {
        const res = await fetch('/api/schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: text })
        });
        const data = await res.json();

        if (data.action === 'created' || data.action === 'created_decomposed' || data.action === 'batch_created') {
          alert.className = 'mt-4 p-3 rounded-xl text-sm bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
          const count = data.events ? data.events.length : 1;
          alert.innerText = `✓ Successfully scheduled ${count} event(s) on Google Calendar!`;
          alert.classList.remove('hidden');
          input.value = '';
          loadEvents();
        } else if (data.action === 'conflict') {
          currentProposal = data.proposal;
          showConflictSection(data.proposal);
        } else if (data.action === 'delete') {
          alert.className = 'mt-4 p-3 rounded-xl text-sm bg-amber-500/10 text-amber-400 border border-amber-500/20';
          alert.innerText = data.status === 'deleted' ? '✓ Event removed successfully.' : 'No matching event found.';
          alert.classList.remove('hidden');
          loadEvents();
        } else {
          alert.className = 'mt-4 p-3 rounded-xl text-sm bg-rose-500/10 text-rose-400 border border-rose-500/20';
          alert.innerText = data.message || 'Operation failed.';
          alert.classList.remove('hidden');
        }
      } catch (err) {
        alert.className = 'mt-4 p-3 rounded-xl text-sm bg-rose-500/10 text-rose-400 border border-rose-500/20';
        alert.innerText = 'Error connecting to backend.';
        alert.classList.remove('hidden');
      } finally {
        btn.disabled = false;
        btn.innerText = 'Schedule';
      }
    }

    async function emergencyReshuffle() {
      const alert = document.getElementById('statusAlert');
      alert.className = 'mt-4 p-3 rounded-xl text-sm bg-indigo-500/10 text-indigo-400 border border-indigo-500/20';
      alert.innerText = 'Calculating optimal realignment for remaining events...';
      alert.classList.remove('hidden');

      try {
        const res = await fetch('/api/emergency-reshuffle', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success' && data.targets && data.targets.length > 0) {
          currentProposal = data;
          showConflictSection(data);
          alert.className = 'mt-4 p-3 rounded-xl text-sm bg-amber-500/10 text-amber-400 border border-amber-500/20';
          alert.innerText = `Emergency Reshuffle ready: ${data.targets.length} events adjusted to prevent overlaps. Review below.`;
        } else {
          alert.className = 'mt-4 p-3 rounded-xl text-sm bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
          alert.innerText = 'All remaining events are already non-overlapping!';
        }
      } catch (err) {
        alert.className = 'mt-4 p-3 rounded-xl text-sm bg-rose-500/10 text-rose-400 border border-rose-500/20';
        alert.innerText = 'Error triggering emergency reshuffle.';
      }
    }

    function showConflictSection(proposal) {
      const section = document.getElementById('conflictSection');
      const list = document.getElementById('proposalList');
      list.innerHTML = '';
      if (!proposal || !proposal.targets) return;

      proposal.targets.forEach(t => {
        const s = new Date(t.new_start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const e = new Date(t.new_end).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const div = document.createElement('div');
        div.className = 'p-2 bg-slate-900/80 rounded border border-slate-700 text-xs flex justify-between items-center';
        div.innerHTML = `<span><strong>${t.summary}</strong>: ${s} - ${e}</span><span class="text-amber-400 font-mono">${t.breakdown ? t.breakdown.reason : ''}</span>`;
        list.appendChild(div);
      });
      section.classList.remove('hidden');
    }

    async function applyProposal() {
      if (!currentProposal) return;
      try {
        const res = await fetch('/api/apply-proposal', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ targets: currentProposal.targets })
        });
        const data = await res.json();
        dismissConflict();
        loadEvents();
        const alert = document.getElementById('statusAlert');
        alert.className = 'mt-4 p-3 rounded-xl text-sm bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
        alert.innerText = '✓ Realignment applied using CP-SAT optimization.';
        alert.classList.remove('hidden');
      } catch (err) {
        alert('Failed to apply proposal.');
      }
    }

    function dismissConflict() {
      document.getElementById('conflictSection').classList.add('hidden');
      currentProposal = null;
    }

    loadEvents();
  </script>
</body>
</html>
    """

from integrations.telegram_service import telegram_service

@app.post("/api/telegram/webhook")
async def telegram_webhook(payload: Dict[str, Any]):
    """Handles incoming messages from Telegram bot webhook."""
    try:
        msg = payload.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = msg.get("chat", {}).get("id")
        
        if not text or not chat_id:
            return {"status": "ignored"}
            
        if text.startswith("/start"):
            telegram_service.send_message(
                "🐢 *Welcome to LazyScheduler Bot!*\n\n"
                "Send me any scheduling command in plain English:\n"
                "• `Gym tomorrow at 8am for 45 mins`\n"
                "• `Deep work Thursday at 2pm`\n"
                "• `What do I have today?`",
                chat_id=str(chat_id)
            )
            return {"status": "ok"}
            
        res = schedule_natural_language(CommandRequest(command=text))
        if res.get("action") == "created":
            evt = res.get("event", {})
            msg_text = f"✅ *Scheduled:* {evt.get('summary')}\n🕒 {evt.get('start', {}).get('dateTime')}"
        elif res.get("action") == "conflict":
            msg_text = "⚠️ *Conflict detected!* Magic Fix realignment has been computed on your LazyScheduler dashboard."
        elif res.get("action") == "delete":
            msg_text = "🗑️ Event removed from calendar."
        else:
            msg_text = f"ℹ️ {res.get('message', 'Processed request.')}"
            
        telegram_service.send_message(msg_text, chat_id=str(chat_id))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

