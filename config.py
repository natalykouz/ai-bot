"""
Загрузка конфигурации из .env-файла через python-dotenv.
Системные промпты читаются из файлов в папке prompts/.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env
load_dotenv()


def load_prompt(filename: str) -> str:
    """Загружает системный промпт из файла в папке prompts/.
    Если файл не найден — возвращает пустую строку."""
    path = os.path.join(os.path.dirname(__file__), "prompts", filename)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


# Максимальный размер входящего текста в символах
MAX_CHARS_CONTRACT = 20000
MAX_CHARS_HTML = 20000

# Промпты загружаются из файлов один раз при старте приложения
CONTRACT_CHECK_PROMPT = load_prompt("contract_check.txt")
HTML_FORMAT_PROMPT = load_prompt("html_format.txt")


@dataclass
class Settings:
    bot_token: str
    openai_api_key: str
    openai_model: str


def _load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    # Модель OpenAI — берётся из .env, по умолчанию gpt-4o-mini
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not bot_token:
        raise ValueError("Переменная окружения BOT_TOKEN не задана")
    if not openai_api_key:
        raise ValueError("Переменная окружения OPENAI_API_KEY не задана")

    return Settings(
        bot_token=bot_token,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
    )


# Единственный экземпляр настроек для всего приложения
settings = _load_settings()
