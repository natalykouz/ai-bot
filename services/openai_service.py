"""
Сервис для взаимодействия с OpenAI API.
Отправляет текст договора и возвращает результат анализа.
"""

import logging

from openai import AsyncOpenAI

import config
from config import settings, CONTRACT_CHECK_PROMPT, MAX_CHARS_CONTRACT

logger = logging.getLogger(__name__)

# Асинхронный клиент OpenAI — создаётся один раз при импорте модуля
_client = AsyncOpenAI(api_key=settings.openai_api_key)

# Ограничение длины ответа модели для анализа договора
MAX_TOKENS_CONTRACT = 4000


async def analyze_contract(text: str, model: str | None = None) -> tuple[str, bool]:
    """
    Отправляет текст договора в OpenAI и возвращает анализ рисков.

    :param text: Полный текст договора.
    :param model: Модель OpenAI для анализа. Если не передана — берётся из настроек.
    :return: Кортеж (текст ответа модели, был ли ответ обрезан по лимиту токенов).
    """
    # Проверяем размер текста перед отправкой запроса
    if len(text) > MAX_CHARS_CONTRACT:
        return "⚠️ Документ слишком большой. Максимальный размер — 20 000 символов. Пожалуйста, сократите текст.", False

    # Используем переданную модель или дефолтную из настроек
    selected_model = model if model else config.settings.openai_model

    response = await _client.chat.completions.create(
        model=selected_model,
        messages=[
            {
                "role": "system",
                # Системный промпт загружается из файла prompts/contract_check.txt
                "content": CONTRACT_CHECK_PROMPT,
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        temperature=0.2,  # низкая температура для более точных юридических ответов
        max_tokens=MAX_TOKENS_CONTRACT,
    )

    choice = response.choices[0]
    finish_reason = choice.finish_reason
    usage = response.usage
    logger.info(
        "analyze_contract: model=%s finish_reason=%s prompt_tokens=%s completion_tokens=%s",
        selected_model,
        finish_reason,
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )

    truncated = finish_reason == "length"
    return choice.message.content or "Модель не вернула ответ.", truncated
