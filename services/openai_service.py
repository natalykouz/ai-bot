"""
Сервис для взаимодействия с OpenAI API.
Отправляет текст договора и возвращает результат анализа.
"""

from openai import AsyncOpenAI

from config import settings

# Асинхронный клиент OpenAI — создаётся один раз при импорте модуля
_client = AsyncOpenAI(api_key=settings.openai_api_key)


async def analyze_contract(contract_text: str) -> str:
    """
    Отправляет текст договора в OpenAI и возвращает анализ рисков.

    :param contract_text: Полный текст договора.
    :return: Текстовый ответ модели.
    """
    # TODO: добавить обработку слишком длинных документов (chunking / summarization)
    # TODO: настроить модель и параметры через config при необходимости

    response = await _client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": settings.system_prompt,
            },
            {
                "role": "user",
                "content": contract_text,
            },
        ],
        temperature=0.2,  # низкая температура для более точных юридических ответов
    )

    # Извлекаем текст первого варианта ответа
    return response.choices[0].message.content or "Модель не вернула ответ."
