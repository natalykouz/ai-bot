"""Build Manager v1.

Фиксирует production source set проекта под уникальным BUILD_ID.
Правила см. в BUILD_MANAGER.md.

Команды:
    python build_manager.py create
    python build_manager.py add-input <BUILD_ID> <file> [<file> ...]
    python build_manager.py verify <BUILD_ID>
    python build_manager.py schedule <BUILD_ID>
    python build_manager.py generate <BUILD_ID> <content_file>
    python build_manager.py qa <BUILD_ID>
    python build_manager.py cleanup [--days N] [--dry-run]
"""

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import generation
import qa
import schedule_processor

BASE_DIR = Path(__file__).resolve().parent
SOURCES_CONFIG = BASE_DIR / "build_manager.sources.txt"
BUILDS_DIR = BASE_DIR / "builds"


def load_source_set() -> list[str]:
    if not SOURCES_CONFIG.exists():
        print(f"Ошибка: конфигурационный файл не найден: {SOURCES_CONFIG}")
        sys.exit(1)

    paths = []
    for line in SOURCES_CONFIG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        paths.append(line)
    return paths


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def next_build_id() -> str:
    today = datetime.now().strftime("%Y%m%d")
    existing = []
    if BUILDS_DIR.exists():
        for d in BUILDS_DIR.iterdir():
            if d.is_dir() and d.name.startswith(f"{today}-"):
                suffix = d.name.split("-", 1)[1]
                if suffix.isdigit():
                    existing.append(int(suffix))
    n = max(existing, default=0) + 1
    return f"{today}-{n:03d}"


