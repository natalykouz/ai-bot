"""Schedule v1: XLSX выгрузка мероприятий -> builds/<BUILD_ID>/schedule/SCHEDULE.txt.

Правила обработки — EVENTS_RULES.md (единственный источник бизнес-правил).
Основной источник базовых ссылок и Заголовка/Подзаголовка мероприятия — Google
Sheets, один HTTP-запрос за запуск, см. load_tour_sheet_data() и переменную
окружения TOUR_LINKS_SHEET_CSV_URL. tour_links.csv/load_tour_links() — локальный
справочник ссылок, оставлен в коде, но в run() больше не вызывается (см. ниже).
"""

import csv
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date as date_cls, datetime, time as time_cls
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

import openpyxl

TOUR_LINKS_FILENAME = "tour_links.csv"
TOUR_LINKS_SHEET_URL_ENV = "TOUR_LINKS_SHEET_CSV_URL"
TOUR_LINKS_SHEET_TIMEOUT = 5

REQUIRED_COLUMNS = [
    "ID события",
    "ID тура",
    "Частная",
    "Дата",
    "Статус",
    "Название",
    "Всего билетов",
    "Осталось",
    "Филиал",
]

MAX_EVENTS_PER_DATE = 20
FALLBACK_URL_TEMPLATE = "https://engineer-history.ru/tour/{tour_id}"

# EVENTS_RULES.md, раздел 6 — числовой код филиала.
BRANCH_UTM_SOURCE_BY_CODE = {
    2: "emailmgi",
    3: "mail_pgi",
    4: "email_kgi",
}

# Реальные выгрузки содержат название города вместо числового кода.
# Соответствие городов кодам — по EVENTS_RULES.md/UTM.md (МГИ=Москва, ПГИ=Санкт-Петербург, КГИ=Казань).
BRANCH_UTM_SOURCE_BY_NAME = {
    "москва": "emailmgi",
    "санкт-петербург": "mail_pgi",
    "казань": "email_kgi",
}


def _resolve_branch_utm_source(value):
    code = _to_int_or_none(value)
    if code in BRANCH_UTM_SOURCE_BY_CODE:
        return BRANCH_UTM_SOURCE_BY_CODE[code]
    if isinstance(value, str):
        name = value.strip().lower()
        if name in BRANCH_UTM_SOURCE_BY_NAME:
            return BRANCH_UTM_SOURCE_BY_NAME[name]
    return None

UNREADABLE_MESSAGE = "Не могу прочитать файл."
INVALID_STRUCTURE_MESSAGE = "Содержимое не соответствует полям выгрузки."


class UnreadableFileError(Exception):
    pass


class InvalidStructureError(Exception):
    pass


def load_tour_links(build_dir: Path) -> dict:
    """Локальный справочник tour_links.csv (module docstring) — не вызывается в run(),
    оставлен для отладки/возврата."""
    tour_links_path = build_dir / "source" / TOUR_LINKS_FILENAME
    if not tour_links_path.is_file():
        return {}

    links = {}
    with tour_links_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tour_id = row.get("ID")
            link = row.get("Ссылка")
            if tour_id and link:
                links[str(tour_id).strip()] = link.strip()
    return links


def _fetch_tour_sheet_rows(url: str | None, timeout: float = TOUR_LINKS_SHEET_TIMEOUT) -> list:
    """Единственный HTTP-запрос к Google Sheets (TSV-экспорт, output=tsv) за один
    запуск генерации расписания — источник и ссылок, и Заголовка/Подзаголовка (см.
    load_tour_sheet_data() ниже). Без Google API и авторизации.

    Любая проблема — не задан URL, сеть недоступна, таймаут, ответ не похож на
    табличные данные (например HTML-страница логина у закрытой таблицы) —
    возвращает [], а не исключение: генерация расписания не должна падать из-за
    недоступности таблицы."""
    if not url:
        return []
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "html" in content_type.lower():
                return []
            raw = resp.read().decode("utf-8-sig")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return []
    try:
        return list(csv.DictReader(raw.splitlines(), delimiter="\t"))
    except csv.Error:
        return []


