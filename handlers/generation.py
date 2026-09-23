"""
Хендлер раздела «Email-рассылки» — подменю с тремя независимыми операциями:
Генерация расписания / Генерация письма / Проверка качества письма.

Отдельный FSM/роутер от handlers/contract.py — не переиспользует и не меняет
состояния двух существующих функций. Отвечает только за диалог, кнопки,
приём и отправку файлов, состояние сессии; вся бизнес-логика Build
Manager/Schedule/Generation/QA находится в services/generation_service.py
и mail_project/*.

Три операции — независимые flow (свои состояния, свой BUILD_ID у каждого
запуска), не объединены в единый pipeline.
"""

import html
import json
import logging
import re
from pathlib import Path
from tempfile import mkdtemp

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

from config import settings
from handlers.contract import AuthMiddleware, keyboard as main_menu_keyboard
from services import auth_service, component_fields, generation_service as gensvc

CLEANUP_DEFAULT_DAYS = 7

SMM_INSTRUCTION_PATH = Path(__file__).resolve().parent.parent / "SMM_INSTRUCTION.md"
SMM_INSTRUCTION_TEXT = SMM_INSTRUCTION_PATH.read_text(encoding="utf-8").strip()

logger = logging.getLogger(__name__)

router = Router()
router.message.middleware(AuthMiddleware())
router.callback_query.middleware(AuthMiddleware())

cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
    resize_keyboard=True,
)


class ScheduleGenStates(StatesGroup):
    """Самостоятельный flow «Генерация расписания» — после XLSX СММ выбирает режим
    отбора по билетам (choosing_filter_mode) и указывает порог для выбранного
    режима (waiting_filter_threshold); оба хранятся в state и переиспользуются
    при перегенерации без повторного вопроса. После генерации СММ может сколько
    угодно раз перегенерировать (reviewing_schedule), см.
    _deliver_schedule_result()."""
    waiting_xlsx = State()
    choosing_filter_mode = State()
    waiting_filter_threshold = State()
    reviewing_schedule = State()
    choosing_fish_branch = State()
    choosing_fish_template = State()


class LetterGenStates(StatesGroup):
    """Самостоятельный flow «Генерация письма» (без обработки XLSX/Schedule processor)."""
    waiting_subject = State()
    choosing_branch = State()
    choosing_schedule_choice = State()
    waiting_schedule_txt = State()
    choosing_schedule_template = State()
    choosing_header = State()
    choosing_hero = State()
    choosing_footer = State()
    choosing_content_module = State()  # старый flow «выбор модуля -> выбор элемента» (закомментирован ниже) — состояния оставлены для быстрого возврата
    choosing_content_element = State()  # см. выше
    waiting_component_name = State()  # новый режим: название компонента вводится текстом (соответствие manifest.json), см. _ask_component_menu/_add_component_by_name
    waiting_content_text = State()
    confirming_content_fields = State()  # старый многошаговый flow (закомментирован ниже) — состояние оставлено для быстрого возврата
    waiting_field_value = State()  # см. выше
    reviewing_filled_component = State()  # см. выше
    waiting_single_field_value = State()  # упрощённый режим: одно поле на компонент, см. COMPONENT_SMM_FIELD_MAPPING.md
    choosing_add_more = State()


class QAStates(StatesGroup):
    """Самостоятельный flow «Проверка качества письма» — работает с загруженным HTML,
    не привязан к BUILD_ID/build_dir/Build Manager."""
    waiting_html = State()


class ScheduleTxtFishStates(StatesGroup):
    """Самостоятельный flow «HTML-основа письма из SCHEDULE.txt» — СММ присылает уже
    готовый SCHEDULE.txt напрямую (без XLSX/Schedule processor, см. ScheduleGenStates),
    build_id создаётся только чтобы получить доступ к canonical-библиотеке компонентов
    (source/manifest.json + unisender_components.zip) для build_schedule_fish()."""
    waiting_schedule_txt = State()
    choosing_branch = State()
    choosing_template = State()


class ScheduleSwapStates(StatesGroup):
    """Самостоятельный flow «Заменить расписание в готовом письме» — СММ присылает уже
    собранный из блоков UniSender-шаблон целиком (не произвольный HTML, waiting_html),
    затем новый SCHEDULE.txt (waiting_schedule_txt) — старый Schedule-регион в шаблоне
    заменяется на новые блоки, построенные тем же canonical-генератором Schedule."""
    waiting_html = State()
    waiting_schedule_txt = State()


_VARIANT_RE = re.compile(r"^Вариант\s+(\d+)$")


def _idx_keyboard(items: list, prefix: str) -> InlineKeyboardMarkup:
    variant_matches = [_VARIANT_RE.match(item) for item in items]
    if items and all(variant_matches):
        buttons = [
            InlineKeyboardButton(text=m.group(1), callback_data=f"{prefix}:{i}")
            for i, m in enumerate(variant_matches)
        ]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=item, callback_data=f"{prefix}:{i}")] for i, item in enumerate(items)]
    )


def _plural_stroki(n: int) -> str:
    if n == 1:
        return "строка"
    if 2 <= n <= 4:
        return "строки"
    return "строк"


def _find_line(output: str, prefix: str) -> str:
    return next((line.strip() for line in output.splitlines() if line.strip().startswith(prefix)), "")


# --- Точка входа: подменю «Email-рассылки» ---------------------------------------

@router.message(F.text == "Email-рассылки")
async def btn_email_menu(message: Message, state: FSMContext) -> None:
    """Открывает подменю с независимыми операциями. Сами кнопки не выведены
    в главное меню — только через этот пункт.

    «Генерация письма» и «Проверка качества письма» временно скрыты как
    неактуальные (кнопки убраны из клавиатуры) — сам код флоу (LetterGenStates,
    QAStates, все их хендлеры ниже) не удалён и не изменён."""
    await state.clear()
    await message.answer(
        "Email-рассылки. Выберите операцию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Генерация расписания", callback_data="mail:schedule")],
            [InlineKeyboardButton(text="HTML-основа письма из SCHEDULE.txt", callback_data="mail:schedule_txt_fish")],
            [InlineKeyboardButton(text="Заменить расписание в готовом письме", callback_data="mail:schedule_swap")],
        ]),
    )


# =================================================================================
# 1. Генерация расписания: XLSX -> новый BUILD_ID -> add-input -> Schedule -> SCHEDULE.txt
# =================================================================================

@router.callback_query(F.data == "mail:schedule")
async def start_schedule_gen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ScheduleGenStates.waiting_xlsx)
    await callback.message.edit_text("Генерация расписания.")
    await callback.message.answer(
        "Пришлите XLSX-файл выгрузки мероприятий (документом).",
        reply_markup=cancel_keyboard,
    )
    await callback.answer()


@router.message(ScheduleGenStates.waiting_xlsx, F.document)
async def schedule_gen_receive_xlsx(message: Message, state: FSMContext) -> None:
    document = message.document
    file_name = document.file_name or "schedule.xlsx"
    if not file_name.lower().endswith(".xlsx"):
        await message.answer("Нужен файл в формате .xlsx. Пришлите выгрузку ещё раз.")
        return

    tmp_dir = Path(mkdtemp(prefix="sched_xlsx_"))
    tmp_path = tmp_dir / file_name
    file = await message.bot.get_file(document.file_id)
    await message.bot.download_file(file.file_path, destination=str(tmp_path))

    await message.answer("Создаю сборку и обрабатываю выгрузку...")
    try:
        build_id = await gensvc.create_build()
        await gensvc.add_input(build_id, tmp_path)
    except gensvc.GenerationServiceError as exc:
        await message.answer(f"Не удалось построить расписание:\n{exc}\n\nПришлите файл ещё раз или отправьте /cancel.")
        return
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass

    await state.update_data(build_id=build_id)
    await _ask_filter_mode(message, state)


