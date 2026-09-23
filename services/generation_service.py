"""
Сервисный слой для ветки «Генерация рассылки».

Оборачивает существующие mail_project/build_manager.py (запускается как CLI —
subprocess, файл не меняется), mail_project/generation.py (импортируется напрямую
для чтения manifest.json и вызова run_selected()) и mail_project/qa.py
(импортируется напрямую для run_standalone() — QA произвольного HTML без BUILD_ID).

Telegram-хендлеры (handlers/generation.py) используют только функции этого
модуля и не содержат логики Build Manager / Generation.
"""

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIL_PROJECT_DIR = PROJECT_ROOT / "mail_project"
BUILD_MANAGER_PY = MAIL_PROJECT_DIR / "build_manager.py"
MANIFEST_PATH = MAIL_PROJECT_DIR / "manifest.json"
BUILDS_DIR = MAIL_PROJECT_DIR / "builds"
RESULTS_SEQ_DIR = MAIL_PROJECT_DIR / "results" / "_sequence"

if str(MAIL_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(MAIL_PROJECT_DIR))

import generation  # noqa: E402  (mail_project/generation.py — существующий Generation engine)
import qa  # noqa: E402  (mail_project/qa.py — существующий QA engine)

UNISENDER_ZIP_PATH = MAIL_PROJECT_DIR / "unisender_components.zip"


class GenerationServiceError(Exception):
    """Ошибка любого этапа Build Manager / Generation — текст пригоден для показа пользователю."""


# Модули manifest.json, доступные для контентных компонентов шага 4.
# Шапки/Подвалы — отдельные шаги (Header/Footer), Мероприятия — обрабатываются Schedule v1.
CONTENT_MODULES = [
    "Баннеры",
    "Дополнительные Баннеры",
    "Изображения",
    "Текстовые блоки",
    "Кнопки",
    "Разделители",
    "Авторы",
    "Большие контентные блоки",
    "Контентные блоки",
]


def _load_manifest() -> list:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _elements_for(module: str, folder: str | None = None) -> list:
    result = []
    for entry in _load_manifest():
        if entry["module"] != module:
            continue
        if folder is not None and not entry["element"].startswith(f"{folder} "):
            continue
        result.append(entry["element"])
    return result


def branches() -> list:
    """Допустимые филиалы — реестр уже зафиксирован в generation.BRANCH_REGISTRY."""
    return list(generation.BRANCH_REGISTRY.keys())


def header_options(branch: str) -> list:
    folder = generation.BRANCH_REGISTRY[branch]["header_folder"]
    if not folder:
        return []
    return _elements_for("Шапки", folder)


def footer_options(branch: str) -> list:
    folder = generation.BRANCH_REGISTRY[branch]["footer_folder"]
    return _elements_for("Подвалы", folder)


def hero_options() -> list:
    return _elements_for("Баннеры")


def schedule_template_options() -> list:
    """Варианты canonical-шаблона расписания (модуль «Мероприятия», элементы
    «Расписание N») — выбор шаблона в шаге после загрузки SCHEDULE.txt."""
    return _elements_for("Мероприятия", "Расписание")


def content_elements(module: str) -> list:
    return _elements_for(module)


def find_content_component(name: str) -> tuple | None:
    """Ищет контентный компонент по названию среди CONTENT_MODULES (Шапки/Баннеры/
    Подвалы/Мероприятия сюда не входят — у них отдельные шаги выбора). Используется
    вводом названия компонента текстом при добавлении.

    Принимает как полный вид «Модуль / Компонент» (именно так подписаны блоки в
    каталоге и копируются пользователем), так и просто «Компонент» без модуля."""
    name = name.strip()
    module_part: str | None = None
    element_part = name
    if " / " in name:
        module_part, _, element_part = name.partition(" / ")
        module_part = module_part.strip()
        element_part = element_part.strip()

    for entry in _load_manifest():
        if entry["module"] not in CONTENT_MODULES:
            continue
        if module_part is not None and entry["module"] != module_part:
            continue
        if entry["element"] == element_part:
            return entry["module"], entry["element"]
    return None