def load_tour_sheet_data(url: str | None, timeout: float = TOUR_LINKS_SHEET_TIMEOUT) -> tuple:
    """Один запрос к Google Sheets (_fetch_tour_sheet_rows) -> (tour_links, tour_titles),
    сопоставление по `ID` (это `ID тура`, EVENTS_RULES.md, разделы 6-7):

    - tour_links: ID -> Ссылка. Отсутствие записи/URL/таблицы — {} — означает, что
      _build_link() (не меняется) подставит FALLBACK_URL_TEMPLATE для tour_id;
    - tour_titles: ID -> {"title": Заголовок или None, "subtitle": Подзаголовок или
      None} — пустое значение колонки -> None, дальше это решает _build_event()
      (fallback на Название из XLSX / отсутствие подзаголовка)."""
    links = {}
    titles = {}
    for row in _fetch_tour_sheet_rows(url, timeout):
        tour_id = row.get("ID")
        if not tour_id:
            continue
        tour_id = str(tour_id).strip()

        link = row.get("Ссылка")
        if link:
            links[tour_id] = link.strip()

        title = (row.get("Заголовок") or "").strip()
        subtitle = (row.get("Подзаголовок") or "").strip()
        titles[tour_id] = {"title": title or None, "subtitle": subtitle or None}

    return links, titles


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _to_int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _to_number_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _parse_datetime_cell(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date_cls):
        return datetime.combine(value, time_cls(0, 0))
    if isinstance(value, str):
        v = value.strip()
        v = re.sub(r"\s*\([^)]*\)\s*$", "", v).strip()
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
    return None


def load_xlsx_rows(path: Path) -> list:
    try:
        workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:
        raise UnreadableFileError()

    try:
        sheet = workbook.active
        rows_iter = sheet.iter_rows(values_only=True)

        try:
            header_row = next(rows_iter)
        except StopIteration:
            raise InvalidStructureError()

        header = [str(c).strip() if c is not None else "" for c in header_row]
        missing = [col for col in REQUIRED_COLUMNS if col not in header]
        if missing:
            raise InvalidStructureError()

        col_index = {name: header.index(name) for name in REQUIRED_COLUMNS}

        rows = []
        for raw_row in rows_iter:
            if raw_row is None or all(v is None for v in raw_row):
                continue
            row = {
                name: (raw_row[idx] if idx < len(raw_row) else None)
                for name, idx in col_index.items()
            }
            rows.append(row)
        return rows
    except InvalidStructureError:
        raise
    except Exception:
        raise UnreadableFileError()
    finally:
        workbook.close()


def _build_link(tour_id: int, event_id: int, event_date: date_cls, utm_source: str, tour_links: dict) -> str:
    base = tour_links.get(str(tour_id)) or FALLBACK_URL_TEMPLATE.format(tour_id=tour_id)

    parsed = urlparse(base)
    query = parse_qs(parsed.query, keep_blank_values=True)
    has_utm = any(key.startswith("utm_") for key in query)

    new_query = {key: (values[0] if len(values) == 1 else values) for key, values in query.items()}
    new_query["startDate"] = event_date.strftime("%Y-%m-%d")
    new_query["event_id"] = str(event_id)
    if not has_utm:
        new_query["utm_source"] = utm_source

    new_qs = urlencode(new_query, doseq=True)
    return urlunparse(parsed._replace(query=new_qs))


