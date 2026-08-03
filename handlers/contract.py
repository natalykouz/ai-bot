"""
Хендлер для проверки договоров и форматирования HTML.
Использует FSM для отслеживания режима ожидания ввода от пользователя.
"""

import io
import os
from typing import Any, Awaitable, Callable, Dict, Union

from aiogram import BaseMiddleware, Bot, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, TelegramObject,
)

from services.openai_service import analyze_contract
from services.html_service import analyze_html
from services import auth_service

# Клавиатура главного меню
keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Проверка договоров")],
        [KeyboardButton(text="Форматирование HTML страниц")]
    ],
    resize_keyboard=True
)

# Роутер для группировки хендлеров
router = Router()


class UserStates(StatesGroup):
    """Состояния FSM: ожидание договора, текста для HTML-форматирования или нового промпта."""
    waiting_contract = State()
    waiting_html = State()
    waiting_new_prompt = State()   # ожидание нового текста промпта от администратора
    waiting_model_choice = State() # ожидание выбора модели перед анализом договора
    waiting_password = State()     # ожидание ввода пароля для доступа к боту


class AuthMiddleware(BaseMiddleware):
    """
    Закрывает доступ к любым хендлерам, пока пользователь не введёт пароль.
    Админы (см. services/auth_service.py) и уже допущенные пользователи проходят без проверки.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Union[Message, CallbackQuery],
        data: Dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id

        if auth_service.is_allowed(user_id):
            return await handler(event, data)

        state: FSMContext = data["state"]
        if await state.get_state() == UserStates.waiting_password.state:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("Нет доступа. Отправьте /start и введите пароль.", show_alert=True)
            return None

        await state.set_state(UserStates.waiting_password)
        await event.answer("🔒 Бот защищён паролем. Введите пароль для доступа:")
        return None


router.message.middleware(AuthMiddleware())
router.callback_query.middleware(AuthMiddleware())


def get_model_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура для выбора модели OpenAI перед анализом договора."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Быстрая (gpt-4o-mini)",
            callback_data="model_mini",
        )],
        [InlineKeyboardButton(
            text="Умная (gpt-4o)",
            callback_data="model_gpt4o",
        )],
    ])


ADMIN_INFO_TEXT = (
    "👑 Вы являетесь администратором бота.\n\n"
    "<b>Команды администратора:</b>\n\n"
    "Сменить пароль доступа к боту:\n"
    "<code>/setpassword новый_пароль</code>\n\n"
    "Скачать текущий промпт:\n"
    "<code>/getprompt contract</code>\n"
    "<code>/getprompt html</code>\n\n"
    "Загрузить новый промпт:\n"
    "<code>/setprompt contract</code>\n"
    "<code>/setprompt html</code>"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Сбрасывает состояние и отправляет главное меню с клавиатурой."""
    await state.clear()
    if auth_service.is_admin(message.from_user.id):
        await message.answer(ADMIN_INFO_TEXT)
    await message.answer("Выберите действие:", reply_markup=keyboard)


