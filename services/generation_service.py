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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIL_PROJECT_DIR = PROJECT_ROOT / "mail_project"
BUILD_MANAGER_PY = MAIL_PROJECT_DIR / "build_manager.py"
MANIFEST_PATH = MAIL_PROJECT_DIR / "manifest.json"
BUILDS_DIR = MAIL_PROJECT_DIR / "builds"

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


def content_elements(module: str) -> list:
    return _elements_for(module)


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


async def run_schedule(build_id: str) -> str:
    """Запускает Schedule v1 (build_manager.py schedule) над XLSX, уже добавленным через add_input()."""
    return await _run_build_manager("schedule", build_id)


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