# Режимы отбора события по билетам (mail_project/schedule_processor.py FILTER_MODES) —
# текст кнопки, ключ callback_data и текст вопроса про порог для каждого режима.
_FILTER_MODE_OPTIONS = [
    ("percent", "Процент свободных билетов"),
    ("remaining_min", "Осталось билетов (минимум)"),
    ("sold_max", "Продано билетов (максимум)"),
]

_FILTER_MODE_THRESHOLD_PROMPT = {
    "percent": (
        "Какой процент свободных билетов должен быть у мероприятия, чтобы оно попало в расписание?\n\n"
        "Например, если указать 50, в расписание попадут мероприятия, где свободно больше 50% билетов.\n\n"
        "Введите число от 0 до 100."
    ),
    "remaining_min": (
        "При каком минимальном количестве оставшихся билетов мероприятие попадёт в расписание?\n\n"
        "Например, если указать 5, в расписание попадут мероприятия, где осталось от 5 билетов.\n\n"
        "Введите целое число от 0."
    ),
    "sold_max": (
        "При каком максимальном количестве проданных билетов мероприятие попадёт в расписание?\n\n"
        "Например, если указать 5, в расписание попадут мероприятия, где продано до 5 билетов.\n\n"
        "Введите целое число от 0."
    ),
}


async def _ask_filter_mode(message: Message, state: FSMContext) -> None:
    await state.set_state(ScheduleGenStates.choosing_filter_mode)
    await message.answer(
        "По какому критерию отбирать мероприятия в расписание?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"schedgen:mode:{mode}")]
            for mode, label in _FILTER_MODE_OPTIONS
        ]),
    )


@router.callback_query(ScheduleGenStates.choosing_filter_mode, F.data.startswith("schedgen:mode:"))
async def schedule_gen_choose_mode(callback: CallbackQuery, state: FSMContext) -> None:
    filter_mode = callback.data.split(":", 2)[2]
    await state.update_data(filter_mode=filter_mode)
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(ScheduleGenStates.waiting_filter_threshold)
    await callback.message.answer(
        _FILTER_MODE_THRESHOLD_PROMPT[filter_mode],
        reply_markup=cancel_keyboard,
    )
    await callback.answer()


@router.message(ScheduleGenStates.choosing_filter_mode, F.text == "Отмена")
@router.message(ScheduleGenStates.choosing_filter_mode, Command("cancel"))
@router.message(ScheduleGenStates.waiting_filter_threshold, F.text == "Отмена")
@router.message(ScheduleGenStates.waiting_filter_threshold, Command("cancel"))
async def schedule_gen_cancel_threshold(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Генерация расписания отменена.", reply_markup=main_menu_keyboard)


@router.message(ScheduleGenStates.waiting_filter_threshold, F.text)
async def schedule_gen_receive_threshold(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    filter_mode = data["filter_mode"]

    text = (message.text or "").strip().replace(",", ".")
    try:
        threshold = float(text)
    except ValueError:
        await message.answer("Нужно число. Введите порог ещё раз.")
        return
    if filter_mode == "percent":
        if not (0 <= threshold <= 100):
            await message.answer("Нужно число от 0 до 100. Введите процент ещё раз.")
            return
    elif threshold < 0:
        await message.answer("Нужно целое число от 0. Введите порог ещё раз.")
        return

    build_id = data["build_id"]
    await state.update_data(filter_threshold=threshold)

    await message.answer("Строю расписание...")
    try:
        output = await gensvc.run_schedule(build_id, filter_mode, threshold)
    except gensvc.GenerationServiceError as exc:
        await state.set_state(ScheduleGenStates.waiting_xlsx)
        await message.answer(f"Не удалось построить расписание:\n{exc}\n\nПришлите файл ещё раз или отправьте /cancel.")
        return

    await _deliver_schedule_result(message, state, build_id, output)


@router.message(ScheduleGenStates.waiting_filter_threshold)
async def schedule_gen_threshold_wrong_input(message: Message) -> None:
    await message.answer("Ожидаю число. Или отправьте /cancel для отмены.")


async def _deliver_schedule_result(message: Message, state: FSMContext, build_id: str, output: str) -> None:
    """Выдаёт SCHEDULE.txt и предлагает «Перегенерировать»/«Оставить как есть» —
    вызывается и после первой генерации, и после каждой перегенерации
    (schedule_gen_regen). Список мероприятий, отсутствующих в Google Sheets,
    определяется заново из output каждый раз, отдельно не хранится. Файл
    выдаётся всегда, независимо от того, есть такие мероприятия или нет."""
    schedule_path = gensvc.build_dir_for(build_id) / "schedule" / "SCHEDULE.txt"
    summary_line = _find_line(output, "Дат:")
    # Филиал здесь надёжно не определим: один XLSX/SCHEDULE.txt может содержать
    # события нескольких филиалов одновременно (см. schedule_processor.py) — имя
    # без филиала.
    out_name = gensvc.next_result_filename(gensvc.RESULT_TYPE_SCHEDULE, "txt")
    await message.answer_document(
        BufferedInputFile(schedule_path.read_bytes(), filename=out_name),
        caption=f"BUILD_ID: {build_id}. {summary_line}".strip(),
    )

    missing = gensvc.missing_events_from_output(output)
    if missing:
        lines = ["Не найдены в Google Sheets (использованы название/ссылка из выгрузки):"]
        lines.extend(
            f"- {item['branch']}, ID тура {item['tour_id']}: {item['title']}"
            for item in missing
        )
        await message.answer("\n".join(lines))

    await state.set_state(ScheduleGenStates.reviewing_schedule)
    await message.answer(
        "Что дальше?",
        reply_markup=_reviewing_schedule_keyboard(),
    )


def _reviewing_schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Перегенерировать расписание", callback_data="schedgen:regen")],
        [InlineKeyboardButton(text="Создать HTML-основу письма", callback_data="schedgen:fish")],
        [InlineKeyboardButton(text="Оставить как есть", callback_data="schedgen:keep")],
    ])


@router.callback_query(ScheduleGenStates.reviewing_schedule, F.data == "schedgen:regen")
async def schedule_gen_regen(callback: CallbackQuery, state: FSMContext) -> None:
    """Перегенерация поверх того же build/XLSX и того же режима/порога отбора по
    билетам (СММ не спрашивают заново) — заново читает Google Sheets
    (run_schedule запускает schedule_processor.run() с нуля) и заново выдаёт
    файл/список/кнопки. Цикл может повторяться сколько угодно раз."""
    data = await state.get_data()
    build_id = data["build_id"]
    filter_mode = data["filter_mode"]
    threshold = data["filter_threshold"]
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Перегенерирую расписание...")
    try:
        output = await gensvc.run_schedule(build_id, filter_mode, threshold)
    except gensvc.GenerationServiceError as exc:
        await callback.message.answer(
            f"Не удалось перегенерировать расписание:\n{exc}\n\nПредыдущий файл остаётся в силе.",
            reply_markup=_reviewing_schedule_keyboard(),
        )
        await callback.answer()
        return
    await _deliver_schedule_result(callback.message, state, build_id, output)
    await callback.answer()