@router.message(UserStates.waiting_password)
async def handle_password(message: Message, state: FSMContext) -> None:
    """Проверяет введённый пароль и открывает доступ пользователю при совпадении."""
    if message.text and auth_service.check_password(message.text.strip()):
        auth_service.grant_access(message.from_user.id)
        await state.clear()
        await message.answer("✅ Доступ разрешён.", reply_markup=keyboard)
    else:
        await message.answer("❌ Неверный пароль. Попробуйте ещё раз.")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Справка по командам бота."""
    await message.answer(
        "<b>Команды бота:</b>\n"
        "/start — начало работы\n"
        "/help — справка\n"
        "/check — начать проверку договора\n\n"
        "Поддерживаемые форматы файлов: PDF, DOCX, TXT.\n"
        "Также можно отправить текст договора напрямую."
    )


@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext) -> None:
    """Переводит бота в режим ожидания договора и объясняет что будет проверено."""
    await state.set_state(UserStates.waiting_contract)
    await message.answer(
        "Отправьте текст договора или файл в формате .txt, .docx или .pdf. "
        "Бот проверит реквизиты сторон, наличие обязательных разделов и корректность НДС. "
        "На выходе — список замечаний с указанием что именно требует исправления."
    )


@router.message(F.text == "Проверка договоров")
async def btn_check(message: Message, state: FSMContext) -> None:
    """Обрабатывает нажатие кнопки 'Проверка договоров' — вызывает cmd_check."""
    await cmd_check(message, state)


@router.message(F.text == "Форматирование HTML страниц")
async def btn_html(message: Message, state: FSMContext) -> None:
    """Переводит бота в режим ожидания текста и объясняет как использовать результат."""
    await state.set_state(UserStates.waiting_html)
    await message.answer(
        "Отправьте текст анонса экскурсии — сырой или уже с HTML-разметкой. "
        "Бот приведёт его к корректной вёрстке. "
        "Полученный код вставьте в поле «Описание» на сайте в режиме «Источник», "
        "предварительно очистив содержимое поля."
    )


@router.message(F.document)
async def handle_document(message: Message, state: FSMContext, bot: Bot) -> None:
    """
    Обрабатывает полученный документ.
    Если активно состояние waiting_new_prompt — передаёт управление handle_new_prompt.
    Иначе обрабатывает как договор:
    - .txt  — читает как текст (UTF-8)
    - .docx — парсит через python-docx
    - .pdf  — парсит через pdfplumber
    - другой формат — сообщает об ошибке
    """
    # Если ждём новый промпт — передаём управление соответствующему хендлеру
    current_state = await state.get_state()
    if current_state == UserStates.waiting_new_prompt.state:
        await handle_new_prompt(message, state, bot)
        return

    document = message.document
    file_name = document.file_name or ""

    # Определяем расширение файла в нижнем регистре
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    if ext not in ("txt", "docx", "pdf"):
        await message.answer("Поддерживаются только .txt, .docx, .pdf")
        return

    # Скачиваем файл в оперативную память
    file = await message.bot.get_file(document.file_id)
    buf = io.BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    buf.seek(0)

    # Извлекаем текст в зависимости от расширения
    if ext == "txt":
        # Читаем байты и декодируем как UTF-8
        contract_text = buf.read().decode("utf-8", errors="replace")

    elif ext == "docx":
        # Парсим .docx через python-docx, объединяем все абзацы
        from docx import Document as DocxDocument
        doc = DocxDocument(buf)
        contract_text = "\n".join(para.text for para in doc.paragraphs)

    else:  # pdf
        # Парсим .pdf через pdfplumber, собираем текст со всех страниц
        import pdfplumber
        pages_text = []
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
        contract_text = "\n".join(pages_text)

    if not contract_text.strip():
        await message.answer("Не удалось извлечь текст из файла. Попробуйте другой файл.")
        return

    # Сохраняем текст договора и переходим к выбору модели
    await state.update_data(contract_text=contract_text)
    await state.set_state(UserStates.waiting_model_choice)
    await message.answer(
            "Выберите режим проверки:\n\n"
            "Быстрая (gpt-4o-mini) — дешевле, справляется с большинством договоров\n"
            "Умная (gpt-4o) — точнее, лучше для сложных или нестандартных документов",
            reply_markup=get_model_keyboard(),
        )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Сбрасывает текущее состояние FSM и отменяет любое ожидаемое действие."""
    await state.clear()
    await message.answer("Действие отменено.")


@router.message(Command("setpassword"))
async def cmd_setpassword(message: Message, bot: Bot) -> None:
    """
    Скрытая команда для смены пароля доступа к боту. Доступна только админам
    (см. ADMIN_IDS / data/auth.json). Не требует правки кода или .env.
    Пример: /setpassword новый_пароль
    """
    if not auth_service.is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /setpassword <новый пароль>")
        return

    auth_service.set_password(args[1].strip())

    # Удаляем сообщение с открытым текстом пароля из чата
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    await message.answer("✅ Пароль обновлён.")


