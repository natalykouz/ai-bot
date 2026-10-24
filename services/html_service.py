"""
Сервис для форматирования текста в HTML через OpenAI API.
"""

import logging

from openai import AsyncOpenAI

from config import settings, HTML_FORMAT_PROMPT, MAX_CHARS_HTML

logger = logging.getLogger(__name__)

# Асинхронный клиент OpenAI — создаётся один раз при импорте модуля
_client = AsyncOpenAI(api_key=settings.openai_api_key)

# Ограничение длины ответа модели для форматирования HTML
MAX_TOKENS_HTML = 8000


async def analyze_html(text: str) -> tuple[str, bool]:
    """
    Отправляет текст в OpenAI для форматирования в HTML и возвращает результат.

    :param text: Исходный текст для форматирования.
    :return: Кортеж (HTML-фрагмент, был ли ответ обрезан по лимиту токенов).
    """
    # Проверяем размер текста перед отправкой запроса
    if len(text) > MAX_CHARS_HTML:
        return "⚠️ Текст слишком большой. Максимальный размер — 20 000 символов.", False

    response = await _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                # Системный промпт загружается из файла prompts/html_format.txt
                "content": HTML_FORMAT_PROMPT,
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        temperature=0.2,  # низкая температура для стабильного форматирования
        max_tokens=MAX_TOKENS_HTML,
    )

    choice = response.choices[0]
    finish_reason = choice.finish_reason
    usage = response.usage
    logger.info(
        "analyze_html: finish_reason=%s prompt_tokens=%s completion_tokens=%s",
        finish_reason,
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )

    truncated = finish_reason == "length"
    return choice.message.content or "Модель не вернула ответ.", truncated