@router.callback_query(ScheduleGenStates.reviewing_schedule, F.data == "schedgen:keep")
async def schedule_gen_keep(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Готово.", reply_markup=main_menu_keyboard)
    await callback.answer()


@router.callback_query(ScheduleGenStates.reviewing_schedule, F.data == "schedgen:fish")
async def schedule_gen_fish_start(callback: CallbackQuery, state: FSMContext) -> None:
    """ЭТАП 3: «Создать HTML-основу письма» — сначала спрашивает филиал (для выбора
    Editor Template, см. generation.EDITOR_TEMPLATE_PATHS; в этом flow филиал никогда
    не спрашивался и не выводится из событий SCHEDULE.txt — один build может содержать
    события нескольких филиалов), затем вид Schedule-компонента (schedule_gen_fish_branch_choose)."""
    options = gensvc.fish_branch_options()
    await state.update_data(_fish_branch_list=options)
    await state.set_state(ScheduleGenStates.choosing_fish_branch)
    buttons = [
        InlineKeyboardButton(text=branch, callback_data=f"schedgen:fishbranch:{i}")
        for i, branch in enumerate(options)
    ]
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Выберите филиал:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons]),
    )
    await callback.answer()


@router.callback_query(ScheduleGenStates.choosing_fish_branch, F.data.startswith("schedgen:fishbranch:"))
async def schedule_gen_fish_branch_choose(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    branch = data["_fish_branch_list"][idx]
    await state.update_data(fish_branch=branch)

    options = gensvc.schedule_template_options()
    await state.update_data(_fish_template_list=options)
    await state.set_state(ScheduleGenStates.choosing_fish_template)
    buttons = [
        InlineKeyboardButton(text=str(i + 1), callback_data=f"schedgen:fishtpl:{i}")
        for i in range(len(options))
    ]
    await callback.message.edit_text(f"Филиал: {branch}")
    await callback.message.answer(
        "Выберите вид расписания:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )
    await callback.answer()


@router.callback_query(ScheduleGenStates.choosing_fish_template, F.data.startswith("schedgen:fishtpl:"))
async def schedule_gen_fish_choose(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    element = data["_fish_template_list"][idx]
    branch = data["fish_branch"]
    build_id = data["build_id"]

    await callback.message.edit_text(f"Расписание: {element}")
    await callback.message.answer("Собираю HTML-основу письма...")
    try:
        fish_path = await gensvc.build_schedule_fish(build_id, element, branch)
    except gensvc.GenerationServiceError as exc:
        await callback.message.answer(
            f"Не удалось собрать HTML-основу письма:\n{exc}",
            reply_markup=_reviewing_schedule_keyboard(),
        )
        await state.set_state(ScheduleGenStates.reviewing_schedule)
        await callback.answer()
        return

    out_name = gensvc.next_result_filename(gensvc.RESULT_TYPE_SCHEDULE_FISH, "html", branch=branch)
    await callback.message.answer_document(
        BufferedInputFile(fish_path.read_bytes(), filename=out_name),
        caption=f"BUILD_ID: {build_id}. {branch}. {element}.",
    )
    await state.set_state(ScheduleGenStates.reviewing_schedule)
    await callback.message.answer("Что дальше?", reply_markup=_reviewing_schedule_keyboard())
    await callback.answer()


@router.message(ScheduleGenStates.waiting_xlsx, F.text == "Отмена")
@router.message(ScheduleGenStates.waiting_xlsx, Command("cancel"))
async def schedule_gen_cancel(message: Message, state: FSMContext) -> None:
    """Кнопка «Отмена»/команда /cancel на шаге ожидания XLSX — выходит из flow
    без повторного «Ожидаю XLSX-файл» (в отличие от catch-all ниже) и без
    попадания в общий /cancel из contract.py."""
    await state.clear()
    await message.answer("Генерация расписания отменена.", reply_markup=main_menu_keyboard)


@router.message(ScheduleGenStates.waiting_xlsx)
async def schedule_gen_wrong_input(message: Message) -> None:
    await message.answer("Ожидаю XLSX-файл документом. Или отправьте /cancel для отмены.")


# =================================================================================
# 1b. HTML-основа письма из готового SCHEDULE.txt: файл присылается напрямую,
#     без XLSX/Schedule processor -> филиал -> вид расписания -> email_base.html.
#     Переиспользует ту же gensvc.build_schedule_fish(), что и ScheduleGenStates —
#     Schedule-парсинг/рендеринг не дублируется.
# =================================================================================

@router.callback_query(F.data == "mail:schedule_txt_fish")
async def start_schedule_txt_fish(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ScheduleTxtFishStates.waiting_schedule_txt)
    await callback.message.edit_text("HTML-основа письма из готового SCHEDULE.txt.")
    await callback.message.answer(
        "Пришлите файл SCHEDULE.txt документом.",
        reply_markup=cancel_keyboard,
    )
    await callback.answer()


@router.message(ScheduleTxtFishStates.waiting_schedule_txt, F.document)
async def schedule_txt_fish_receive(message: Message, state: FSMContext) -> None:
    document = message.document
    file_name = document.file_name or "SCHEDULE.txt"
    if not file_name.lower().endswith(".txt"):
        await message.answer("Нужен текстовый файл SCHEDULE.txt. Пришлите файл ещё раз.")
        return

    file = await message.bot.get_file(document.file_id)
    buf = await message.bot.download_file(file.file_path)
    content = buf.read()

    await message.answer("Создаю сборку...")
    try:
        build_id = await gensvc.create_build()
        gensvc.save_schedule_file(build_id, content)
    except gensvc.GenerationServiceError as exc:
        await message.answer(f"Не удалось сохранить SCHEDULE.txt: {exc}\n\nПришлите файл ещё раз или отправьте /cancel.")
        return

    await state.update_data(build_id=build_id)
    await _ask_schedule_txt_fish_branch(message, state)


async def _ask_schedule_txt_fish_branch(message: Message, state: FSMContext) -> None:
    options = gensvc.fish_branch_options()
    await state.update_data(_branch_list=options)
    await state.set_state(ScheduleTxtFishStates.choosing_branch)
    buttons = [
        InlineKeyboardButton(text=branch, callback_data=f"schedtxt:branch:{i}")
        for i, branch in enumerate(options)
    ]
    await message.answer(
        "Выберите филиал:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons]),
    )


@router.callback_query(ScheduleTxtFishStates.choosing_branch, F.data.startswith("schedtxt:branch:"))
async def schedule_txt_fish_choose_branch(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    branch = data["_branch_list"][idx]
    await state.update_data(branch=branch)

    options = gensvc.schedule_template_options()
    await state.update_data(_template_list=options)
    await state.set_state(ScheduleTxtFishStates.choosing_template)
    buttons = [
        InlineKeyboardButton(text=str(i + 1), callback_data=f"schedtxt:tpl:{i}")
        for i in range(len(options))
    ]
    await callback.message.edit_text(f"Филиал: {branch}")
    await callback.message.answer(
        "Выберите вид расписания:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )
    await callback.answer()


@router.callback_query(ScheduleTxtFishStates.choosing_template, F.data.startswith("schedtxt:tpl:"))
async def schedule_txt_fish_choose_template(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    element = data["_template_list"][idx]
    branch = data["branch"]
    build_id = data["build_id"]

    await callback.message.edit_text(f"Расписание: {element}")
    await callback.message.answer("Собираю HTML-основу письма...")
    try:
        fish_path = await gensvc.build_schedule_fish(build_id, element, branch)
    except gensvc.GenerationServiceError as exc:
        await callback.message.answer(f"Не удалось собрать HTML-основу письма: {exc}")
        await state.clear()
        await callback.answer()
        return

    out_name = gensvc.next_result_filename(gensvc.RESULT_TYPE_SCHEDULE_FISH, "html", branch=branch)
    await callback.message.answer_document(
        BufferedInputFile(fish_path.read_bytes(), filename=out_name),
        caption=f"BUILD_ID: {build_id}. {branch}. {element}.",
    )
    await state.clear()
    await callback.message.answer("Готово.", reply_markup=main_menu_keyboard)
    await callback.answer()


@router.message(ScheduleTxtFishStates.waiting_schedule_txt, F.text == "Отмена")
@router.message(ScheduleTxtFishStates.waiting_schedule_txt, Command("cancel"))
async def schedule_txt_fish_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard)


@router.message(ScheduleTxtFishStates.waiting_schedule_txt)
async def schedule_txt_fish_wrong_input(message: Message) -> None:
    await message.answer("Ожидаю файл SCHEDULE.txt документом. Или отправьте /cancel для отмены.")


# =================================================================================
# 1c. Заменить расписание в готовом письме: СММ присылает уже собранный из блоков
#     UniSender-шаблон целиком (этап 1 — входная проверка формата и Schedule-региона),
#     затем новый SCHEDULE.txt (этап 2) — старый Schedule-регион заменяется на новые
#     блоки, построенные тем же canonical-генератором Schedule (build_schedule_blocks),
#     что и во всех остальных Schedule-flow.
# =================================================================================

@router.callback_query(F.data == "mail:schedule_swap")
async def start_schedule_swap(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ScheduleSwapStates.waiting_html)
    await callback.message.edit_text("Заменить расписание в готовом письме.")
    await callback.message.answer(
        "Пришлите HTML-шаблон письма документом — целиком, собранный из блоков UniSender.",
        reply_markup=cancel_keyboard,
    )
    await callback.answer()


@router.message(ScheduleSwapStates.waiting_html, F.document)
async def receive_schedule_swap_html(message: Message, state: FSMContext) -> None:
    document = message.document
    file_name = document.file_name or ""

    html_text = ""
    valid_format = False
    if file_name.lower().endswith((".html", ".htm")):
        file = await message.bot.get_file(document.file_id)
        buf = await message.bot.download_file(file.file_path)
        html_text = buf.read().decode("utf-8", errors="replace")
        valid_format = bool(html_text.strip()) and gensvc.is_full_unisender_template(html_text)

    if not valid_format:
        await state.clear()
        await message.answer(
            "Извините, этот файл не подходит, нужен шаблон Юнисендер составленный из блоков целиком",
            reply_markup=main_menu_keyboard,
        )
        return

    schedule_status = gensvc.schedule_blocks_status(html_text)
    if schedule_status == "missing":
        await state.clear()
        await message.answer(
            "В этом шаблоне не найден блок расписания.",
            reply_markup=main_menu_keyboard,
        )
        return
    if schedule_status == "not_contiguous":
        await state.clear()
        await message.answer(
            "Блоки расписания в этом шаблоне идут не подряд — между ними есть другой блок. "
            "Расписание можно заменить только если все его блоки идут подряд.",
            reply_markup=main_menu_keyboard,
        )
        return

    # Формат подтверждён, Schedule-блоки найдены и идут подряд — берём точные границы
    # региона тем же алгоритмом (find_schedule_region), что и schedule_blocks_status.
    # Несовпадение статуса здесь означало бы рассинхронизацию двух функций — на
    # случай такого (не должно происходить) не чиним автоматически, а тоже
    # останавливаем flow.
    region_status, region = gensvc.find_schedule_region(html_text)
    if region_status != "ok" or region is None:
        await state.clear()
        await message.answer(
            "Не удалось однозначно определить границы блока расписания в этом шаблоне. "
            "Замена не выполнена.",
            reply_markup=main_menu_keyboard,
        )
        return

    await state.update_data(html_text=html_text, region=region)
    await state.set_state(ScheduleSwapStates.waiting_schedule_txt)
    await message.answer(
        "Формат подходит: это полный UniSender-шаблон, собранный из блоков, с расписанием.\n\n"
        "Пришлите новый файл SCHEDULE.txt документом.",
        reply_markup=cancel_keyboard,
    )


@router.message(ScheduleSwapStates.waiting_html, F.text == "Отмена")
@router.message(ScheduleSwapStates.waiting_html, Command("cancel"))
async def cancel_schedule_swap(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard)


@router.message(ScheduleSwapStates.waiting_html)
async def schedule_swap_wrong_input(message: Message) -> None:
    await message.answer("Ожидаю HTML-файл документом. Или отправьте /cancel для отмены.")


@router.message(ScheduleSwapStates.waiting_schedule_txt, F.document)
async def receive_schedule_swap_txt(message: Message, state: FSMContext) -> None:
    document = message.document
    file_name = document.file_name or "SCHEDULE.txt"
    if not file_name.lower().endswith(".txt"):
        await message.answer("Нужен текстовый файл SCHEDULE.txt. Пришлите файл ещё раз.")
        return

    data = await state.get_data()
    html_text = data["html_text"]
    region = tuple(data["region"])  # (start, end, element)
    element = region[2]

    file = await message.bot.get_file(document.file_id)
    buf = await message.bot.download_file(file.file_path)
    content = buf.read()

    await message.answer("Строю новое расписание...")
    try:
        new_blocks = await gensvc.build_schedule_replacement_blocks(content, element)
    except gensvc.GenerationServiceError as exc:
        await message.answer(
            f"Не удалось построить новое расписание:\n{exc}\n\nПришлите файл ещё раз или отправьте /cancel."
        )
        return

    new_html = gensvc.replace_schedule_region(html_text, region, new_blocks)

    # Филиал здесь надёжно не определим: пользователь загружает произвольный
    # UniSender-шаблон, шапка (если есть) не гарантированно соответствует
    # фактическому филиалу письма (см. анализ реального экспорта — заголовок
    # «Москва 3» встречался в письме для другого филиала) — имя без филиала.
    out_name = gensvc.next_result_filename(gensvc.RESULT_TYPE_LETTER_WITH_SCHEDULE, "html")
    await message.answer_document(
        BufferedInputFile(new_html.encode("utf-8"), filename=out_name),
        caption="Готово: расписание в шаблоне заменено.",
    )
    await state.clear()
    await message.answer("Готово.", reply_markup=main_menu_keyboard)


@router.message(ScheduleSwapStates.waiting_schedule_txt, F.text == "Отмена")
@router.message(ScheduleSwapStates.waiting_schedule_txt, Command("cancel"))
async def cancel_schedule_swap_txt(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard)


@router.message(ScheduleSwapStates.waiting_schedule_txt)
async def schedule_swap_txt_wrong_input(message: Message) -> None:
    await message.answer("Ожидаю файл SCHEDULE.txt документом. Или отправьте /cancel для отмены.")


# =================================================================================
# 2. Генерация письма: филиал -> (расписание Да/Нет, готовый SCHEDULE.txt) ->
#    Header -> Hero -> Footer -> контентные компоненты (цикл) -> завершение.
# =================================================================================

@router.callback_query(F.data == "mail:letter")
async def start_letter_gen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    branch_list = gensvc.branches()
    await state.update_data(step="waiting_subject", _branch_list=branch_list)
    await state.set_state(LetterGenStates.waiting_subject)
    await callback.message.edit_text("Генерация письма.")
    # Кнопка «Отмена» в нижней клавиатуре (тот же cancel_keyboard, что и в
    # «Генерации расписания») — показывается один раз здесь и остаётся видимой
    # на всех последующих шагах (inline-выбор не трогает нижнюю клавиатуру), пока
    # flow не завершится или не будет отменён. На шагах с inline-выбором (филиал/
    # расписание/шапка/hero/подвал/меню компонентов) у generation.router нет своего
    # message-хендлера — «Отмена»/​/cancel там уже сами уходят в общий /cancel из
    # contract.py. Явные хендлеры нужны только там, где catch-all перехватывает
    # текст раньше (SCHEDULE.txt, поле компонента, тема письма) — см. _cancel_letter_gen ниже.
    await callback.message.answer(
        "Чтобы прервать генерацию письма в любой момент — нажмите «Отмена» или отправьте /cancel.",
        reply_markup=cancel_keyboard,
    )
    if settings.catalog_url:
        await callback.message.answer(
            "Каталог компонентов — все варианты Шапок/Баннеров/Подвалов и "
            "контентных блоков с превью, открывается в браузере:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть каталог компонентов", url=settings.catalog_url),
            ]]),
        )
    await callback.message.answer("Введите тему письма:")
    await callback.answer()


async def _cancel_letter_gen(message: Message, state: FSMContext) -> None:
    """Прерывание «Генерации письма» кнопкой «Отмена»/командой /cancel — единая
    точка выхода из flow независимо от текущего шага, возвращает в главное меню."""
    await state.clear()
    await message.answer("Генерация письма прервана.", reply_markup=main_menu_keyboard)


@router.message(LetterGenStates.waiting_subject, F.text == "Отмена")
@router.message(LetterGenStates.waiting_subject, Command("cancel"))
async def cancel_waiting_subject(message: Message, state: FSMContext) -> None:
    await _cancel_letter_gen(message, state)


@router.message(LetterGenStates.waiting_subject, F.text)
async def receive_subject(message: Message, state: FSMContext) -> None:
    subject = message.text.strip()
    if not subject:
        await message.answer("Тема письма не может быть пустой. Введите тему письма:")
        return

    data = await state.get_data()
    branch_list = data["_branch_list"]
    await state.update_data(subject=subject, step="choosing_branch")
    await state.set_state(LetterGenStates.choosing_branch)
    await message.answer("Выберите филиал:", reply_markup=_idx_keyboard(branch_list, "gen:branch"))


@router.message(LetterGenStates.waiting_subject)
async def waiting_subject_wrong_input(message: Message) -> None:
    await message.answer("Ожидаю текст темы письма. Или отправьте /cancel для отмены.")


@router.callback_query(LetterGenStates.choosing_branch, F.data.startswith("gen:branch:"))
async def choose_branch(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    branch = data["_branch_list"][idx]

    await callback.message.edit_text(f"Филиал: {branch}\n\nСоздаю сборку...")
    try:
        build_id = await gensvc.create_build()
    except gensvc.GenerationServiceError as exc:
        await callback.message.answer(f"Не удалось создать сборку:\n{exc}")
        await state.clear()
        await callback.answer()
        return

    await state.update_data(
        branch=branch,
        build_id=build_id,
        schedule_enabled=False,
        header=None,
        hero=None,
        footer=None,
        content_blocks=[],
    )
    await callback.message.answer(f"Сборка создана: {build_id}")
    await _ask_schedule_choice(callback.message, state)
    await callback.answer()


async def _ask_schedule_choice(message: Message, state: FSMContext) -> None:
    await state.update_data(step="choosing_schedule_choice")
    await state.set_state(LetterGenStates.choosing_schedule_choice)
    await message.answer(
        "Использовать расписание?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да", callback_data="gen:sched:yes"),
            InlineKeyboardButton(text="Нет", callback_data="gen:sched:no"),
        ]]),
    )