@router.message(Command("getprompt"))
async def cmd_getprompt(message: Message) -> None:
    """
    Скрытая команда для получения текущего промпта в виде файла.
    Принимает аргумент: contract или html.
    Пример: /getprompt contract
    """
    args = message.text.split()
    if len(args) < 2 or args[1] not in ("contract", "html"):
        await message.answer("Использование: /getprompt contract или /getprompt html")
        return

    # Определяем имя файла в зависимости от типа промпта
    filename = "contract_check.txt" if args[1] == "contract" else "html_format.txt"
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", filename)

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # Отправляем файл документом — удобно сохранить и отредактировать
    from aiogram.types import BufferedInputFile
    await message.answer_document(
        BufferedInputFile(text.encode("utf-8"), filename=filename),
        caption=f"Текущий промпт [{args[1]}]"
    )


@router.message(Command("setprompt"))
async def cmd_setprompt(message: Message, state: FSMContext) -> None:
    """
    Скрытая команда для обновления промптов на лету.
    Принимает аргумент: contract или html.
    Пример: /setprompt contract
    """
    args = message.text.split()
    if len(args) < 2 or args[1] not in ("contract", "html"):
        await message.answer("Использование: /setprompt contract или /setprompt html")
        return
    # Переходим в состояние ожидания нового текста промпта
    await state.set_state(UserStates.waiting_new_prompt)
    await state.update_data(prompt_type=args[1])
    await message.answer(f"Отправьте .txt файл с новым промптом для [{args[1]}]. Для отмены — /cancel")


async def handle_new_prompt(message: Message, state: FSMContext, bot: Bot) -> None:
    """
    Получает новый промпт в виде .txt файла, сохраняет его и перезагружает в памяти.
    Вызывается из handle_document при активном состоянии waiting_new_prompt.
    """
    # Принимаем только .txt файл
    if not message.document.file_name.endswith(".txt"):
        await message.answer("Отправьте файл в формате .txt")
        return

    data = await state.get_data()
    prompt_type = data.get("prompt_type")

    # Определяем имя файла в зависимости от типа промпта
    filename = "contract_check.txt" if prompt_type == "contract" else "html_format.txt"
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", filename)

    # Скачиваем файл и сохраняем на диск
    file = await bot.get_file(message.document.file_id)
    content = await bot.download_file(file.file_path)
    text = content.read().decode("utf-8")

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    # Обновляем промпт в памяти, чтобы изменения вступили в силу немедленно
    import config
    if prompt_type == "contract":
        config.CONTRACT_CHECK_PROMPT = text
    else:
        config.HTML_FORMAT_PROMPT = text

    await state.clear()
    await message.answer(f"✅ Промпт [{prompt_type}] обновлён.")


@router.callback_query(F.data.startswith("model_"))
async def handle_model_choice(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Обрабатывает выбор модели из инлайн-клавиатуры.
    Получает сохранённый текст договора и запускает анализ выбранной моделью.
    """
    # Определяем модель по значению callback_data
    model = "gpt-4o-mini" if callback.data == "model_mini" else "gpt-4o"

    data = await state.get_data()
    text = data.get("contract_text")

    # Редактируем сообщение с клавиатурой — убираем кнопки и показываем статус
    await callback.message.edit_text("Анализирую договор, подождите...")

    result = await analyze_contract(text, model=model)
    await callback.message.answer(result)
    await state.clear()


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает текстовое сообщение в зависимости от текущего состояния:
    - waiting_html — форматирует текст в HTML через analyze_html()
    - waiting_contract или без состояния — проверяет как договор через analyze_contract()
    """
    current_state = await state.get_state()
    await state.clear()

    if current_state == UserStates.waiting_html.state:
        # Режим форматирования HTML
        await message.answer("Форматирую текст, подождите...")
        result = await analyze_html(message.text)
        await message.answer(f"```\n{result}\n```", parse_mode="MarkdownV2")
    else:
        # Режим проверки договора — сохраняем текст и предлагаем выбрать модель
        await state.update_data(contract_text=message.text)
        await state.set_state(UserStates.waiting_model_choice)
        await message.answer(
            "Выберите режим проверки:\n\n"
            "Быстрая (gpt-4o-mini) — дешевле, справляется с большинством договоров\n"
            "Умная (gpt-4o) — точнее, лучше для сложных или нестандартных документов",
            reply_markup=get_model_keyboard(),
        )
