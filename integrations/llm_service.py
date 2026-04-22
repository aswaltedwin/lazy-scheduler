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

llm_service = LLMService()