@router.callback_query(LetterGenStates.choosing_schedule_choice, F.data == "gen:sched:no")
async def schedule_choice_no(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(schedule_enabled=False)
    await callback.message.edit_text("Расписание: без расписания.")
    await _ask_header(callback.message, state)
    await callback.answer()


@router.callback_query(LetterGenStates.choosing_schedule_choice, F.data == "gen:sched:yes")
async def schedule_choice_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(schedule_enabled=True, step="waiting_schedule_txt")
    await state.set_state(LetterGenStates.waiting_schedule_txt)
    await callback.message.edit_text("Расписание: да.")
    await callback.message.answer(
        "Пришлите готовый файл SCHEDULE.txt документом. "
        "Его можно подготовить через «Генерация расписания».",
        reply_markup=cancel_keyboard,
    )
    await callback.answer()


@router.message(LetterGenStates.waiting_schedule_txt, F.document)
async def receive_schedule_txt(message: Message, state: FSMContext) -> None:
    document = message.document
    file_name = document.file_name or "SCHEDULE.txt"
    if not file_name.lower().endswith(".txt"):
        await message.answer("Нужен текстовый файл SCHEDULE.txt. Пришлите файл ещё раз.")
        return

    data = await state.get_data()
    build_id = data["build_id"]

    file = await message.bot.get_file(document.file_id)
    buf = await message.bot.download_file(file.file_path)
    content = buf.read()

    try:
        gensvc.save_schedule_file(build_id, content)
    except gensvc.GenerationServiceError as exc:
        await message.answer(f"Не удалось сохранить SCHEDULE.txt:\n{exc}\n\nПришлите файл ещё раз или отправьте /cancel.")
        return

    await message.answer("SCHEDULE.txt получен.")
    await _ask_schedule_template(message, state)


async def _ask_schedule_template(message: Message, state: FSMContext) -> None:
    options = gensvc.schedule_template_options()
    await state.update_data(_schedule_template_list=options, step="choosing_schedule_template")
    await state.set_state(LetterGenStates.choosing_schedule_template)
    buttons = [
        InlineKeyboardButton(text=str(i + 1), callback_data=f"gen:schedtpl:{i}")
        for i in range(len(options))
    ]
    await message.answer(
        "Выберите вариант расписания:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


@router.callback_query(LetterGenStates.choosing_schedule_template, F.data.startswith("gen:schedtpl:"))
async def choose_schedule_template(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    element = data["_schedule_template_list"][idx]
    await state.update_data(schedule_template=element)
    await callback.message.edit_text(f"Расписание: {element}")
    await _ask_header(callback.message, state)
    await callback.answer()


@router.message(LetterGenStates.waiting_schedule_txt, F.text == "Отмена")
@router.message(LetterGenStates.waiting_schedule_txt, Command("cancel"))
async def cancel_waiting_schedule_txt(message: Message, state: FSMContext) -> None:
    await _cancel_letter_gen(message, state)


@router.message(LetterGenStates.waiting_schedule_txt)
async def waiting_schedule_txt_wrong_input(message: Message) -> None:
    await message.answer("Ожидаю файл SCHEDULE.txt документом. Или отправьте /cancel для отмены.")


# --- обязательные компоненты (Header / Hero / Footer) ----------------------------

async def _ask_header(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    branch = data["branch"]
    options = gensvc.header_options(branch)

    if not options:
        await state.update_data(header=None)
        await message.answer(
            f"Для филиала «{branch}» нет системного компонента Шапки в библиотеке — шаг пропущен."
        )
        await _ask_hero(message, state)
        return

    await state.update_data(_header_list=options, step="choosing_header")
    await state.set_state(LetterGenStates.choosing_header)
    await message.answer("Выберите компонент Шапки:", reply_markup=_idx_keyboard(options, "gen:header"))


@router.callback_query(LetterGenStates.choosing_header, F.data.startswith("gen:header:"))
async def choose_header(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    element = data["_header_list"][idx]
    await state.update_data(header={"module": "Шапки", "element": element})
    await callback.message.edit_text(f"Шапки: {element}")
    await _ask_hero(callback.message, state)
    await callback.answer()


async def _ask_hero(message: Message, state: FSMContext) -> None:
    options = gensvc.hero_options()
    await state.update_data(_hero_list=options, step="choosing_hero")
    await state.set_state(LetterGenStates.choosing_hero)
    await message.answer("Выберите компонент Баннеры:", reply_markup=_idx_keyboard(options, "gen:hero"))


@router.callback_query(LetterGenStates.choosing_hero, F.data.startswith("gen:hero:"))
async def choose_hero(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    element = data["_hero_list"][idx]
    await state.update_data(hero={"module": "Баннеры", "element": element})
    await callback.message.edit_text(f"Баннеры: {element}")
    await _ask_footer(callback.message, state)
    await callback.answer()


async def _ask_footer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    branch = data["branch"]
    options = gensvc.footer_options(branch)

    if not options:
        await message.answer(
            f"Для филиала «{branch}» нет системного компонента Подвалы в библиотеке. "
            "Генерация невозможна без компонента Подвалы. Отправьте /cancel."
        )
        return

    await state.update_data(_footer_list=options, step="choosing_footer")
    await state.set_state(LetterGenStates.choosing_footer)
    await message.answer("Выберите компонент Подвалы:", reply_markup=_idx_keyboard(options, "gen:footer"))


@router.callback_query(LetterGenStates.choosing_footer, F.data.startswith("gen:footer:"))
async def choose_footer(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(callback.data.split(":")[-1])
    element = data["_footer_list"][idx]
    await state.update_data(footer={"module": "Подвалы", "element": element})
    await callback.message.edit_text(f"Подвалы: {element}")
    await _ask_component_menu(callback.message, state)
    await callback.answer()


# --- контентные компоненты (цикл) -------------------------------------------------
#
# Текущий режим: после обязательных компонентов показываются две кнопки —
# «Добавить компонент» / «Завершить и скачать файл». Название компонента при
# добавлении вводится текстом и ищется по manifest.json (gensvc.find_content_component),
# после чего запускается тот же single-field flow, что и раньше. См. _ask_component_menu
# и _add_component_by_name ниже.

async def _ask_component_menu(message: Message, state: FSMContext) -> None:
    await state.update_data(step="choosing_add_more")
    await state.set_state(LetterGenStates.choosing_add_more)
    await message.answer(
        "Контентные компоненты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Добавить компонент", callback_data="gen:addcomp")],
            [InlineKeyboardButton(text="Завершить и скачать файл", callback_data="gen:finish")],
        ]),
    )


@router.callback_query(LetterGenStates.choosing_add_more, F.data == "gen:addcomp")
async def add_component_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(step="waiting_component_name")
    await state.set_state(LetterGenStates.waiting_component_name)
    await callback.message.edit_text("Добавление компонента.")
    if settings.catalog_url:
        await callback.message.answer(
            "Каталог компонентов — все варианты с превью, открывается в браузере:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть каталог компонентов", url=settings.catalog_url),
            ]]),
        )
    await callback.message.answer(
        "Введите название компонента в формате «Модуль / Компонент», как в каталоге.",
        reply_markup=cancel_keyboard,
    )
    await callback.answer()


