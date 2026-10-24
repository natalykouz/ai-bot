"""
Хендлер для проверки договоров и форматирования HTML.
Использует FSM для отслеживания режима ожидания ввода от пользователя.
"""

import html
import io
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Union

from aiogram import BaseMiddleware, Bot, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, TelegramObject,
)

from services.openai_service import analyze_contract
from services.html_service import analyze_html
from services import auth_service
from utils.telegram_send import send_long_text

logger = logging.getLogger(__name__)

# Порог, после которого результат HTML-форматирования отправляется файлом, а не сообщением
HTML_INLINE_LIMIT = 3500

# Клавиатура главного меню
keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Email-рассылки")],
        [KeyboardButton(text="Проверка договоров")],
        [KeyboardButton(text="Форматирование HTML страниц")]
    ],
    resize_keyboard=True
)

# Клавиатура с кнопкой отмены — показывается, пока бот ждёт договор или текст для HTML
cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
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


async def _fail_analysis(answerable, state: FSMContext, context: str) -> None:
    """
    Общая обработка ошибок анализа/парсинга: логирует с traceback,
    сбрасывает состояние и возвращает пользователя в главное меню.
    """
    logger.exception("Ошибка при %s", context)
    await state.clear()
    await answerable.answer(
        "Не удалось выполнить проверку, попробуйте ещё раз позже.",
        reply_markup=keyboard,
    )


async def _send_html_result(answerable, result: str, truncated: bool) -> None:
    """
    Отправляет результат HTML-форматирования: короткий — в <pre> с parse_mode=HTML,
    длинный — файлом result.html. В обоих случаях добавляет предупреждение,
    если ответ модели был обрезан по лимиту токенов.
    """
    if len(result) <= HTML_INLINE_LIMIT:
        text = f"<pre>{html.escape(result)}</pre>"
        if truncated:
            text += "\n\n⚠️ Ответ получился слишком длинным и был обрезан."
        await answerable.answer(text, parse_mode="HTML")
    else:
        caption = "Результат форматирования (слишком длинный для сообщения)"
        if truncated:
            caption += "\n⚠️ Ответ получился слишком длинным и был обрезан."
        await answerable.answer_document(
            BufferedInputFile(result.encode("utf-8"), filename="result.html"),
            caption=caption,
        )


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
    "<code>/setprompt html</code>\n\n"
    "Добавить администратора:\n"
    "<code>/addadmin user_id</code>\n\n"
    "Удалить администратора:\n"
    "<code>/removeadmin user_id</code>\n\n"
    "Удалить сборки Email-рассылок старше N дней (по умолчанию 7):\n"
    "<code>/cleanupbuilds</code>\n"
    "<code>/cleanupbuilds 14</code>"
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
        "На выходе — список замечаний с указанием что именно требует исправления.",
        reply_markup=cancel_keyboard,
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
        "предварительно очистив содержимое поля.",
        reply_markup=cancel_keyboard,
    )


@router.message(F.text == "Отмена")
async def btn_cancel(message: Message, state: FSMContext) -> None:
    """Обрабатывает нажатие кнопки 'Отмена' — вызывает cmd_cancel."""
    await cmd_cancel(message, state)