def sample_text_count(module: str, element: str) -> int:
    for entry in _load_manifest():
        if entry["module"] == module and entry["element"] == element:
            return len(entry["sample_text"])
    raise GenerationServiceError(f"Компонент отсутствует в manifest.json: {module}/{element}")


def sample_texts(module: str, element: str) -> list:
    """Полный demo-текст компонента по порядку sample_text — используется
    упрощённым режимом ввода (одно поле СММ, остальные позиции остаются
    demo-контентом библиотеки, см. services/component_fields.get_single_field)."""
    for entry in _load_manifest():
        if entry["module"] == module and entry["element"] == element:
            return list(entry["sample_text"])
    raise GenerationServiceError(f"Компонент отсутствует в manifest.json: {module}/{element}")


# --- Build Manager (существующий CLI — mail_project/build_manager.py) ----------

async def _run_build_manager(*args: str) -> str:
    # На Windows дочерний процесс без явного PYTHONIOENCODING пишет в pipe в системной
    # кодировке консоли (cp1251/cp866), а не в UTF-8 — из-за этого кириллица в выводе
    # build_manager.py (например "Build создан: ...") приходит битой при decode("utf-8").
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(BUILD_MANAGER_PY), *args,
        cwd=str(MAIL_PROJECT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise GenerationServiceError(output.strip() or f"build_manager.py {' '.join(args)} завершился с ошибкой")
    return output


async def create_build() -> str:
    """Создаёт новый BUILD_ID через build_manager.py create. Единственная точка создания BUILD_ID."""
    output = await _run_build_manager("create")
    for line in output.splitlines():
        if line.startswith("Build создан:"):
            return line.split(":", 1)[1].strip()
    raise GenerationServiceError("Не удалось определить BUILD_ID из вывода build_manager.py create:\n" + output)


async def add_input(build_id: str, file_path: Path) -> None:
    await _run_build_manager("add-input", build_id, str(file_path))


async def run_schedule(build_id: str, filter_mode: str, threshold: float) -> str:
    """Запускает Schedule v1 (build_manager.py schedule) над XLSX, уже добавленным через add_input().
    filter_mode/threshold — режим и порог отбора события по билетам, указанные
    СММ (см. mail_project/schedule_processor.py FILTER_MODES). Безопасно
    вызывать повторно на том же build_id — XLSX остаётся в input/, Google Sheets
    перечитывается заново при каждом вызове (используется для «Перегенерировать
    расписание», см. handlers/generation.py); режим и порог при этом передаются
    те же, что были указаны при первом запуске."""
    return await _run_build_manager(
        "schedule", build_id, "--filter-mode", filter_mode, "--threshold", str(threshold)
    )


def missing_events_from_output(output: str) -> list:
    """Разбирает строку `MISSING_IN_SHEET_JSON: [...]` из stdout run_schedule() —
    мероприятия расписания, чей ID тура не найден в Google Sheets листе своего
    филиала (schedule_processor.filter_events()). [] если строка не найдена
    или список пуст."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("MISSING_IN_SHEET_JSON:"):
            payload = line.split(":", 1)[1].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return []
    return []


async def verify_build(build_id: str) -> None:
    await _run_build_manager("verify", build_id)


async def run_qa_on_html(html_text: str) -> dict:
    """QA v1 для произвольного загруженного HTML — не привязан к BUILD_ID/build_dir и не
    вызывает Build Manager. Canonical-источники — актуальные mail_project/manifest.json
    и mail_project/unisender_components.zip проекта (не source set какой-либо сборки)."""

    def _run() -> dict:
        try:
            return qa.run_standalone(html_text, MANIFEST_PATH, UNISENDER_ZIP_PATH)
        except qa.QABlocked as exc:
            raise GenerationServiceError(str(exc)) from exc

    return await asyncio.to_thread(_run)


def is_full_unisender_template(html_text: str) -> bool:
    """Входная проверка для сценария «заменить Schedule в загруженном письме» (этап 1):
    отличает UniSender-шаблон, собранный из распознаваемых UniSender-блоков, от
    произвольного HTML или одиночного вырванного компонента. Состав блоков не
    фиксирован — наличие конкретных модулей (например Шапки/Подвалы) не требуется,
    их пользователь может добавлять/заменять отдельно.

    Признаки блочной структуры UniSender (все три обязательны хотя бы у одного
    top-level блока):
    - документ целиком, не фрагмент — есть тег <html>;
    - есть <tr em="block" ... letteros-element="..." letteros-module="...">;
    - границы блока корректно выделяются с учётом вложенных <tr> (тот же парсер,
      что и у QA: qa.find_components -> generation._scan_balanced_tr_end)."""
    if "<html" not in html_text.lower():
        return False
    components = qa.find_components(html_text)
    for _module, _element, start, end in components:
        tag_end = html_text.find(">", start)
        if tag_end != -1 and 'em="block"' in html_text[start:tag_end]:
            return True
    return False


_SCHEDULE_MODULE = "Мероприятия"
_DIVIDER_MODULE = "Разделители"


def schedule_blocks_status(html_text: str) -> str:
    """Продолжение входной проверки для сценария «заменить Schedule в загруженном
    письме» (этап 1, вызывается после is_full_unisender_template) — не про саму
    замену, только про то, что и где менять можно однозначно.

    Schedule-блок — top-level блок с letteros-module="Мероприятия" и
    letteros-element, начинающимся с "Расписание" (тот же список top-level
    блоков, что и в is_full_unisender_template: qa.find_components).

    Возвращает:
    - "missing" — в шаблоне нет ни одного Schedule-блока;
    - "not_contiguous" — Schedule-блоки есть, но между первым и последним
      найден другой top-level блок, который не является визуальным
      разделителем (letteros-module="Разделители" — отступ/линия допускаются
      между Schedule-блоками, любой другой модуль — нет);
    - "ok" — один или несколько Schedule-блоков идут подряд (с разделителями
      или без), однозначно заменяемы."""
    components = qa.find_components(html_text)
    schedule_idx = [
        i for i, (module, element, _start, _end) in enumerate(components)
        if module == _SCHEDULE_MODULE and element.startswith("Расписание")
    ]
    if not schedule_idx:
        return "missing"

    first_idx, last_idx = schedule_idx[0], schedule_idx[-1]
    for i in range(first_idx, last_idx + 1):
        if i in schedule_idx:
            continue
        module, _element, _start, _end = components[i]
        if module != _DIVIDER_MODULE:
            return "not_contiguous"
    return "ok"


def find_schedule_region(html_text: str):
    """«Заменить расписание в готовом письме», этап 2 — та же логика поиска и
    проверки смежности Schedule-блоков, что и в schedule_blocks_status() (эта
    функция не меняется), но дополнительно возвращает точные границы старого
    Schedule-региона для физической замены. Отдельная функция с продублированным
    отбором/проверкой смежности — чтобы не трогать уже проверенный код
    schedule_blocks_status().

    Возвращает (status, region):
    - status — тот же набор значений, что и у schedule_blocks_status
      ("missing"/"not_contiguous"/"ok");
    - region — при status == "ok" кортеж (start, end, element): start/end —
      границы региона в html_text от начала первого Schedule-блока до конца
      последнего (включая допустимые между ними Разделители), element —
      letteros-element первого найденного Schedule-блока (тот же
      canonical-вариант Расписания используется для новых блоков, чтобы
      сохранить визуал письма); при любом другом status region — None."""
    components = qa.find_components(html_text)
    schedule_idx = [
        i for i, (module, element, _start, _end) in enumerate(components)
        if module == _SCHEDULE_MODULE and element.startswith("Расписание")
    ]
    if not schedule_idx:
        return "missing", None

    first_idx, last_idx = schedule_idx[0], schedule_idx[-1]
    for i in range(first_idx, last_idx + 1):
        if i in schedule_idx:
            continue
        module, _element, _start, _end = components[i]
        if module != _DIVIDER_MODULE:
            return "not_contiguous", None

    region_start = components[first_idx][2]
    region_end = components[last_idx][3]
    first_element = components[first_idx][1]
    return "ok", (region_start, region_end, first_element)


async def build_schedule_replacement_blocks(schedule_txt_content: bytes, element: str | None = None) -> list:
    """«Заменить расписание в готовом письме», этап 2 — строит новые Schedule-блоки
    из загруженного SCHEDULE.txt. Переиспользует существующий Schedule-генератор
    без изменений: generation._parse_schedule_txt() (тот же парсер, что и
    build_schedule_fish()) и generation.build_schedule_blocks() (тот же
    генератор canonical Schedule, что и во всех остальных Schedule-flow) —
    HTML-логика Schedule не дублируется и не переписывается.

    Без BUILD_ID/Build Manager — как и run_qa_on_html(), на актуальных
    mail_project/manifest.json + unisender_components.zip проекта. element —
    canonical-вариант Расписания (например "Расписание 3"), уже использованный
    в загруженном письме (см. find_schedule_region); также используется
    flow «Добавить расписание в готовое письмо», где в письме ещё нет ни одного
    Schedule-блока и element передаётся None — тогда generation.build_schedule_blocks()
    сама берёт canonical-вариант по умолчанию (SCHEDULE_TEMPLATE["element"])."""

    def _run() -> list:
        with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as tmp:
            tmp.write(schedule_txt_content)
            tmp_path = Path(tmp.name)
        try:
            groups = generation._parse_schedule_txt(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        if not groups:
            raise GenerationServiceError("SCHEDULE.txt не содержит ни одного события.")

        library = generation.ComponentLibrary(MANIFEST_PATH, UNISENDER_ZIP_PATH)
        try:
            return generation.build_schedule_blocks(library, groups, element)
        except generation.GenerationError as exc:
            raise GenerationServiceError(str(exc)) from exc

    return await asyncio.to_thread(_run)


def replace_schedule_region(html_text: str, region: tuple, new_blocks: list) -> str:
    """Физическая замена: удаляет старый Schedule-регион (region — из
    find_schedule_region) целиком и вставляет новые блоки на его место (то есть
    на место первого старого Schedule-блока) — без изменения остального HTML."""
    start, end, _element = region
    return html_text[:start] + "\n".join(new_blocks) + html_text[end:]


def append_schedule_blocks(html_text: str, new_blocks: list) -> str:
    """«Добавить расписание в готовое письмо» — вставляет новые Schedule-блоки в
    конец письма, сразу после последнего top-level UniSender-блока (тот же
    список блоков, что у is_full_unisender_template/schedule_blocks_status:
    qa.find_components). Порядок уже существующих в письме блоков не меняется;
    UniSender позволяет пользователю после импорта перетащить блоки в нужное
    место самостоятельно — бот порядок не переставляет."""
    components = qa.find_components(html_text)
    insert_at = components[-1][3]
    return html_text[:insert_at] + "\n" + "\n".join(new_blocks) + html_text[insert_at:]


# --- Единое именование трёх результатов Email-flow (Расписание/РасписаниеПодложка/
# ПисьмоСРасписанием) — "[Филиал_]ТипРезультата_YYYYMMDD-NNN.расширение". ------------

RESULT_TYPE_SCHEDULE = "Расписание"
RESULT_TYPE_SCHEDULE_FISH = "РасписаниеПодложка"
RESULT_TYPE_LETTER_WITH_SCHEDULE = "ПисьмоСРасписанием"


def _next_result_seq(today: str) -> int:
    """Единый дневной счётчик NNN — общий на все три типа результатов, а не
    отдельный на каждый (иначе имена совпадали бы при выдаче разных типов в один
    день). Тот же принцип, что у build_manager.next_build_id() (скан каталога на
    маркеры вида "<today>-NNN", max+1) — отдельный каталог, не builds/: одна
    выдача результата не обязана совпадать с созданием build'а (например,
    расписание можно перегенерировать несколько раз в одном build — несколько
    результатов на один BUILD_ID), а build_manager.py как CLI-скрипт (subprocess,
    см. модульный docstring) напрямую не импортируется и не меняется."""
    RESULTS_SEQ_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    for p in RESULTS_SEQ_DIR.iterdir():
        if p.is_file() and p.name.startswith(f"{today}-"):
            suffix = p.name.split("-", 1)[1]
            if suffix.isdigit():
                existing.append(int(suffix))
    n = max(existing, default=0) + 1
    (RESULTS_SEQ_DIR / f"{today}-{n:03d}").touch()
    return n


def next_result_filename(result_type: str, extension: str, branch: str | None = None) -> str:
    """Единое имя файла для одного из трёх результатов Email-flow — формат
    "[Филиал_]ТипРезультата_YYYYMMDD-NNN.расширение" (result_type — одна из
    RESULT_TYPE_* констант). branch подставляется в начало имени только если
    передан явно — в конкретном flow филиал может быть надёжно недоступен
    (например, «Генерация расписания» может смешивать события нескольких
    филиалов в одном SCHEDULE.txt), тогда вызывающий код передаёт None и имя
    начинается сразу с типа результата. NNN — общий на все три типа счётчик за
    текущую дату (_next_result_seq), поэтому повторная выдача любого из трёх
    типов в один день получает разные NNN и имена не повторяются."""
    today = datetime.now().strftime("%Y%m%d")
    n = _next_result_seq(today)
    prefix = f"{branch}_" if branch else ""
    return f"{prefix}{result_type}_{today}-{n:03d}.{extension}"


def fish_branch_options() -> list:
    """Филиалы, для которых есть отдельный Editor Template («HTML-основа письма»,
    см. generation.EDITOR_TEMPLATE_PATHS) — фиксированный список из трёх филиалов,
    независимый от generation.BRANCH_REGISTRY обычной Генерации письма."""
    return list(generation.EDITOR_TEMPLATE_PATHS.keys())


async def build_schedule_fish(build_id: str, element: str, branch: str) -> Path:
    """ЭТАП 3: «HTML-основа письма» — только Schedule-блоки уже сформированного
    расписания build'а (SCHEDULE.txt), обёрнутые в Editor Template выбранного
    филиала. Переиспользует generation.build_schedule_fish() (сам parsing/rendering
    Schedule не дублируется, см. mail_project/generation.py)."""
    await verify_build(build_id)
    build_dir = build_dir_for(build_id)

    def _run() -> Path:
        try:
            fish_html = generation.build_schedule_fish(build_dir, element, branch)
        except generation.GenerationError as exc:
            raise GenerationServiceError(str(exc)) from exc
        generation_dir = build_dir / "generation"
        generation_dir.mkdir(parents=True, exist_ok=True)
        out_path = generation_dir / "email_base.html"
        out_path.write_text(fish_html, encoding="utf-8")
        return out_path

    return await asyncio.to_thread(_run)


def build_dir_for(build_id: str) -> Path:
    return BUILDS_DIR / build_id


async def preview_old_builds(days: int) -> str:
    """Список сборок старше days дней (build_manager.py cleanup --dry-run) — ничего не удаляет."""
    return await _run_build_manager("cleanup", "--days", str(days), "--dry-run")


async def cleanup_old_builds(days: int) -> str:
    """Безвозвратно удаляет сборки старше days дней (build_manager.py cleanup)."""
    return await _run_build_manager("cleanup", "--days", str(days))


def save_schedule_file(build_id: str, content: bytes) -> None:
    """Кладёт уже готовый SCHEDULE.txt в build — без запуска Schedule processor.
    Используется в Generation flow, когда расписание собрано заранее отдельным
    flow «Генерация расписания» (build_manager.py/schedule_processor.py не меняются
    и не дублируются — просто перекладывается уже собранный артефакт)."""
    if not content.strip():
        raise GenerationServiceError("Файл SCHEDULE.txt пуст.")
    build_dir = build_dir_for(build_id)
    if not build_dir.exists():
        raise GenerationServiceError(f"Сборка {build_id} не найдена.")
    schedule_dir = build_dir / "schedule"
    schedule_dir.mkdir(parents=True, exist_ok=True)
    (schedule_dir / "SCHEDULE.txt").write_bytes(content)


# --- Generation engine (mail_project/generation.py, импорт в процессе) ---------

async def generate_email(build_id: str, selection: dict) -> Path:
    """Проверяет целостность build и собирает email.html через generation.run_selected()."""
    await verify_build(build_id)
    build_dir = build_dir_for(build_id)

    def _run() -> Path:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                generation.run_selected(build_dir, selection)
        except SystemExit as exc:
            raise GenerationServiceError(buf.getvalue().strip() or "Generation остановлена.") from exc
        return build_dir / "generation" / "email.html"

    return await asyncio.to_thread(_run)
