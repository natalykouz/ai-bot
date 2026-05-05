"""
Сервис для форматирования текста в HTML через OpenAI API.
"""

from openai import AsyncOpenAI

from config import settings, HTML_FORMAT_PROMPT, MAX_CHARS_HTML

# Асинхронный клиент OpenAI — создаётся один раз при импорте модуля
_client = AsyncOpenAI(api_key=settings.openai_api_key)


async def analyze_html(text: str) -> str:
    """
    Отправляет текст в OpenAI для форматирования в HTML и возвращает результат.

    :param text: Исходный текст для форматирования.
    :return: Текст в виде HTML-фрагмента.
    """
    # Проверяем размер текста перед отправкой запроса
    if len(text) > MAX_CHARS_HTML:
        return "⚠️ Текст слишком большой. Максимальный размер — 20 000 символов."

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
        max_tokens=2000,  # HTML может быть длиннее исходного текста
    )

    # Извлекаем текст первого варианта ответа
    return response.choices[0].message.content or "Модель не вернула ответ."
