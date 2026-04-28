"""
Загрузка конфигурации из .env-файла через python-dotenv.
"""

from dataclasses import dataclass

from dotenv import load_dotenv
import os

# Загружаем переменные окружения из файла .env
load_dotenv()


@dataclass
class Settings:
    bot_token: str
    openai_api_key: str
    system_prompt: str


def _load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    system_prompt = os.getenv("SYSTEM_PROMPT", "Ты — помощник по проверке договоров.")

    if not bot_token:
        raise ValueError("Переменная окружения BOT_TOKEN не задана")
    if not openai_api_key:
        raise ValueError("Переменная окружения OPENAI_API_KEY не задана")

    return Settings(
        bot_token=bot_token,
        openai_api_key=openai_api_key,
        system_prompt=system_prompt,
    )


# Единственный экземпляр настроек для всего приложения
settings = _load_settings()
