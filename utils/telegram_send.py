"""
Хелпер для надёжной отправки длинных текстовых ответов в Telegram.
Telegram ограничивает длину одного сообщения 4096 символами —
split_text режет текст на части с запасом, стараясь не рвать слово.
"""

from typing import List, Optional

# Запас от лимита Telegram (4096), чтобы не упереться в него на граничных случаях
MAX_MESSAGE_LENGTH = 4000

TRUNCATED_WARNING = "⚠️ Ответ получился слишком длинным и был обрезан."


def split_text(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """
    Разбивает текст на части не длиннее max_len символов.
    Режет по границе абзаца (\\n\\n), затем по переносу строки,
    затем по пробелу — и только если ничего из этого не нашлось, жёстко по max_len.
    """
    chunks: List[str] = []
    remaining = text

    while len(remaining) > max_len:
        window = remaining[:max_len]

        cut = window.rfind("\n\n")
        if cut == -1:
            cut = window.rfind("\n")
        if cut == -1:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_len

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks or [""]


async def send_long_text(
    message,
    text: str,
    parse_mode: Optional[str] = None,
    truncated: bool = False,
    max_len: int = MAX_MESSAGE_LENGTH,
) -> None:
    """
    Отправляет текст в Telegram, при необходимости разбивая его на несколько
    сообщений не длиннее max_len символов (режет по абзацам/строкам, не посреди слова).

    :param message: объект с методом .answer(text, parse_mode=...) — Message или CallbackQuery.message
    :param text: текст для отправки
    :param parse_mode: режим разметки; None — отправить как обычный текст без парсинга разметки
    :param truncated: если True — после текста добавляется предупреждение об обрезанном ответе
    :param max_len: максимальная длина одной части
    """
    full_text = text
    if truncated:
        full_text = f"{full_text}\n\n{TRUNCATED_WARNING}"

    for chunk in split_text(full_text, max_len):
        await message.answer(chunk, parse_mode=parse_mode)
