"""Generation v1: content YAML (+ SCHEDULE.txt) -> canonical HTML письма + список image-slot'ов.

Правила — GENERATION_RULES.md (алгоритм), TEMPLATE_SPEC.md (структура),
CONTENT_SCHEMA.md (входные данные), IMAGE_SLOTS.md (image-slot'ы), UTM.md,
MGI_UNISENDER_ASSETS.md. Компоненты берутся только из зафиксированных в build
manifest.json и unisender_components.zip.

Формат content-файла — минимальное согласованное расширение CONTENT_SCHEMA.md:
добавлены top-level `branch` и необязательный `component`-намёк на hero/card
(см. README проекта/обсуждение). Ничего сверх этого не придумывается.
"""

import base64
import json
import re
import sys
import zipfile
from datetime import datetime, date as date_cls
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

import yaml

WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]
MONTHS_RU = [
    "ЯНВАРЯ", "ФЕВРАЛЯ", "МАРТА", "АПРЕЛЯ", "МАЯ", "ИЮНЯ",
    "ИЮЛЯ", "АВГУСТА", "СЕНТЯБРЯ", "ОКТЯБРЯ", "НОЯБРЯ", "ДЕКАБРЯ",
]

# Соответствие branch -> utm_source (UTM.md) и папка филиала в Шапки/Подвалы.
# Только явно описанные в UTM.md подразделения — остальные не придумываются.
BRANCH_REGISTRY = {
    "Москва глазами инженера": {
        "utm_source": "emailmgi", "header_folder": "Москва", "footer_folder": "Москва",
    },
    "Петербург глазами инженера": {
        "utm_source": "mail_pgi", "header_folder": "Санкт-Петербург", "footer_folder": "Санкт-Петербург",
    },
    "Казань глазами инженера": {
        "utm_source": "email_kgi", "header_folder": "Казань", "footer_folder": "Казань",
    },
    "Онлайн-лекторий": {
        "utm_source": "emaillektoriy", "header_folder": None, "footer_folder": "Онлайн-лекторий",
    },
}

# Curated-реестр canonical-компонентов для каждого логического блока v1.
# Выбраны как ближайшие по смыслу/структуре (GENERATION_RULES.md §3.2) на основании
# фактического HTML в unisender_components.zip. Компоненты вне этого реестра
# (SMM-подсказка `component`) в v1 не поддерживаются — Generation сообщает об этом,
# а не подбирает наугад.
HERO_TEMPLATE = {"module": "Баннеры", "element": "Вариант 5"}
INTRO_TEMPLATE = {"module": "Текстовые блоки", "element": "Текст 16px"}
CARD_TEMPLATE = {"module": "Дополнительные Баннеры", "element": "Вариант 1"}
SCHEDULE_TEMPLATE = {"module": "Мероприятия", "element": "Расписание 1"}
SCHEDULE_MAX_EVENTS = 20


class GenerationError(Exception):
    """Недостающие данные или несоответствие библиотеке — блокирует выдачу HTML."""


class ComponentLibrary:
    def __init__(self, manifest_path: Path, zip_path: Path):
        if not manifest_path.is_file():
            raise GenerationError(f"manifest.json отсутствует в source set: {manifest_path}")
        if not zip_path.is_file():
            raise GenerationError(f"unisender_components.zip отсутствует в source set: {zip_path}")
        self.entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.zip_path = zip_path
        self._index = {(e["module"], e["element"]): e for e in self.entries}

    def entry(self, module: str, element: str) -> dict:
        key = (module, element)
        if key not in self._index:
            raise GenerationError(f"Компонент отсутствует в актуальном manifest.json: {module}/{element}")
        return self._index[key]

    def html(self, module: str, element: str) -> str:
        name = f"{module}/{element}.html"
        with zipfile.ZipFile(self.zip_path) as z:
            try:
                return z.read(name).decode("utf-8")
            except KeyError:
                raise GenerationError(f"Компонент отсутствует в актуальной библиотеке unisender_components: {name}")


class SlotCounter:
    def __init__(self):
        self._counts = {}

    def __call__(self, prefix: str) -> str:
        self._counts[prefix] = self._counts.get(prefix, 0) + 1
        return f"{prefix}_{self._counts[prefix]:02d}"