def _build_event(row: dict, tour_links: dict, tour_titles: dict):
    event_id = _to_int_or_none(row.get("ID события"))
    tour_id = _to_int_or_none(row.get("ID тура"))
    dt = _parse_datetime_cell(row.get("Дата"))
    title = row.get("Название")
    status = row.get("Статус")
    private = row.get("Частная")
    total = _to_number_or_none(row.get("Всего билетов"))
    remaining = _to_number_or_none(row.get("Осталось"))

    # Раздел 7: обязательные поля.
    if event_id is None or tour_id is None or dt is None or _is_blank(title):
        return None

    # Раздел 2, п.1.
    if status is None or str(status).strip() != "Active":
        return None

    # Раздел 2, п.2.
    if private is not None and str(private).strip() == "Ч":
        return None

    # Раздел 2, п.3 + Раздел 2, последний абзац.
    if total is None or total == 0 or remaining is None:
        return None
    if not (remaining / total > 0.5):
        return None

    # Раздел 6: другие значения Филиал не обрабатываются в этом формате.
    utm_source = _resolve_branch_utm_source(row.get("Филиал"))
    if utm_source is None:
        return None

    link = _build_link(tour_id, event_id, dt.date(), utm_source, tour_links)

    # Заголовок/Подзаголовок из Google Sheets (сопоставление по ID тура, см.
    # load_tour_sheet_data). Название из XLSX остаётся источником для
    # проверки обязательности поля выше и fallback-значением отображаемого
    # названия — здесь никакого автоматического разбиения XLSX-названия на
    # заголовок/подзаголовок не производится, только опциональная подмена
    # целиком данными из таблицы.
    sheet_meta = tour_titles.get(str(tour_id)) or {}
    display_title = sheet_meta.get("title") or str(title).strip()
    subtitle = sheet_meta.get("subtitle")

    return {
        "event_id": event_id,
        "tour_id": tour_id,
        "date": dt.date(),
        "time": dt.time(),
        "title": display_title,
        "subtitle": subtitle,
        "remaining": remaining,
        "link": link,
    }


def filter_events(rows: list, tour_links: dict, tour_titles: dict) -> list:
    events = []
    for row in rows:
        event = _build_event(row, tour_links, tour_titles)
        if event is not None:
            events.append(event)
    return events


def group_and_limit(events: list) -> list:
    by_date = defaultdict(list)
    for event in events:
        by_date[event["date"]].append(event)

    result = []
    for event_date in sorted(by_date.keys()):
        day_events = by_date[event_date]
        if len(day_events) > MAX_EVENTS_PER_DATE:
            day_events = sorted(day_events, key=lambda e: (-e["remaining"], e["time"]))[:MAX_EVENTS_PER_DATE]
        day_events = sorted(day_events, key=lambda e: e["time"])
        result.append((event_date, day_events))
    return result


def render_schedule(grouped: list) -> str:
    lines = ["SCHEDULE", ""]
    for event_date, day_events in grouped:
        lines.append(f"DATE: {event_date.strftime('%d.%m.%Y')}")
        lines.append("")
        for event in day_events:
            lines.append("EVENT")
            lines.append(f"event_id: {event['event_id']}")
            lines.append(f"tour_id: {event['tour_id']}")
            lines.append(f"time: {event['time'].strftime('%H:%M')}")
            lines.append(f"title: {event['title']}")
            if event.get("subtitle"):
                lines.append(f"subtitle: {event['subtitle']}")
            lines.append(f"link: {event['link']}")
            lines.append("")

    if lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines) + "\n"


def run(build_dir: Path) -> None:
    input_dir = build_dir / "input"
    xlsx_files = sorted(input_dir.glob("*.xlsx")) if input_dir.exists() else []

    if not xlsx_files:
        print(UNREADABLE_MESSAGE)
        sys.exit(1)

    if len(xlsx_files) > 1:
        names = ", ".join(f.name for f in xlsx_files)
        print(f"Ошибка: в input/ найдено больше одного XLSX-файла ({names}). Оставьте один файл выгрузки.")
        sys.exit(1)

    xlsx_path = xlsx_files[0]

    try:
        rows = load_xlsx_rows(xlsx_path)
    except UnreadableFileError:
        print(UNREADABLE_MESSAGE)
        sys.exit(1)
    except InvalidStructureError:
        print(INVALID_STRUCTURE_MESSAGE)
        sys.exit(1)

    tour_links, tour_titles = load_tour_sheet_data(os.getenv(TOUR_LINKS_SHEET_URL_ENV))
    events = filter_events(rows, tour_links, tour_titles)
    grouped = group_and_limit(events)
    schedule_text = render_schedule(grouped)

    schedule_dir = build_dir / "schedule"
    schedule_dir.mkdir(parents=True, exist_ok=True)
    schedule_path = schedule_dir / "SCHEDULE.txt"
    schedule_path.write_text(schedule_text, encoding="utf-8")

    total_events = sum(len(day_events) for _, day_events in grouped)
    print(f"SCHEDULE.txt создан: {schedule_path}")
    print(f"  Дат: {len(grouped)}, событий: {total_events}")