@router.message(LetterGenStates.waiting_component_name, F.text == "Отмена")
@router.message(LetterGenStates.waiting_component_name, Command("cancel"))
async def cancel_add_component(message: Message, state: FSMContext) -> None:
    """Кнопка «Отмена»/команда /cancel в этом состоянии отменяют только ввод названия
    компонента и возвращают в меню «Добавить компонент» / «Завершить и скачать файл»,
    а не сбрасывают всю сборку письма (в отличие от глобального /cancel в contract.py)."""
    await message.answer("Добавление компонента отменено.")
    await _ask_component_menu(message, state)


@router.message(LetterGenStates.waiting_component_name, F.text)
async def receive_component_name(message: Message, state: FSMContext) -> None:
    await _add_component_by_name(message, state, message.text.strip())


async def _add_component_by_name(message: Message, state: FSMContext, name: str) -> None:
    found = gensvc.find_content_component(name)
    if found is None:
        await message.answer(
            "Компонент с таким названием не найден. Введите название точно как в "
            "каталоге (manifest.json), или отправьте /cancel."
        )
        return

    module, element = found
    count = gensvc.sample_text_count(module, element)
    await state.update_data(_current_module=module, _current_element=element, _current_text_count=count)
    await message.answer(f"Компонент: {module} / {element}")

    if count == 0:
        await _content_added(message, state, module, element, [])
        return

    single = component_fields.get_single_field(module, element)
    if single is not None:
        # Упрощённый режим (текущий, действующий): одно поле «заголовок/основная
        # мысль блока» на компонент — см. mail_project/COMPONENT_SMM_FIELD_MAPPING.md
        # и services/component_fields.SINGLE_FIELD_INDEX. Остальные позиции
        # sample_text остаются demo-текстом библиотеки, введённое значение
        # подставляется только в выбранный индекс.
        field_index, field_label = single
        demo_texts = gensvc.sample_texts(module, element)
        await state.update_data(
            _single_field_index=field_index,
            _demo_texts=demo_texts,
            step="waiting_single_field_value",
        )
        await state.set_state(LetterGenStates.waiting_single_field_value)
        await message.answer(
            f"<b>{html.escape(field_label)}</b>\nНапишите {field_label[0].lower()}{field_label[1:]}:",
            reply_markup=cancel_keyboard,
        )
        return

    # Безопасный откат: для компонентов без запланированного единственного поля
    # (не должно происходить для count > 0 — SINGLE_FIELD_INDEX покрывает все
    # такие компоненты, оставлено на случай несовпадения) — старый режим
    # «N строк одним сообщением», без придуманных названий полей.
    await state.update_data(step="waiting_content_text")
    await state.set_state(LetterGenStates.waiting_content_text)
    await message.answer(
        f"Введите содержимое компонента — {count} {_plural_stroki(count)}, каждая с новой строки.",
        reply_markup=cancel_keyboard,
    )