# --- низкоуровневые HTML-операции ------------------------------------------------

def _escape_html_text(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Канонический системный asset стрелки рядом с "ЗАПИСАТЬСЯ" (MGI_UNISENDER_ASSETS.md,
# раздел «Расписание», f7008a36-9043-4ae5-a770-c3018cdc38f9.png) — декоративная иконка
# компонента, а не контентная переменная. manifest.json тем не менее описывает её как
# variables[].type == "image" (см. IMAGE_SLOTS.md §11), поэтому без этого исключения
# _fill_first_local_image ниже подставляет вместо неё SVG-плейсхолдер отсутствующего
# изображения (real_url для неё в Telegram-flow никогда не передаётся).
_ARROW_ASSET_SRC = (
    "https://img.hiteml.com/en/v5/user-files?userId=7788090&resource=himg&disposition=inline"
    "&name=689qtgee7jj88kb4a6h4mzmz4k4q8gk381pdsfs47w4iunexiftsjkgf8nt11izc71rwfs4guaky3hxah53q5ytezsa4pcndz1uafkqkb46h895rgu5gryoih9r3ct567"
)


def _missing_image_placeholder_src(width: int | None, alt: str) -> str:
    """Информативный placeholder для отсутствующего asset'а image-slot'а: data-URI SVG
    того же размера, что и canonical <img> (по его width), с подписью размера и
    ALT/назначения — вместо обычного broken image. Позиционирование и размеры самого
    <img> (width/style canonical-компонента) не трогаются, меняется только src."""
    w = width if width else 400
    h = max(1, round(w * 0.75))
    label_size = max(10, min(22, w // 12))
    sub_size = max(9, label_size - 4)
    dims_text = _escape_html_text(f"{w}×{h}")
    alt_text = _escape_html_text(alt)[:90] if alt else "Изображение отсутствует"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<rect x="1" y="1" width="{w - 2}" height="{h - 2}" fill="#FFF3CD" '
        f'stroke="#856404" stroke-width="2" stroke-dasharray="6,4"/>'
        f'<text x="50%" y="45%" text-anchor="middle" dominant-baseline="middle" '
        f'font-family="Arial, sans-serif" font-size="{label_size}" font-weight="700" '
        f'fill="#856404">{dims_text}</text>'
        f'<text x="50%" y="65%" text-anchor="middle" dominant-baseline="middle" '
        f'font-family="Arial, sans-serif" font-size="{sub_size}" '
        f'fill="#856404">{alt_text}</text>'
        f'</svg>'
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _scan_balanced_tr_end(html: str, start: int) -> int:
    """start указывает на '<' открывающего <tr ...>. Возвращает индекс сразу после парного </tr>."""
    open_re = re.compile(r"<tr\b", re.IGNORECASE)
    close_re = re.compile(r"</tr\s*>", re.IGNORECASE)
    depth = 1
    pos = start + 3
    while depth > 0:
        m_open = open_re.search(html, pos)
        m_close = close_re.search(html, pos)
        if m_close is None:
            raise GenerationError("Не удалось найти закрывающий </tr> в каноническом компоненте")
        if m_open is not None and m_open.start() < m_close.start():
            depth += 1
            pos = m_open.end()
        else:
            depth -= 1
            pos = m_close.end()
    return pos


def _get_hide_row_span(html: str, hide_name: str, occurrence: int = 0):
    pattern = re.compile(r'<tr\b[^>]*letteros-hide="' + re.escape(hide_name) + r'"[^>]*>')
    matches = list(pattern.finditer(html))
    if occurrence >= len(matches):
        return None
    start = matches[occurrence].start()
    end = _scan_balanced_tr_end(html, start)
    return start, end


def _remove_hide_row(html: str, hide_name: str) -> str:
    """Удаляет <tr letteros-hide="hide_name">...</tr> целиком — родная опциональность компонента."""
    span = _get_hide_row_span(html, hide_name)
    if span is None:
        return html
    start, end = span
    return html[:start] + html[end:]


def _replace_text_once(html: str, sample_text: str, real_text: str) -> str:
    if sample_text not in html:
        raise GenerationError(f"Образец текста компонента не найден в HTML: {sample_text!r}")
    return html.replace(sample_text, _escape_html_text(real_text), 1)


def _replace_nth_href_hash(html: str, n: int, real_url: str) -> str:
    target = 'href="#"'
    count = 0
    pos = 0
    while True:
        idx = html.find(target, pos)
        if idx == -1:
            raise GenerationError(f"Не найдена ожидаемая ссылка №{n + 1} компонента")
        if count == n:
            return html[:idx] + f'href="{_escape_html_text(real_url)}"' + html[idx + len(target):]
        count += 1
        pos = idx + len(target)


def _fill_first_local_image(html: str, real_url, slot_id: str, alt: str):
    m = re.search(r"<img\b[^>]*>", html)
    if m is None:
        raise GenerationError("Не найден <img> для image-slot компонента")
    tag = m.group(0)
    width = None
    w_match = re.search(r'\bwidth="(\d+)"', tag)
    if w_match:
        width = int(w_match.group(1))
    if 'src="' not in tag:
        raise GenerationError("<img> без src в каноническом компоненте")
    if real_url:
        new_src = real_url
    else:
        src_match = re.search(r'src="([^"]*)"', tag)
        original_src = src_match.group(1) if src_match else None
        new_src = original_src if original_src == _ARROW_ASSET_SRC else _missing_image_placeholder_src(width, alt)
    new_tag = re.sub(r'src="[^"]*"', f'src="{_escape_html_text(new_src)}"', tag, count=1)
    if re.search(r'\balt="[^"]*"', new_tag):
        new_tag = re.sub(r'alt="[^"]*"', f'alt="{_escape_html_text(alt)}"', new_tag, count=1)
    else:
        new_tag = new_tag.replace("<img", f'<img alt="{_escape_html_text(alt)}"', 1)
    return html[: m.start()] + new_tag + html[m.end():], width


def _fill_component_images(html: str, image_count: int, real_urls: list, block: str, purpose_fn, alt_fn, slot_counter: SlotCounter, slot_prefix: str):
    """Заполняет все image-slot'ы компонента по порядку (IMAGE_SLOTS.md §11 —
    несколько image variables = несколько отдельных slot'ов). Переиспользует
    _fill_first_local_image() без изменений: вызывает её по одному разу на
    необработанный хвост HTML, что корректно обрабатывает и компоненты
    с одним image-slot'ом (как раньше), и с несколькими.
    real_urls — реальные URL по позиции; отсутствующий/None -> информативный SVG-placeholder
    (см. _missing_image_placeholder_src()).
    Возвращает (html, image_entries) в формате images.json (см. run())."""
    if image_count == 0:
        return html, []

    processed = ""
    remaining = html
    image_entries = []
    for i in range(image_count):
        real_url = real_urls[i] if i < len(real_urls) else None
        slot_id = slot_counter(slot_prefix)
        alt = alt_fn(i)
        fixed, width = _fill_first_local_image(remaining, real_url, slot_id, alt)
        m = re.search(r"<img\b[^>]*>", fixed)
        processed += fixed[: m.end()]
        remaining = fixed[m.end():]
        entry = {"slot_id": slot_id, "block": block, "purpose": purpose_fn(i), "alt": alt, "source": real_url}
        if width:
            entry["width"] = width
        image_entries.append(entry)
    return processed + remaining, image_entries


def _placeholder_content_images(html: str, block: str, purpose_fn, alt_fn, slot_counter: SlotCounter, slot_prefix: str):
    """MVP-поведение для контентных компонентов в run_selected(): Generation не ищет,
    не подбирает и не пытается подставить реальное изображение — в отличие от
    _fill_component_images()/_fill_first_local_image() выше (которые остаются в
    работе для Hero и старого content-YAML пайплайна run()), эта функция не
    принимает real_url и не опирается на manifest.json variables[].type=="image"
    (который перечисляет и системные assets компонента, например стрелку
    «ЗАПИСАТЬСЯ» — см. _ARROW_ASSET_SRC). Вместо этого каждый <img> компонента
    получает штатную визуальную SVG-заглушку с подсказкой размера/формата
    (_missing_image_placeholder_src) по порядку появления в HTML, кроме тегов с
    src известного системного asset — они остаются как в каноническом компоненте.
    Возвращает (html, image_entries) в формате images.json (см. run_selected())."""
    processed = ""
    remaining = html
    image_entries = []
    while True:
        m = re.search(r"<img\b[^>]*>", remaining)
        if m is None:
            break
        tag = m.group(0)
        processed += remaining[: m.start()]
        src_match = re.search(r'src="([^"]*)"', tag)
        original_src = src_match.group(1) if src_match else None
        if original_src == _ARROW_ASSET_SRC:
            processed += tag
            remaining = remaining[m.end():]
            continue

        width = None
        w_match = re.search(r'\bwidth="(\d+)"', tag)
        if w_match:
            width = int(w_match.group(1))
        idx = len(image_entries)
        slot_id = slot_counter(slot_prefix)
        alt = alt_fn(idx)
        new_src = _missing_image_placeholder_src(width, alt)
        new_tag = re.sub(r'src="[^"]*"', f'src="{_escape_html_text(new_src)}"', tag, count=1)
        if re.search(r'\balt="[^"]*"', new_tag):
            new_tag = re.sub(r'alt="[^"]*"', f'alt="{_escape_html_text(alt)}"', new_tag, count=1)
        else:
            new_tag = new_tag.replace("<img", f'<img alt="{_escape_html_text(alt)}"', 1)
        processed += new_tag
        remaining = remaining[m.end():]

        entry = {"slot_id": slot_id, "block": block, "purpose": purpose_fn(idx), "alt": alt, "source": None}
        if width:
            entry["width"] = width
        image_entries.append(entry)
    return processed + remaining, image_entries


def _apply_utm(url: str, utm_source: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(k.startswith("utm_") for k in query):
        return url
    query["utm_source"] = [utm_source]
    new_qs = urlencode({k: (v[0] if len(v) == 1 else v) for k, v in query.items()}, doseq=True)
    return urlunparse(parsed._replace(query=new_qs))


def _weekday_ru(d: date_cls) -> str:
    return WEEKDAYS_RU[d.weekday()]


def _date_heading_ru(d: date_cls) -> str:
    return f"{d.day}\xa0{MONTHS_RU[d.month - 1]}"


# --- сборка блоков -----------------------------------------------------------

def build_hero_block(library: ComponentLibrary, hero_data: dict, slot_counter: SlotCounter):
    missing = [f for f in ("title", "subtitle") if not hero_data.get(f)]
    if missing:
        raise GenerationError("Hero: не хватает обязательных данных: " + ", ".join(missing))

    module, element = HERO_TEMPLATE["module"], HERO_TEMPLATE["element"]
    entry = library.entry(module, element)
    html = library.html(module, element)

    sample_texts = entry["sample_text"]
    html = _replace_text_once(html, sample_texts[0], hero_data["title"])
    html = _replace_text_once(html, sample_texts[1], hero_data["subtitle"])
    html = _remove_hide_row(html, "кнопку")

    slot_id = slot_counter("hero")
    alt = f"Иллюстрация к материалу «{hero_data['title']}»"
    html, width = _fill_first_local_image(html, hero_data.get("image"), slot_id, alt)

    image_entry = {
        "slot_id": slot_id,
        "block": "hero",
        "purpose": "Hero — главное изображение выпуска",
        "alt": alt,
        "source": hero_data.get("image"),
    }
    if width:
        image_entry["width"] = width
    return html, image_entry


def build_intro_block(library: ComponentLibrary, intro_data: dict) -> str:
    if not intro_data.get("text"):
        raise GenerationError("Intro: не хватает обязательных данных: text")
    module, element = INTRO_TEMPLATE["module"], INTRO_TEMPLATE["element"]
    entry = library.entry(module, element)
    html = library.html(module, element)
    return _replace_text_once(html, entry["sample_text"][0], intro_data["text"])


def build_card_block(library: ComponentLibrary, card_data: dict, index: int, branch_info: dict, slot_counter: SlotCounter):
    missing = [f for f in ("title", "text", "cta", "link") if not card_data.get(f)]
    if missing:
        raise GenerationError(f"Card #{index}: не хватает обязательных данных: " + ", ".join(missing))

    module, element = CARD_TEMPLATE["module"], CARD_TEMPLATE["element"]
    entry = library.entry(module, element)
    html = library.html(module, element)

    html = _remove_hide_row(html, "дату")
    sample_texts = entry["sample_text"]
    html = _replace_text_once(html, sample_texts[1], card_data["title"])
    html = _replace_text_once(html, sample_texts[2], card_data["text"])
    html = _replace_text_once(html, sample_texts[3], card_data["cta"])

    link = _apply_utm(card_data["link"], branch_info["utm_source"])
    html = _replace_nth_href_hash(html, 1, link)

    slot_id = slot_counter("card")
    alt = f"Иллюстрация к материалу «{card_data['title']}»"
    html, width = _fill_first_local_image(html, card_data.get("image"), slot_id, alt)

    image_entry = {
        "slot_id": slot_id,
        "block": f"card_{index}",
        "purpose": f"Card — изображение материала «{card_data['title']}»",
        "alt": alt,
        "source": card_data.get("image"),
    }
    if width:
        image_entry["width"] = width
    return html, image_entry


def _parse_schedule_txt(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    groups = []
    current_date = None
    current_events = None
    current_event = None

    def flush_event():
        nonlocal current_event
        if current_event is not None:
            if not all(k in current_event for k in ("time", "title", "link")):
                raise GenerationError("SCHEDULE.txt: событие с неполными полями")
            current_events.append(current_event)
            current_event = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("DATE:"):
            flush_event()
            if current_date is not None:
                groups.append((current_date, current_events))
            current_date = datetime.strptime(line[len("DATE:"):].strip(), "%d.%m.%Y").date()
            current_events = []
        elif line.strip() == "EVENT":
            flush_event()
            current_event = {}
        elif line.startswith("time:") and current_event is not None:
            current_event["time"] = line[len("time:"):].strip()
        elif line.startswith("title:") and current_event is not None:
            current_event["title"] = line[len("title:"):].strip()
        elif line.startswith("link:") and current_event is not None:
            current_event["link"] = line[len("link:"):].strip()
    flush_event()
    if current_date is not None:
        groups.append((current_date, current_events))
    return groups


def _fill_schedule_event_slot(html: str, slot_n: int, event: dict) -> str:
    span = _get_hide_row_span(html, f"событие {slot_n}")
    if span is None:
        raise GenerationError(f"Schedule: не найден слот «событие {slot_n}» в компоненте")
    start, end = span
    block = html[start:end]

    block = re.sub(r"\b\d{2}:\d{2}\b", event["time"], block, count=1)

    # Описания в SCHEDULE.txt нет — легитимно скрываем строку "текст" (letteros-hide).
    block = _remove_hide_row(block, "текст")

    name_span = _get_hide_row_span(block, "название")
    if name_span is None:
        raise GenerationError(f"Schedule: не найдена строка «название» в событии {slot_n}")
    n_start, n_end = name_span
    name_row = block[n_start:n_end]
    name_row = re.sub(
        r"(<tr[^>]*>\s*<td[^>]*>)(.*?)(</td>\s*</tr>)",
        lambda m: m.group(1) + _escape_html_text(event["title"]) + m.group(3),
        name_row, count=1, flags=re.DOTALL,
    )
    block = block[:n_start] + name_row + block[n_end:]

    # Ссылка события (UTM уже проставлен на этапе Schedule v1 — не переобрабатывается).
    block = block.replace('href="#"', f'href="{_escape_html_text(event["link"])}"', 1)

    return html[:start] + block + html[end:]


def build_schedule_blocks(library: ComponentLibrary, groups: list, element: str | None = None) -> list:
    module = SCHEDULE_TEMPLATE["module"]
    element = element or SCHEDULE_TEMPLATE["element"]
    library.entry(module, element)
    blocks = []
    for event_date, events in groups:
        if len(events) > SCHEDULE_MAX_EVENTS:
            raise GenerationError(
                f"Schedule: на {event_date.strftime('%d.%m.%Y')} {len(events)} событий "
                f"— больше {SCHEDULE_MAX_EVENTS}, нарушение TEMPLATE_SPEC.md"
            )
        html = library.html(module, element)
        html = _replace_text_once(html, "пятница", _weekday_ru(event_date))
        html = _replace_text_once(html, "19\xa0СЕНТЯБРЯ", _date_heading_ru(event_date))

        for slot_n in range(1, SCHEDULE_MAX_EVENTS + 1):
            if slot_n <= len(events):
                html = _fill_schedule_event_slot(html, slot_n, events[slot_n - 1])
            else:
                html = _remove_hide_row(html, f"событие {slot_n}")
        blocks.append(html)
    return blocks


def build_email_html(blocks: list) -> str:
    body = "\n".join(blocks)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        "<title>Письмо</title>\n"
        '<style type="text/css">\n'
        "html { -webkit-text-size-adjust: none; -ms-text-size-adjust: none; }\n"
        "@media only screen and (max-width: 599px) {\n"
        "\t.mob_100 {\n"
        "\t\twidth: 100% !important;\n"
        "\t\tmax-width: 100% !important;\n"
        "\t\tmin-width: 100% !important;\n"
        "\t}\n"
        "\t.mob_fs_48 {\n"
        "\t\tfont-size: 34px !important;\n"
        "\t\tline-height: 38px !important;\n"
        "\t}\n"
        "\t.mob_fs_34 {\n"
        "\t\tfont-size: 26px !important;\n"
        "\t\tline-height: 30px !important;\n"
        "\t}\n"
        "\t.mob_br br {\n"
        "\t\tdisplay: none !important;\n"
        "\t}\n"
        "}\n"
        "</style>\n"
        "</head>\n"
        '<body style="margin:0;padding:0;">\n'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" align="center" style="width:100%; max-width:600px;">\n'
        "<tbody>\n" + body + "\n</tbody>\n</table>\n</body>\n</html>\n"
    )


# --- вход этапа ----------------------------------------------------------------

def run(build_dir: Path, content_path: Path) -> None:
    source_dir = build_dir / "source"

    if not content_path.is_file():
        print(f"Ошибка: content-файл не найден: {content_path}")
        sys.exit(1)

    try:
        content = yaml.safe_load(content_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        print(f"Ошибка: content-файл не удалось разобрать как YAML: {exc}")
        sys.exit(1)

    branch = content.get("branch")
    if branch not in BRANCH_REGISTRY:
        known = ", ".join(BRANCH_REGISTRY)
        print(f"Ошибка: неизвестный или отсутствующий branch. Ожидается одно из: {known}")
        sys.exit(1)
    branch_info = BRANCH_REGISTRY[branch]

    try:
        library = ComponentLibrary(source_dir / "manifest.json", source_dir / "unisender_components.zip")
    except GenerationError as exc:
        print(f"Ошибка: {exc}")
        sys.exit(1)

    errors = []
    blocks = []
    image_list = []
    slot_counter = SlotCounter()

    if branch_info["header_folder"]:
        header_element = f"{branch_info['header_folder']} 1"
        try:
            library.entry("Шапки", header_element)
            blocks.append(library.html("Шапки", header_element))
        except GenerationError as exc:
            errors.append(str(exc))
    else:
        print(f"Внимание: для branch «{branch}» нет системного Header-компонента в библиотеке — блок пропущен.")

    hero_data = content.get("hero") or {}
    try:
        hero_html, hero_image = build_hero_block(library, hero_data, slot_counter)
        blocks.append(hero_html)
        image_list.append(hero_image)
    except GenerationError as exc:
        errors.append(str(exc))

    intro_data = content.get("intro") or {}
    try:
        blocks.append(build_intro_block(library, intro_data))
    except GenerationError as exc:
        errors.append(str(exc))

    cards_data = content.get("cards") or []
    if not cards_data:
        errors.append("Card: не передано ни одной карточки (обязательный блок)")
    for i, card_data in enumerate(cards_data, start=1):
        try:
            card_html, card_image = build_card_block(library, card_data, i, branch_info, slot_counter)
            blocks.append(card_html)
            image_list.append(card_image)
        except GenerationError as exc:
            errors.append(str(exc))

    if content.get("schedule"):
        schedule_path = build_dir / "schedule" / "SCHEDULE.txt"
        if not schedule_path.is_file():
            errors.append("Schedule: запрошен, но builds/<BUILD_ID>/schedule/SCHEDULE.txt отсутствует")
        else:
            try:
                groups = _parse_schedule_txt(schedule_path)
            except GenerationError as exc:
                errors.append(str(exc))
                groups = []
            if not groups:
                errors.append("Schedule: SCHEDULE.txt не содержит ни одного события")
            else:
                try:
                    blocks.extend(build_schedule_blocks(library, groups))
                except GenerationError as exc:
                    errors.append(str(exc))

    if branch_info["footer_folder"]:
        footer_element = f"{branch_info['footer_folder']} 1"
        try:
            library.entry("Подвалы", footer_element)
            blocks.append(library.html("Подвалы", footer_element))
        except GenerationError as exc:
            errors.append(str(exc))
    else:
        errors.append(f"Footer: для branch «{branch}» нет системного компонента в библиотеке")

    if errors:
        print("Generation остановлена — недостаточно данных или несоответствие библиотеке:")
        for exc in errors:
            print(f"  - {exc}")
        sys.exit(1)

    email_html = build_email_html(blocks)

    generation_dir = build_dir / "generation"
    generation_dir.mkdir(parents=True, exist_ok=True)
    (generation_dir / "email.html").write_text(email_html, encoding="utf-8")
    (generation_dir / "images.json").write_text(
        json.dumps(image_list, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Generation завершена: {generation_dir}")
    print(f"  Блоков: {len(blocks)}, image-slot'ов: {len(image_list)}")


# --- Telegram Generation flow (выбор компонентов) ------------------------------

def build_generic_block(library: ComponentLibrary, module: str, element: str, texts: list) -> str:
    """Собирает HTML произвольного canonical-компонента, подставляя тексты по порядку
    sample_text (тот же приём, что и в build_hero_block/build_card_block выше).
    Используется Telegram Generation flow для контентных компонентов, выбранных
    пользователем вручную, — не подменяет curated hero/intro/card/schedule пайплайн."""
    entry = library.entry(module, element)
    sample_texts = entry["sample_text"]
    if len(texts) != len(sample_texts):
        raise GenerationError(
            f"Компонент {module}/{element}: ожидается {len(sample_texts)} строк содержимого, получено {len(texts)}"
        )
    html = library.html(module, element)
    for sample, real in zip(sample_texts, texts):
        html = _replace_text_once(html, sample, real)
    return html


def run_selected(build_dir: Path, selection: dict) -> None:
    """Generation v1 для Telegram Generation flow: собирает email.html из явно выбранных
    пользователем canonical-компонентов, а не из content YAML (см. run() выше).

    Header/Hero/Footer вставляются как выбранный компонент без подстановки текста —
    в этом flow свободный ввод содержимого есть только у контентных компонентов.
    Header/Footer — системные assets (IMAGE_SLOTS.md §2.1), их изображения не
    обрабатываются как image-slot'ы. У Hero и контентных компонентов, наоборот,
    variable-изображения (manifest.json variables[].type == "image") заполняются
    через _fill_component_images()/_fill_first_local_image() — как в build_hero_block()/
    build_card_block() выше: реальный URL, если передан в selection, иначе информативный
    SVG-placeholder (см. _missing_image_placeholder_src()). Отдельного шага в
    Telegram-диалоге для загрузки этих изображений сейчас нет — real_urls остаются
    пустыми, поэтому пока такие slot'ы всегда получают этот placeholder (см. HANDOFF).

    selection:
      {
        "header": {"module", "element"} | None,
        "hero": {"module", "element", "images": [url, ...]},
        "content_blocks": [{"module", "element", "texts": [...], "images": [url, ...]}, ...],
        "schedule": bool,
        "footer": {"module", "element"},
      }
    "images" — необязательный список реальных URL по позиции image-slot'а в
    компоненте; отсутствующий элемент списка (или весь ключ) -> информативный
    SVG-placeholder (см. _missing_image_placeholder_src()).
    """
    source_dir = build_dir / "source"
    try:
        library = ComponentLibrary(source_dir / "manifest.json", source_dir / "unisender_components.zip")
    except GenerationError as exc:
        print(f"Ошибка: {exc}")
        sys.exit(1)

    errors = []
    blocks = []
    image_list = []
    slot_counter = SlotCounter()

    header = selection.get("header")
    if header:
        try:
            library.entry(header["module"], header["element"])
            blocks.append(library.html(header["module"], header["element"]))
        except GenerationError as exc:
            errors.append(str(exc))

    hero = selection.get("hero")
    if not hero:
        errors.append("Hero: компонент не выбран (обязательный блок)")
    else:
        try:
            hero_entry = library.entry(hero["module"], hero["element"])
            hero_html = library.html(hero["module"], hero["element"])
            hero_image_count = sum(1 for v in hero_entry.get("variables", []) if v.get("type") == "image")
            hero_html, hero_images = _fill_component_images(
                hero_html, hero_image_count, hero.get("images") or [],
                block="hero",
                purpose_fn=lambda i: "Hero — главное изображение выпуска",
                alt_fn=lambda i: "Иллюстрация Hero-блока выпуска",
                slot_counter=slot_counter, slot_prefix="hero",
            )
            blocks.append(hero_html)
            image_list.extend(hero_images)
        except GenerationError as exc:
            errors.append(str(exc))

    for i, block in enumerate(selection.get("content_blocks") or [], start=1):
        try:
            module, element = block["module"], block["element"]
            texts = block.get("texts") or []
            content_entry = library.entry(module, element)
            content_html = build_generic_block(library, module, element, texts)

            label = texts[0] if texts else None
            alt = f"Иллюстрация к материалу «{label}»" if label else "Иллюстрация к материалу выпуска"

            # Старая логика поиска/подстановки изображений (manifest.json
            # variables[].type=="image" + попытка подставить реальный URL из
            # block["images"]) — не используется в MVP, см. _placeholder_content_images()
            # ниже. Оставлено закомментированным для возврата, если понадобится upload
            # реальных изображений контентных компонентов.
            #
            # content_image_count = sum(1 for v in content_entry.get("variables", []) if v.get("type") == "image")
            # content_html, content_images = _fill_component_images(
            #     content_html, content_image_count, block.get("images") or [],
            #     block=f"content_{i}",
            #     purpose_fn=lambda j: f"{module}/{element} — изображение материала",
            #     alt_fn=lambda j: alt,
            #     slot_counter=slot_counter, slot_prefix="content",
            # )
            content_html, content_images = _placeholder_content_images(
                content_html,
                block=f"content_{i}",
                purpose_fn=lambda j: f"{module}/{element} — изображение материала",
                alt_fn=lambda j: alt,
                slot_counter=slot_counter, slot_prefix="content",
            )
            blocks.append(content_html)
            image_list.extend(content_images)
        except GenerationError as exc:
            errors.append(f"Контентный компонент #{i}: {exc}")

    if selection.get("schedule"):
        schedule_path = build_dir / "schedule" / "SCHEDULE.txt"
        if not schedule_path.is_file():
            errors.append("Schedule: запрошен, но builds/<BUILD_ID>/schedule/SCHEDULE.txt отсутствует")
        else:
            try:
                groups = _parse_schedule_txt(schedule_path)
            except GenerationError as exc:
                errors.append(str(exc))
                groups = []
            if not groups:
                errors.append("Schedule: SCHEDULE.txt не содержит ни одного события")
            else:
                try:
                    blocks.extend(build_schedule_blocks(library, groups, selection.get("schedule_template")))
                except GenerationError as exc:
                    errors.append(str(exc))

    footer = selection.get("footer")
    if not footer:
        errors.append("Footer: компонент не выбран (обязательный блок)")
    else:
        try:
            library.entry(footer["module"], footer["element"])
            blocks.append(library.html(footer["module"], footer["element"]))
        except GenerationError as exc:
            errors.append(str(exc))

    if errors:
        print("Generation остановлена — недостаточно данных или несоответствие библиотеке:")
        for exc in errors:
            print(f"  - {exc}")
        sys.exit(1)

    email_html = build_email_html(blocks)

    generation_dir = build_dir / "generation"
    generation_dir.mkdir(parents=True, exist_ok=True)
    (generation_dir / "email.html").write_text(email_html, encoding="utf-8")
    (generation_dir / "images.json").write_text(
        json.dumps(image_list, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Generation завершена: {generation_dir}")
    print(f"  Блоков: {len(blocks)}, image-slot'ов: {len(image_list)}")
