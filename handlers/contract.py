"""
Хендлер для проверки договоров.
Принимает документы (.pdf, .docx) или текст и отправляет их на анализ.
"""

from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

# Роутер для группировки хендлеров договоров
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Приветственное сообщение при команде /start."""
    await message.answer(
        "Привет! Я помогаю анализировать договоры.\n\n"
        "Отправь мне файл договора в формате <b>.pdf</b> или <b>.docx</b>, "
        "либо просто вставь текст договора — и я проверю его на риски."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Справка по командам бота."""
    await message.answer(
        "<b>Команды бота:</b>\n"
        "/start — начало работы\n"
        "/help — справка\n\n"
        "Поддерживаемые форматы файлов: PDF, DOCX.\n"
        "Также можно отправить текст договора напрямую."
    )


@router.message()
async def handle_document_or_text(message: Message) -> None:
    """
    Заглушка: принимает любое сообщение (документ или текст)
    и сообщает, что функция проверки договора будет реализована позже.
    """
    # TODO: извлечь текст из message.document (PDF/DOCX) или message.text
    # TODO: передать текст в services.openai_service.analyze_contract()
    # TODO: вернуть пользователю результат анализа
    await message.answer(
        "Функция анализа договора пока в разработке. "
        "Скоро здесь появится полноценная проверка!"
    )