# ===================================================================================
# СТАРЫЙ flow «выбор модуля -> выбор элемента» кнопками (заменён вводом названия
# компонента текстом выше) — оставлен закомментированным для быстрого возврата:
# раскомментировать, вернуть вызов _ask_content_module(callback.message, state) вместо
# _ask_component_menu(callback.message, state) в choose_footer выше, и вернуть вызов
# _ask_content_module(callback.message, state) вместо _ask_component_menu(callback.message,
# state) в add_more_yes ниже.
# ===================================================================================
#
# async def _ask_content_module(message: Message, state: FSMContext) -> None:
#     await state.update_data(step="choosing_content_module")
#     await state.set_state(LetterGenStates.choosing_content_module)
#     await message.answer(
#         "Выберите компонент для содержимого:",
#         reply_markup=_idx_keyboard(gensvc.CONTENT_MODULES, "gen:module"),
#     )
#
#
# @router.callback_query(LetterGenStates.choosing_content_module, F.data.startswith("gen:module:"))
# async def choose_content_module(callback: CallbackQuery, state: FSMContext) -> None:
#     idx = int(callback.data.split(":")[-1])
#     module = gensvc.CONTENT_MODULES[idx]
#     elements = gensvc.content_elements(module)
#
#     await state.update_data(_current_module=module, _element_list=elements, step="choosing_content_element")
#     await state.set_state(LetterGenStates.choosing_content_element)
#     await callback.message.edit_text(f"Модуль: {module}")
#     await callback.message.answer("Выберите элемент:", reply_markup=_idx_keyboard(elements, "gen:element"))
#     await callback.answer()
#
#
# @router.callback_query(LetterGenStates.choosing_content_element, F.data.startswith("gen:element:"))
# async def choose_content_element(callback: CallbackQuery, state: FSMContext) -> None:
#     data = await state.get_data()
#     idx = int(callback.data.split(":")[-1])
#     module = data["_current_module"]
#     element = data["_element_list"][idx]
#     count = gensvc.sample_text_count(module, element)
#
#     await state.update_data(_current_element=element, _current_text_count=count)
#     await callback.message.edit_text(f"Компонент: {module} / {element}")
#
#     if count == 0:
#         await _content_added(callback.message, state, module, element, [])
#         await callback.answer()
#         return
#
#     single = component_fields.get_single_field(module, element)
#     if single is not None:
#         field_index, field_label = single
#         demo_texts = gensvc.sample_texts(module, element)
#         await state.update_data(
#             _single_field_index=field_index,
#             _demo_texts=demo_texts,
#             step="waiting_single_field_value",
#         )
#         await state.set_state(LetterGenStates.waiting_single_field_value)
#         await callback.message.answer(
#             f"<b>{html.escape(field_label)}</b>\nНапишите {field_label[0].lower()}{field_label[1:]}:",
#             reply_markup=cancel_keyboard,
#         )
#         await callback.answer()
#         return
#
#     await state.update_data(step="waiting_content_text")
#     await state.set_state(LetterGenStates.waiting_content_text)
#     await callback.message.answer(
#         f"Введите содержимое компонента — {count} {_plural_stroki(count)}, каждая с новой строки.",
#         reply_markup=cancel_keyboard,
#     )
#     await callback.answer()