def cmd_create(args: argparse.Namespace) -> None:
    relative_paths = load_source_set()

    missing = [p for p in relative_paths if not (BASE_DIR / p).is_file()]
    if missing:
        print("Ошибка: сборка не создана — отсутствуют актуальные файлы source set:")
        for p in missing:
            print(f"  - {p}")
        sys.exit(1)

    build_id = next_build_id()
    build_dir = BUILDS_DIR / build_id
    source_dir = build_dir / "source"
    input_dir = build_dir / "input"
    source_dir.mkdir(parents=True)
    input_dir.mkdir(parents=True)

    sources_entries = []
    for rel_path in relative_paths:
        src = BASE_DIR / rel_path
        dst = source_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        sources_entries.append({"path": rel_path, "sha256": sha256_of(dst)})

    build_json = {
        "build_id": build_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "sources": sources_entries,
    }
    (build_dir / "build.json").write_text(
        json.dumps(build_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Build создан: {build_id}")
    print(f"  {build_dir}")
    print(f"  Зафиксировано файлов: {len(sources_entries)}")


def cmd_add_input(args: argparse.Namespace) -> None:
    build_dir = BUILDS_DIR / args.build_id
    input_dir = build_dir / "input"
    if not build_dir.exists():
        print(f"Ошибка: build {args.build_id} не найден в {BUILDS_DIR}")
        sys.exit(1)
    input_dir.mkdir(parents=True, exist_ok=True)

    for file_arg in args.files:
        src = Path(file_arg)
        if not src.is_file():
            print(f"Ошибка: файл не найден: {src}")
            sys.exit(1)
        dst = input_dir / src.name
        shutil.copy2(src, dst)
        print(f"Добавлено в input/: {src.name}")


def cmd_verify(args: argparse.Namespace) -> None:
    build_dir = BUILDS_DIR / args.build_id
    build_json_path = build_dir / "build.json"

    if not build_json_path.is_file():
        print(f"INVALID: {args.build_id}")
        print(f"  Причина: build.json не найден по пути {build_json_path}")
        sys.exit(1)

    data = json.loads(build_json_path.read_text(encoding="utf-8"))
    source_dir = build_dir / "source"

    problems = []
    for entry in data.get("sources", []):
        rel_path = entry["path"]
        expected_hash = entry["sha256"]
        file_path = source_dir / rel_path

        if not file_path.is_file():
            problems.append(f"{rel_path}: файл отсутствует ({file_path})")
            continue

        actual_hash = sha256_of(file_path)
        if actual_hash != expected_hash:
            problems.append(
                f"{rel_path}: SHA-256 не совпадает "
                f"(ожидалось {expected_hash}, получено {actual_hash})"
            )

    if problems:
        print(f"INVALID: {args.build_id}")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    print(f"VALID: {args.build_id}")
    print(f"  Проверено файлов: {len(data.get('sources', []))}")


def cmd_schedule(args: argparse.Namespace) -> None:
    build_dir = BUILDS_DIR / args.build_id
    if not build_dir.exists():
        print(f"Ошибка: build {args.build_id} не найден в {BUILDS_DIR}")
        sys.exit(1)
    schedule_processor.run(build_dir)


def cmd_generate(args: argparse.Namespace) -> None:
    build_dir = BUILDS_DIR / args.build_id
    if not build_dir.exists():
        print(f"Ошибка: build {args.build_id} не найден в {BUILDS_DIR}")
        sys.exit(1)
    cmd_verify(argparse.Namespace(build_id=args.build_id))
    generation.run(build_dir, Path(args.content_file))


def cmd_qa(args: argparse.Namespace) -> None:
    build_dir = BUILDS_DIR / args.build_id
    if not build_dir.exists():
        print(f"Ошибка: build {args.build_id} не найден в {BUILDS_DIR}")
        sys.exit(1)
    cmd_verify(argparse.Namespace(build_id=args.build_id))
    qa.run(build_dir)


def _builds_older_than(days: int) -> list[Path]:
    """BUILD_ID кодирует дату создания в имени (YYYYMMDD-NNN, см. BUILD_MANAGER.md) —
    возраст сборки определяется по этой дате, а не по mtime каталога."""
    if not BUILDS_DIR.exists():
        return []
    cutoff_date = (datetime.now() - timedelta(days=days)).date()
    old = []
    for d in sorted(BUILDS_DIR.iterdir()):
        if not d.is_dir():
            continue
        date_part = d.name.split("-", 1)[0]
        try:
            build_date = datetime.strptime(date_part, "%Y%m%d").date()
        except ValueError:
            continue
        if build_date < cutoff_date:
            old.append(d)
    return old


def cmd_cleanup(args: argparse.Namespace) -> None:
    old_builds = _builds_older_than(args.days)
    if not old_builds:
        print(f"Сборок старше {args.days} дн. не найдено.")
        return

    if args.dry_run:
        print(f"Будет удалено сборок (старше {args.days} дн.): {len(old_builds)}")
        for d in old_builds:
            print(f"  - {d.name}")
        return

    removed = []
    for d in old_builds:
        shutil.rmtree(d)
        removed.append(d.name)

    print(f"Удалено сборок: {len(removed)}")
    for name in removed:
        print(f"  - {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Manager v1")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("create", help="Создать новый build из текущего source set")

    p_add_input = subparsers.add_parser(
        "add-input", help="Добавить входные файлы выпуска в build"
    )
    p_add_input.add_argument("build_id", help="BUILD_ID существующего build")
    p_add_input.add_argument("files", nargs="+", help="Пути к входным файлам")

    p_verify = subparsers.add_parser("verify", help="Проверить целостность build")
    p_verify.add_argument("build_id", help="BUILD_ID для проверки")

    p_schedule = subparsers.add_parser(
        "schedule", help="Обработать XLSX из input/ и сформировать schedule/SCHEDULE.txt"
    )
    p_schedule.add_argument("build_id", help="BUILD_ID существующего build")

    p_generate = subparsers.add_parser(
        "generate", help="Собрать HTML письма и список image-slot'ов из content-файла"
    )
    p_generate.add_argument("build_id", help="BUILD_ID существующего build")
    p_generate.add_argument("content_file", help="Путь к YAML content-файлу текущего выпуска")

    p_qa = subparsers.add_parser(
        "qa", help="Сверить builds/<BUILD_ID>/generation/email.html с canonical-компонентами и точечно исправить"
    )
    p_qa.add_argument("build_id", help="BUILD_ID существующего build")

    p_cleanup = subparsers.add_parser(
        "cleanup", help="Удалить сборки старше N дней (по дате в BUILD_ID)"
    )
    p_cleanup.add_argument("--days", type=int, default=7, help="Порог в днях (по умолчанию 7)")
    p_cleanup.add_argument("--dry-run", action="store_true", help="Только показать, что будет удалено, не удалять")

    args = parser.parse_args()

    if args.command == "create":
        cmd_create(args)
    elif args.command == "add-input":
        cmd_add_input(args)
    elif args.command == "verify":
        cmd_verify(args)
    elif args.command == "schedule":
        cmd_schedule(args)
    elif args.command == "generate":
        cmd_generate(args)
    elif args.command == "qa":
        cmd_qa(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)


if __name__ == "__main__":
    main()
