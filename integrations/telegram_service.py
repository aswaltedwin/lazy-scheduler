import os
import json
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any
from utils.logger import logger

class TelegramService:
    """
    Lightweight Telegram Bot service for LazyScheduler.
    Enables remote calendar scheduling, daily agenda retrieval, and Magic Fix via Telegram messages.
    """
    def __init__(self, bot_token: Optional[str] = None, default_chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.default_chat_id = default_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None

    def is_configured(self) -> bool:
        return bool(self.bot_token)

    def send_message(self, text: str, chat_id: Optional[str] = None) -> bool:
        target_chat = chat_id or self.default_chat_id
        if not self.base_url or not target_chat:
            return False
        
        url = f"{self.base_url}/sendMessage"
        payload = json.dumps({
            "chat_id": target_chat,
            "text": text,
            "parse_mode": "Markdown"
        }).encode("utf-8")
        
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status == 200
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

telegram_service = TelegramService()
