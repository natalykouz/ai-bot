"""
Сервис для взаимодействия с OpenAI API.
Отправляет текст договора и возвращает результат анализа.
"""

from openai import AsyncOpenAI

import config
from config import settings, CONTRACT_CHECK_PROMPT, MAX_CHARS_CONTRACT

# Асинхронный клиент OpenAI — создаётся один раз при импорте модуля
_client = AsyncOpenAI(api_key=settings.openai_api_key)


async def analyze_contract(text: str, model: str | None = None) -> str:
    """
    Отправляет текст договора в OpenAI и возвращает анализ рисков.

    :param text: Полный текст договора.
    :param model: Модель OpenAI для анализа. Если не передана — берётся из настроек.
    :return: Текстовый ответ модели.
    """
    # Проверяем размер текста перед отправкой запроса
    if len(text) > MAX_CHARS_CONTRACT:
        return "⚠️ Документ слишком большой. Максимальный размер — 20 000 символов. Пожалуйста, сократите текст."

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
        max_tokens=1000,  # ограничиваем длину ответа модели
    )

    # Извлекаем текст первого варианта ответа
    return response.choices[0].message.content or "Модель не вернула ответ."