@router.message(F.document)
async def handle_document(message: Message, state: FSMContext, bot: Bot) -> None:
    """
    Обрабатывает полученный документ в зависимости от текущего состояния:
    - waiting_new_prompt — передаёт управление handle_new_prompt (админская загрузка промпта)
    - waiting_html       — принимает .html/.htm/.txt и анализирует через analyze_html()
    - waiting_contract   — принимает .txt/.docx/.pdf и анализирует через analyze_contract()
    - без состояния      — просит сначала выбрать режим проверки
    """
    current_state = await state.get_state()

    if current_state == UserStates.waiting_new_prompt.state:
        await handle_new_prompt(message, state, bot)
        return

    if current_state is None:
        await message.answer("Сначала выберите тип проверки:", reply_markup=keyboard)
        return

    document = message.document
    file_name = document.file_name or ""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    is_html_mode = current_state == UserStates.waiting_html.state

    if is_html_mode:
        allowed_exts = ("html", "htm", "txt")
    else:
        allowed_exts = ("txt", "docx", "pdf")

    if ext not in allowed_exts:
        if is_html_mode:
            await message.answer(
                "Вы выбрали проверку HTML. Пришлите файл .html/.txt или вставьте код текстом. "
                "Если нужно проверить договор — нажмите «Отмена» и выберите проверку договора."
            )
        else:
            await message.answer("Поддерживаются только .txt, .docx, .pdf")
        return

    # Скачиваем файл в оперативную память
    file = await message.bot.get_file(document.file_id)
    buf = io.BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    buf.seek(0)

    if is_html_mode:
        # .html/.htm/.txt — читаем как текст (UTF-8, с fallback на cp1251)
        raw = buf.read()
        try:
            html_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            html_text = raw.decode("cp1251", errors="replace")

        if not html_text.strip():
            await message.answer("Не удалось извлечь текст из файла. Попробуйте другой файл.")
            return

        await message.answer("Форматирую текст, подождите...")
        try:
            result, truncated = await analyze_html(html_text)
        except Exception:
            await _fail_analysis(message, state, "форматировании HTML из файла")
            return

        try:
            await _send_html_result(message, result, truncated)
        except Exception:
            logger.exception("Ошибка при отправке результата HTML-форматирования")
            await message.answer("Не удалось отправить результат, попробуйте ещё раз позже.")

        await state.clear()
        await message.answer(
            "Готово. Чтобы отформатировать другую страницу — выберите тип проверки.",
            reply_markup=keyboard,
        )
        return

    # Режим договора — извлекаем текст в зависимости от расширения
    try:
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
    except Exception:
        await _fail_analysis(message, state, f"парсинге файла .{ext}")
        return

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
    await message.answer("Действие отменено.", reply_markup=keyboard)


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


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message) -> None:
    """
    Скрытая команда для добавления нового администратора по его Telegram user_id.
    Доступна только админам.
    Пример: /addadmin 123456789
    """
    if not auth_service.is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: /addadmin <user_id>")
        return

    user_id = int(args[1])
    if auth_service.add_admin(user_id):
        await message.answer(f"✅ Пользователь {user_id} добавлен в администраторы.")
    else:
        await message.answer("Этот пользователь уже администратор.")


@router.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message) -> None:
    """
    Скрытая команда для удаления администратора по его Telegram user_id.
    Доступна только админам. Нельзя удалить последнего администратора.
    Пример: /removeadmin 123456789
    """
    if not auth_service.is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: /removeadmin <user_id>")
        return

    user_id = int(args[1])
    try:
        removed = auth_service.remove_admin(user_id)
    except ValueError as e:
        await message.answer(f"❌ Не удалось удалить: {e}.")
        return

    if removed:
        await message.answer(f"✅ Пользователь {user_id} удалён из администраторов.")
    else:
        await message.answer("Этот пользователь не администратор.")


@router.message(Command("getprompt"))
async def cmd_getprompt(message: Message) -> None:
    """
    Скрытая команда для получения текущего промпта в виде файла.
    Принимает аргумент: contract или html.
    Доступна только админам.
    Пример: /getprompt contract
    """
    if not auth_service.is_admin(message.from_user.id):
        return

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
    Доступна только админам.
    Пример: /setprompt contract
    """
    if not auth_service.is_admin(message.from_user.id):
        return

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

    try:
        result, truncated = await analyze_contract(text, model=model)
    except Exception:
        await _fail_analysis(callback.message, state, "анализе договора")
        return

    try:
        # Без parse_mode: символы <, >, & в тексте договора не должны ломать отправку
        await send_long_text(callback.message, result, parse_mode=None, truncated=truncated)
    except Exception:
        logger.exception("Ошибка при отправке результата анализа договора")
        await callback.message.answer("Не удалось отправить результат, попробуйте ещё раз позже.")

    await state.clear()
    await callback.message.answer(
        "Готово. Чтобы проверить исправленную версию или другой договор — выберите тип проверки.",
        reply_markup=keyboard,
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext) -> None:
    """
    Обрабатывает текстовое сообщение в зависимости от текущего состояния:
    - waiting_html     — форматирует текст в HTML через analyze_html()
    - waiting_contract — проверяет как договор через analyze_contract()
    - без состояния    — просит сначала выбрать режим проверки
    """
    current_state = await state.get_state()

    if current_state is None:
        await message.answer("Сначала выберите тип проверки:", reply_markup=keyboard)
        return

    await state.clear()

    if current_state == UserStates.waiting_html.state:
        # Режим форматирования HTML
        await message.answer("Форматирую текст, подождите...")
        try:
            result, truncated = await analyze_html(message.text)
        except Exception:
            await _fail_analysis(message, state, "форматировании HTML из текста")
            return

        try:
            await _send_html_result(message, result, truncated)
        except Exception:
            logger.exception("Ошибка при отправке результата HTML-форматирования")
            await message.answer("Не удалось отправить результат, попробуйте ещё раз позже.")

        await message.answer(
            "Готово. Чтобы отформатировать другую страницу — выберите тип проверки.",
            reply_markup=keyboard,
        )
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
