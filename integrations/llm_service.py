from typing import Optional, List, Dict, Any
import os
import json
import datetime
from groq import Groq
from dotenv import load_dotenv
from utils.logger import logger

load_dotenv(override=True)

class LLMService:
    """
    The 'Voice' of the agent. 
    Now supports Task Decomposition (Level 2).
    """
    
    def __init__(self):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            logger.error("Missing GROQ_API_KEY in .env")
            self.client = None
        else:
            self.client = Groq(api_key=api_key)
        
        self.model = "llama-3.3-70b-versatile"

    def understand_intent(self, user_input: str, context: str = ""):
        """
        Extracts intent and detects if task needs decomposition.
        """
        if not self.client:
            return None

        now = datetime.datetime.now(datetime.timezone.utc)
        
        prompt = f"""
        You are 'lazy-scheduler', a strategic calendar partner.
        Reference Time: {now.strftime("%A, %b %d, %Y %I:%M %p")}
        
        Extract intent into JSON.
        
        SPECIAL INSTRUCTION: If the task is large/vague (e.g. "Study OS", "Project work"),
        set "needs_decomposition" to true and provide 3 small sub-tasks in "sub_tasks".
        
        Return JSON:
        - action: "create", "delete", "update", "find", "greet", "chat"
        - title: Name
        - time_str: Time string
        - duration_mins: Minutes (default 60)
        - energy_cost: 1-5 (1=chill, 5=brain-heavy)
        - needs_decomposition: boolean
        - sub_tasks: list of strings (if needs_decomposition is true)
        - search_range: {{"start": ISO, "end": ISO}}
        - reply: friendly response
        """
        
        try:
            chat_completion = self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt + f"\nUser: {user_input}"}],
                model=self.model,
                response_format={"type": "json_object"}
            )
            return json.loads(chat_completion.choices[0].message.content)
        except Exception as e:
            logger.error(f"Groq API Error: {e}")
            return None

    def format_response(self, brain_data: dict):
        """Standard natural language response."""
        if not self.client: return None
        try:
            chat_completion = self.client.chat.completions.create(
                messages=[{"role": "user", "content": f"Format this into a brief friendly sentence: {json.dumps(brain_data)}"}],
                model=self.model
            )
            return chat_completion.choices[0].message.content
        except Exception: return None

    def transcribe_audio(self, audio_file_path: str) -> Optional[str]:
        """
        Transcribes voice notes or speech using Groq's high-speed Whisper Large v3.
        """
        if not self.client:
            logger.error("Groq client not available for transcription")
            return None
        try:
            with open(audio_file_path, "rb") as file:
                transcription = self.client.audio.transcriptions.create(
                    file=(os.path.basename(audio_file_path), file.read()),
                    model="whisper-large-v3",
                    response_format="text"
                )
                return str(transcription).strip()
        except Exception as e:
            logger.error(f"Whisper Transcription Error: {e}")
            return None

    def extract_multi_tasks(self, user_input: str) -> List[Dict[str, Any]]:
        """
        Extracts multiple task intents from a single brain dump or multi-step command.
        Example: 'Gym at 7am, then sync with Alex at 11, and review budget at 4pm'
        """
        if not self.client:
            return []

        now = datetime.datetime.now(datetime.timezone.utc)
        prompt = (
            "You are lazy-scheduler, a strategic calendar partner.\n"
            f"Reference Time: {now.strftime('%A, %b %d, %Y %I:%M %p')}\n\n"
            "Analyze the user prompt. It may contain one or MULTIPLE scheduling requests.\n"
            "Extract every distinct task into an array of objects under 'tasks'.\n\n"
            "Each task object must have:\n"
            "- title: string\n"
            "- time_str: string\n"
            "- duration_mins: integer (default 60)\n"
            "- energy_cost: integer 1-5\n"
            "- depends_on: prerequisite task title if sequential relationship ('then', 'after', 'before') exists\n\n"
            "Return JSON: {\"tasks\": [...]}"
        )

        try:
            chat_completion = self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt + f"\nUser: {user_input}"}],
                model=self.model,
                response_format={"type": "json_object"}
            )
            data = json.loads(chat_completion.choices[0].message.content)
            return data.get("tasks", [])
        except Exception as e:
            logger.error(f"Multi-task Groq extraction error: {e}")
            return []

llm_service = LLMService()