@router.message(LetterGenStates.waiting_single_field_value, F.text == "Отмена")
@router.message(LetterGenStates.waiting_single_field_value, Command("cancel"))
async def cancel_waiting_single_field_value(message: Message, state: FSMContext) -> None:
    await _cancel_letter_gen(message, state)


@router.message(LetterGenStates.waiting_single_field_value, F.text)
async def receive_single_field_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    module = data["_current_module"]
    element = data["_current_element"]
    field_index = data["_single_field_index"]
    texts = list(data["_demo_texts"])
    texts[field_index] = message.text
    await _content_added(message, state, module, element, texts)


# ===================================================================================
# СТАРЫЙ многошаговый flow заполнения ВСЕХ полей компонента (по одному вопросу на
# каждую позицию sample_text) — заменён упрощённым режимом «одно поле» выше.
# Оставлен закомментированным для быстрого возврата: раскомментировать этот блок,
# закомментировать/удалить блок с `single = component_fields.get_single_field(...)`
# выше и вернуть в него ветку `confirming_content_fields` вместо `waiting_single_field_value`.
# ===================================================================================
#
# @router.callback_query(LetterGenStates.confirming_content_fields, F.data == "gen:fillstart")
# async def start_field_fill(callback: CallbackQuery, state: FSMContext) -> None:
#     await _ask_next_field(callback.message, state)
#     await callback.answer()
#
#
# async def _ask_next_field(message: Message, state: FSMContext) -> None:
#     data = await state.get_data()
#     labels = data["_field_labels"]
#     values = data.get("_field_values", [])
#     label = labels[len(values)]
#
#     await state.update_data(step="waiting_field_value")
#     await state.set_state(LetterGenStates.waiting_field_value)
#     await message.answer(
#         f"<b>{html.escape(label)}</b>\nНапишите {label[0].lower()}{label[1:]}:",
#         reply_markup=cancel_keyboard,
#     )
#
#
# @router.message(LetterGenStates.waiting_field_value, F.text)
# async def receive_field_value(message: Message, state: FSMContext) -> None:
#     data = await state.get_data()
#     labels = data["_field_labels"]
#     values = data.get("_field_values", [])
#     values.append(message.text)
#     await state.update_data(_field_values=values)
#
#     if len(values) < len(labels):
#         await _ask_next_field(message, state)
#         return
#
#     await _show_fields_review(message, state)
#
#
# async def _show_fields_review(message: Message, state: FSMContext) -> None:
#     data = await state.get_data()
#     module = data["_current_module"]
#     element = data["_current_element"]
#     labels = data["_field_labels"]
#     values = data["_field_values"]
#
#     recap = "\n".join(
#         f"• {html.escape(label)}: {html.escape(value)}" for label, value in zip(labels, values)
#     )
#     await state.update_data(step="reviewing_filled_component")
#     await state.set_state(LetterGenStates.reviewing_filled_component)
#     await message.answer(
#         f"<b>Компонент заполнен</b>\n{html.escape(module)} / {html.escape(element)}\n\n{recap}",
#         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
#             [InlineKeyboardButton(text="Добавить в рассылку", callback_data="gen:fieldsdone:add")],
#             [InlineKeyboardButton(text="Изменить", callback_data="gen:fieldsdone:edit")],
#         ]),
#     )
#
#
# @router.callback_query(LetterGenStates.reviewing_filled_component, F.data == "gen:fieldsdone:add")
# async def fields_review_add(callback: CallbackQuery, state: FSMContext) -> None:
#     data = await state.get_data()
#     module = data["_current_module"]
#     element = data["_current_element"]
#     values = data["_field_values"]
#     await _content_added(callback.message, state, module, element, values)
#     await callback.answer()
#
#
# @router.callback_query(LetterGenStates.reviewing_filled_component, F.data == "gen:fieldsdone:edit")
# async def fields_review_edit(callback: CallbackQuery, state: FSMContext) -> None:
#     await state.update_data(_field_values=[])
#     await callback.message.edit_text("Заполняю компонент заново.")
#     await _ask_next_field(callback.message, state)
#     await callback.answer()


@router.message(LetterGenStates.waiting_content_text, F.text == "Отмена")
@router.message(LetterGenStates.waiting_content_text, Command("cancel"))
async def cancel_waiting_content_text(message: Message, state: FSMContext) -> None:
    await _cancel_letter_gen(message, state)


@router.message(LetterGenStates.waiting_content_text, F.text)
async def receive_content_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    module = data["_current_module"]
    element = data["_current_element"]
    count = data["_current_text_count"]

    lines = message.text.split("\n")
    if len(lines) != count:
        await message.answer(
            f"Нужно ровно {count} {_plural_stroki(count)} (по одной на поле компонента), "
            f"получено {len(lines)}. Попробуйте ещё раз."
        )
        return

    await _content_added(message, state, module, element, lines)


async def _content_added(message: Message, state: FSMContext, module: str, element: str, texts: list) -> None:
    data = await state.get_data()
    blocks = data.get("content_blocks", [])
    blocks.append({"module": module, "element": element, "texts": texts})
    await state.update_data(content_blocks=blocks)
    await message.answer(
        f"Компонент добавлен: {module} / {element}.\n"
        f"Всего контентных компонентов: {len(blocks)}."
    )
    await _ask_component_menu(message, state)


# СТАРЫЙ Да/Нет flow (заменён двумя кнопками «Добавить компонент» / «Завершить и
# скачать файл» в _ask_component_menu выше) — оставлен закомментированным для
# быстрого возврата вместе со старым flow «выбор модуля -> выбор элемента» выше.
#
# @router.callback_query(LetterGenStates.choosing_add_more, F.data == "gen:more:yes")
# async def add_more_yes(callback: CallbackQuery, state: FSMContext) -> None:
#     await callback.message.edit_text("Добавляю ещё один компонент...")
#     await _ask_content_module(callback.message, state)
#     await callback.answer()
#
#
# @router.callback_query(LetterGenStates.choosing_add_more, F.data == "gen:more:no")
# async def add_more_no(callback: CallbackQuery, state: FSMContext) -> None:
#     await callback.message.edit_text("Содержимое собрано.")
#     await callback.message.answer(
#         "Готово к сборке письма.",
#         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
#             InlineKeyboardButton(text="Завершить и скачать", callback_data="gen:finish"),
#         ]]),
#     )
#     await callback.answer()


# --- завершение --------------------------------------------------------------------

@router.callback_query(F.data == "gen:finish")
async def finish_generation(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    build_id = data.get("build_id")
    if not build_id:
        await callback.answer("Сессия устарела, начните заново через «Email-рассылки».", show_alert=True)
        return

    await callback.message.edit_text("Собираю письмо...")

    selection = {
        "header": data.get("header"),
        "hero": data.get("hero"),
        "footer": data.get("footer"),
        "content_blocks": data.get("content_blocks", []),
        "schedule": data.get("schedule_enabled", False),
        "schedule_template": data.get("schedule_template"),
        "subject": data.get("subject"),
    }

    try:
        html_path = await gensvc.generate_email(build_id, selection)
    except gensvc.GenerationServiceError as exc:
        await callback.message.answer(f"Не удалось собрать письмо:\n{exc}")
        await callback.answer()
        return

    html_bytes = html_path.read_bytes()
    await callback.message.answer_document(
        BufferedInputFile(html_bytes, filename=f"{build_id}.html"),
        caption="Готово! Рассылка создана.",
    )
    await callback.message.answer(SMM_INSTRUCTION_TEXT, parse_mode=None)
    await state.clear()
    await callback.message.answer("Выберите действие:", reply_markup=main_menu_keyboard)
    await callback.answer()


# =================================================================================
# 3. Проверка качества письма: пользователь загружает готовый HTML, QA сверяет его
# с актуальной canonical-библиотекой проекта. Не привязан к BUILD_ID/build_dir,
# Build Manager не запускается (существующий алгоритм технических проверок и
# точечных исправлений в mail_project/qa.py не менялся — см. qa.run_standalone()).
# =================================================================================

@router.callback_query(F.data == "mail:qa")
async def start_qa(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(QAStates.waiting_html)
    await callback.message.edit_text("Проверка качества письма.")
    await callback.message.answer(
        "Пришлите HTML-файл письма документом.",
        reply_markup=cancel_keyboard,
    )
    await callback.answer()


@router.message(QAStates.waiting_html, F.document)
async def receive_qa_html(message: Message, state: FSMContext) -> None:
    document = message.document
    file_name = document.file_name or "email.html"
    if not file_name.lower().endswith((".html", ".htm")):
        await message.answer("Нужен файл в формате .html. Пришлите файл ещё раз.")
        return

    file = await message.bot.get_file(document.file_id)
    buf = await message.bot.download_file(file.file_path)
    html_text = buf.read().decode("utf-8", errors="replace")

    if not html_text.strip():
        await message.answer("Файл пустой. Пришлите файл ещё раз.")
        return

    await message.answer("Проверяю письмо...")
    try:
        result = await gensvc.run_qa_on_html(html_text)
    except gensvc.GenerationServiceError as exc:
        await message.answer(
            f"Не удалось выполнить проверку:\n{exc}\n\nПришлите файл ещё раз или отправьте /cancel."
        )
        return

    caption = (
        f"QA завершён: {result['status']}\n"
        f"Дефектов: {result['defects_found']}, исправлено: {result['fixes_applied']}, "
        f"заблокировано: {len(result['blocked'])}"
    )
    report_bytes = json.dumps(
        {k: v for k, v in result.items() if k != "final_html"}, ensure_ascii=False, indent=2
    ).encode("utf-8")

    await message.answer_document(
        BufferedInputFile(result["final_html"].encode("utf-8"), filename="final.html"),
        caption=caption,
    )
    await message.answer_document(
        BufferedInputFile(report_bytes, filename="report.json"),
    )
    await state.clear()
    await message.answer("Готово.", reply_markup=main_menu_keyboard)


@router.message(QAStates.waiting_html)
async def waiting_qa_html_wrong_input(message: Message) -> None:
    await message.answer("Ожидаю HTML-файл документом. Или отправьте /cancel для отмены.")


# =================================================================================
# Админ: удаление старых сборок (builds/<BUILD_ID> старше N дней по дате в BUILD_ID).
# Само удаление — build_manager.py cleanup (subprocess, не дублируется здесь).
# Двухшаговое подтверждение — операция необратимая (сборки удаляются с диска целиком).
# =================================================================================

@router.message(Command("cleanupbuilds"))
async def cmd_cleanup_builds(message: Message) -> None:
    """Скрытая команда — только для админов. Пример: /cleanupbuilds или /cleanupbuilds 14."""
    if not auth_service.is_admin(message.from_user.id):
        return

    args = message.text.split()
    days = CLEANUP_DEFAULT_DAYS
    if len(args) > 1:
        if not args[1].isdigit():
            await message.answer("Использование: /cleanupbuilds [дней] — по умолчанию 7.")
            return
        days = int(args[1])

    try:
        output = await gensvc.preview_old_builds(days)
    except gensvc.GenerationServiceError as exc:
        await message.answer(f"Не удалось получить список сборок:\n{exc}")
        return

    build_names = [line.strip()[2:] for line in output.splitlines() if line.strip().startswith("- ")]
    if not build_names:
        await message.answer(f"Сборок старше {days} дн. не найдено.")
        return

    preview = "\n".join(f"— {name}" for name in build_names[:30])
    if len(build_names) > 30:
        preview += f"\n... и ещё {len(build_names) - 30}"

    await message.answer(
        f"Будет удалено сборок старше {days} дн.: {len(build_names)}\n{preview}\n\n"
        "Удаление необратимо. Подтвердить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Удалить", callback_data=f"cleanup:confirm:{days}"),
            InlineKeyboardButton(text="Отмена", callback_data="cleanup:cancel"),
        ]]),
    )


@router.callback_query(F.data == "cleanup:cancel")
async def cleanup_cancel(callback: CallbackQuery) -> None:
    if not auth_service.is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text("Удаление отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("cleanup:confirm:"))
async def cleanup_confirm(callback: CallbackQuery) -> None:
    if not auth_service.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    days = int(callback.data.split(":")[-1])
    await callback.message.edit_text("Удаляю...")
    try:
        output = await gensvc.cleanup_old_builds(days)
    except gensvc.GenerationServiceError as exc:
        await callback.message.answer(f"Не удалось удалить сборки:\n{exc}")
        await callback.answer()
        return

    summary_line = _find_line(output, "Удалено сборок:") or output.strip()
    await callback.message.answer(summary_line)
    await callback.answer()
