"""Letteros -> UniSender, этап 1.5: техническая адаптация оболочки уже
распознанного canonical Letteros-компонента (см. letteros_recognition.py) до
формы, пригодной для UniSender — без извлечения или переноса содержимого.

Подтверждено экспериментально сравнением всех 121 canonical-пар
letteros_components.zip <-> unisender_components.zip (симметричная структурная
проверка через letteros_recognition.structural_match(), с учётом того, что
<td background>, CSS background-image и VML <v:image src> внутри
MSO-комментариев — тоже переменный контент, не структура): ровно две
универсальные, не зависящие от module/element трансформации (adapt_wrapper())
достаточны для 118 из 121 пары. Три пары — не технические различия Letteros-
редактора, а реальные расхождения между версиями двух библиотек (осознанная
правка вёрстки или рассинхронизация состава) — см. KNOWN_ADAPTATION_EXCEPTIONS.

Этот модуль не делает следующий шаг production-конвейера (перенос текста/
ссылок/картинок в найденный фрагмент, сборку письма, расписание) — только
подготавливает уже найденный (recognize_components()) и подтверждённый
фрагмент к использованию, либо явно сообщает, что автоматической подготовке
доверять нельзя (REQUIRES_MANUAL_REVIEW).
"""

import re

import generation
import letteros_recognition

# Editor-only атрибуты Letteros, которые снимаются adapt_wrapper(). Порядок
# важен для регэкспа: "letteros-background-outlook" должен идти раньше
# "letteros-background", иначе более короткая альтернатива съест только
# префикс и оставит "-outlook="..."" висеть неудалённым.
EDITOR_ONLY_ATTRS_TO_STRIP = (
    "letteros-align",
    "letteros-background-outlook",
    "letteros-background",
    "letteros-border-color",
    "letteros-border-radius",
    "letteros-resizer",
    "letteros-tag",
)

# letteros-element/letteros-module/letteros-hide/letteros-no-utm сюда не
# входят и adapt_wrapper() их не трогает — они уже совпадают между
# letteros_components.zip и unisender_components.zip как есть.
_EDITOR_ONLY_ATTR_RE = re.compile(
    r'\s+(?:' + "|".join(EDITOR_ONLY_ATTRS_TO_STRIP) + r')(?:="[^"]*")?',
    re.IGNORECASE,
)

_ROOT_TR_RE = re.compile(r"^<tr\b")
_WRAPPER_MARKER = 'em="block" class="em-structure"'
_WRAPPER_PREFIX = f'<tr {_WRAPPER_MARKER}'

# Letteros click-tracking редиректор: https://api.letteros.com/pixel/<id>/
# link/<uuid>?url=<base64-исходный-URL>. Подтверждено на всех 4 real
# fixtures ("letteros htmls/"): это единственная форма, в которой
# api.letteros.com встречается внутри href (сегмент "/link/" отличает её от
# отдельного открывающего трекинг-пикселя вида ".../pixel/<id>?v=..." —
# тот встречается только в src, не в href, и этим правилом не затрагивается).
# Решение первой версии миграции: href с этой ссылкой удаляется целиком (URL
# внутри НЕ декодируется и не восстанавливается — это сделает СММ вручную в
# UniSender), но сам <a> и его содержимое (картинка/текст/атрибуты) остаются.
_LETTEROS_TRACKING_HREF_RE = re.compile(
    r'\s+href="https://api\.letteros\.com/pixel/[^"]*/link/[^"]*"'
)
_LETTEROS_TRACKING_HREF_VALUE_RE = re.compile(r'^https://api\.letteros\.com/pixel/.*/link/.*$')

# --- класс 1: production-вариант без внешней card-обвязки ------------------
#
# letteros_recognition.recognize_components() уже подтвердил (structural_match
# по внутренней <tr>, см. её docstring), что production-фрагмент структурно
# соответствует ВНУТРЕННЕЙ части canonical-компонента без внешней card-
# обвязки/MSO ghost-table. Здесь эта обвязка СОБИРАЕТСЯ заново — берётся из
# canonical UniSender-компонента (letteros_migrate передаёт его html), а не
# сохраняется production-версия без обвязки как есть.
_IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc="([^"]*)"')
_IMG_ALT_RE = re.compile(r'<img\b[^>]*\balt="([^"]*)"')
_A_HREF_RE = re.compile(r'<a\b[^>]*\bhref="([^"]*)"')
_FIRST_A_HREF_ATTR_RE = re.compile(r'(<a\b[^>]*?)\s+href="[^"]*"')


def _strip_first_anchor_href(html: str) -> str:
    return _FIRST_A_HREF_ATTR_RE.sub(r"\1", html, count=1)


def _set_first_anchor_href(html: str, href_value: str) -> str:
    escaped = generation._escape_html_text(href_value)
    return _FIRST_A_HREF_ATTR_RE.sub(rf'\1 href="{escaped}"', html, count=1)


def _transplant_image_slot(uni_inner_html: str, production_html: str) -> str:
    """image-slot внутри canonical UniSender-компонента: существующий
    механизм assets/image slots (generation._fill_first_local_image()) — src/
    alt берутся из production, РАЗМЕР/border-radius/остальная геометрия —
    из canonical (см. IMAGE_SLOTS.md §9-10, не придумываются и не переносятся
    из production). href обрабатывается отдельно: Letteros tracking-ссылка
    удаляется (уже принятое правило), обычная — переносится как есть."""
    src_m = _IMG_SRC_RE.search(production_html)
    real_url = src_m.group(1) if src_m else None
    production_alt_m = _IMG_ALT_RE.search(production_html)
    production_alt = (production_alt_m.group(1) if production_alt_m else "").strip()
    uni_alt_m = _IMG_ALT_RE.search(uni_inner_html)
    uni_alt = uni_alt_m.group(1) if uni_alt_m else ""
    alt = production_alt or uni_alt

    new_html, _width = generation._fill_first_local_image(uni_inner_html, real_url, "image", alt)

    href_m = _A_HREF_RE.search(production_html)
    if href_m:
        href_value = href_m.group(1)
        if _LETTEROS_TRACKING_HREF_VALUE_RE.match(href_value):
            new_html = _strip_first_anchor_href(new_html)
        else:
            new_html = _set_first_anchor_href(new_html, href_value)
    return new_html


def _rebuild_wrapper_stripped(unisender_html: str, production_fragment: str) -> str:
    split = letteros_recognition.split_wrapper_stripped(unisender_html)
    if split is None:
        raise ValueError(
            "canonical UniSender-компонент не имеет ожидаемой формы "
            "card-обвязки для wrapper_stripped адаптации"
        )
    prefix, uni_inner, suffix = split
    if letteros_recognition._is_single_image_slot_shape(uni_inner):
        new_inner = _transplant_image_slot(uni_inner, production_fragment)
    else:
        # не image-slot (например, кнопка) — текст/href уже "переменные"
        # каналы (см. letteros_recognition._VARIABLE_ATTRS), поэтому
        # production-версия внутренней <tr> переносится как есть; tracking
        # href из неё снимет adapt_wrapper() ниже, как и для обычных блоков.
        new_inner = production_fragment
    return prefix + new_inner + suffix


# --- класс 3: content-aware перенос текста заголовков -----------------------

_TD_INNER_CONTENT_RE = re.compile(r"(<td\b[^>]*>)(.*)(</td>)", re.DOTALL)


def _rebuild_heading_text_injection(unisender_html: str, extracted_text: str) -> str:
    split = letteros_recognition.split_wrapper_stripped(unisender_html)
    if split is None:
        raise ValueError(
            "canonical UniSender-компонент не имеет ожидаемой формы для "
            "heading_text_injection"
        )
    prefix, uni_inner, suffix = split
    m = _TD_INNER_CONTENT_RE.search(uni_inner)
    if m is None:
        raise ValueError("не найден <td>...</td> внутри canonical UniSender-заголовка")
    # extracted_text — уже сырой срез исходного HTML-текста (см.
    # letteros_recognition.extract_text()), с валидными entity вроде &nbsp; —
    # вставляется как есть, повторное HTML-экранирование испортило бы их.
    new_inner = uni_inner[:m.start(2)] + extracted_text + uni_inner[m.end(2):]
    return prefix + new_inner + suffix


# --- класс 4: 2-колоночный grid "Контентные блоки/Вариант 2-4" -------------
#
# letteros_recognition.extract_grid_items() уже подтвердил (по устойчивым
# HTML-маркерам, см. её docstring), что production-фрагмент структурно
# является 2-колоночным grid'ом этой family, и для каждого item'а определил
# accent card. Здесь каждый item СОБИРАЕТСЯ заново из существующего canonical
# UniSender item-шаблона нужного accent'а (letteros_recognition.
# GRID_ACCENT_TEMPLATES) — production-контент (картинка/заголовок/текст/CTA/
# tag-pill'ы) переносится в него через уже существующие механизмы
# generation.py (_fill_first_local_image/_replace_text_once/
# _replace_nth_href_hash/_remove_hide_row/_remove_hide_td) — тот же принцип
# "родной опциональности компонента", которым Generation уже пользуется для
# других полей (кнопка/дата/событие в Мероприятия).
_GRID_SKELETON_KEY = ("Контентные блоки", "Вариант 2")

# sample-текст tag-pill'ов зависит от того, с какой позиции ("left"/"right")
# взят исходный canonical-файл item-шаблона (см. GRID_ACCENT_TEMPLATES) — не
# от итоговой позиции item'а в письме. Подтверждено на всех трёх вариантах:
# left-позиция (белый) везде "Лекция"/"Дегустация", right-позиция (коралл/
# teal) везде "Экскурсия"/"Лекция".
_GRID_TAG_SAMPLES = {
    "left": ("Лекция", "Дегустация"),
    "right": ("Экскурсия", "Лекция"),
}
# Позиционная замена (не literal sample-текст -- он в unisender_components
# содержит &nbsp; и чувствителен к пробелам) теми же style-якорями, что и
# letteros_recognition.extract_grid_items() при извлечении -- откуда и куда
# переносится содержимое, определяется одинаково на обоих концах.
_GRID_HEADING_ANCHOR_RE = re.compile(
    r'(<td\b[^>]*style="[^"]*font-size:\s*18px[^>]*>)(.*?)(</td>)', re.DOTALL | re.IGNORECASE,
)
_GRID_TEXT_ANCHOR_RE = re.compile(
    r'(<td\b[^>]*style="[^"]*font-size:\s*16px;[^"]*line-height:\s*24px[^>]*>)(.*?)(</td>)',
    re.DOTALL | re.IGNORECASE,
)
_GRID_CTA_ANCHOR_RE = re.compile(
    r'(line-height:\s*19px[^"]*"[^>]*>\s*<a\b[^>]*>)(.*?)(\s*<img\b)', re.DOTALL | re.IGNORECASE,
)


def _replace_anchored(html: str, pattern: re.Pattern, new_content: str) -> str:
    m = pattern.search(html)
    if m is None:
        raise ValueError(f"canonical grid item-шаблон не имеет ожидаемого узла для {pattern.pattern!r}")
    return html[:m.start(2)] + new_content + html[m.end(2):]


def _fill_grid_item(item_html: str, item_data: dict, position: str) -> str:
    """position -- "left"/"right", с которой взят САМ item_html-шаблон (см.
    letteros_recognition.GRID_ACCENT_TEMPLATES), не итоговая позиция в
    письме -- определяет sample-текст tag-pill'ов и имена letteros-hide."""
    word = "левом" if position == "left" else "правом"
    tag1_sample, tag2_sample = _GRID_TAG_SAMPLES[position]

    html, _width = generation._fill_first_local_image(
        item_html, item_data["img_src"], "grid_image", item_data["img_alt"],
    )
    # occurrence 0 -- href="#" обёртки картинки (не трогаем, см. IMAGE_SLOTS.md
    # "не придумывать" -- нет production-сигнала, куда должна вести обёртка);
    # occurrence 1 -- href="#" CTA-ссылки.
    html = generation._replace_nth_href_hash(html, 1, item_data["cta_href"])
    # heading/text/cta_text — уже сырые срезы исходного HTML-текста (см.
    # letteros_recognition._strip_tags_to_text()), с валидными entity вроде
    # &nbsp; — вставляются как есть, без повторного экранирования.
    html = _replace_anchored(html, _GRID_HEADING_ANCHOR_RE, item_data["heading"])
    html = _replace_anchored(html, _GRID_TEXT_ANCHOR_RE, item_data["text"])
    html = _replace_anchored(html, _GRID_CTA_ANCHOR_RE, item_data["cta_text"])

    tags = item_data["tags"]
    if len(tags) == 0:
        # родная опциональность компонента (letteros-hide) -- вся строка
        # tag-pild'ов отсутствует в production целиком, текст не выдумывается.
        html = generation._remove_hide_row(html, "все ярлыки")
    elif len(tags) == 1:
        html = generation._remove_hide_td(html, f"ярлык в {word} блоке 2")
        html = generation._replace_text_once(html, tag1_sample, tags[0])
    else:
        html = generation._replace_text_once(html, tag1_sample, tags[0])
        html = generation._replace_text_once(html, tag2_sample, tags[1])
    return html


def _rebuild_grid_content_injection(unisender_library, letteros_html: str) -> str:
    items_data = letteros_recognition.extract_grid_items(letteros_html)
    if items_data is None:
        raise ValueError("match_mode='grid_content_injection': не удалось повторно извлечь содержимое grid'а")

    skeleton = unisender_library.html(*_GRID_SKELETON_KEY)
    split = letteros_recognition.split_grid_items(skeleton)
    if split is None:
        raise ValueError(f"canonical {_GRID_SKELETON_KEY} не имеет ожидаемой формы grid'а")
    prefix, _item1_skeleton, connector, _item2_skeleton, suffix = split

    built_items = []
    for item_data in items_data:
        canon_key, position = letteros_recognition.GRID_ACCENT_TEMPLATES[item_data["accent"]]
        template_html = unisender_library.html(*canon_key)
        template_split = letteros_recognition.split_grid_items(template_html)
        if template_split is None:
            raise ValueError(f"canonical {canon_key} не имеет ожидаемой формы grid'а")
        t_item1, t_item2 = template_split[1], template_split[3]
        item_template = t_item1 if position == "left" else t_item2
        built_items.append(_fill_grid_item(item_template, item_data, position))

    M, C = letteros_recognition.GRID_ITEM_OPEN_MARKER, letteros_recognition.GRID_ITEM_CLOSE_MARKER
    return prefix + M + built_items[0] + C + connector + M + built_items[1] + C + suffix


class AdaptationStatus:
    """Простой явный статус — не enum-класс, чтобы не тянуть лишнюю
    зависимость; значения используются как обычные строки-константы."""
    SUPPORTED = "SUPPORTED"
    REQUIRES_MANUAL_REVIEW = "REQUIRES_MANUAL_REVIEW"


# Установлено экспериментом "121 пара" (см. модульный docstring). Список
# фиксированный и не эвристический — новый компонент сюда не должен
# добавляться без отдельной проверки; сам adapt_wrapper() не умеет и не
# пытается угадывать, есть ли у произвольного компонента такая же проблема.
KNOWN_ADAPTATION_EXCEPTIONS = {
    ("Дополнительные Баннеры", "Вариант 7"): (
        'внешний align отличается ("right" у Letteros-источника, "center" у '
        "canonical UniSender-компонента) — осознанная правка вёрстки при "
        "переносе в библиотеку UniSender, не editor-артефакт Letteros"
    ),
    ("Дополнительные Баннеры", "Вариант 8"): (
        'то же расхождение, align="left" у Letteros-источника vs "center" '
        "у canonical UniSender-компонента"
    ),
    ("Подвалы", "Школа 1"): (
        'у Letteros-источника есть дополнительная иконка соцсети '
        '(letteros-hide="Max"), которой нет в canonical UniSender-компоненте '
        "— рассинхронизация состава двух версий библиотеки, не устранимо "
        "технической адаптацией оболочки"
    ),
}


_MODULES_WITH_KNOWN_EXCEPTIONS = {module for module, _element in KNOWN_ADAPTATION_EXCEPTIONS}


class AdaptedComponent:
    """element может быть None -- см. letteros_recognition.RecognizedComponent
    (module-level распознавание для "Шапки"/"Подвалы"). adapt_component()
    обрабатывает этот случай отдельно -- см. её docstring."""

    __slots__ = ("module", "element", "html", "status", "note")

    def __init__(self, module: str, element: str | None, html: str, status: str, note: str | None = None):
        self.module = module
        self.element = element
        self.html = html
        self.status = status
        self.note = note

    def __repr__(self):
        element_repr = self.element if self.element is not None else "<module-level>"
        return f"AdaptedComponent({self.module}/{element_repr}, status={self.status})"


def adapt_wrapper(letteros_html: str) -> str:
    """Три универсальные трансформации, детерминированные и не зависящие от
    того, какой это module/element:

    1) в открывающий <tr> добавляется em="block" class="em-structure";
    2) удаляются 7 editor-only атрибутов Letteros (EDITOR_ONLY_ATTRS_TO_STRIP);
    3) удаляются href-атрибуты, указывающие на Letteros click-tracking
       редиректор (см. _LETTEROS_TRACKING_HREF_RE) — сам <a> и его содержимое
       (текст/картинка/остальные атрибуты) остаются, удаляется только href.

    Не меняются: текст, src (img/a), href, не указывающий на Letteros
    tracking (в т.ч. прямые ссылки расписания), CSS, вложенность, порядок
    элементов, комментарии, letteros-element/module/hide/no-utm, любые другие
    атрибуты.

    Идемпотентна: letteros_html, уже несущий маркер em="block" в начале, не
    содержащий ни одного из 7 атрибутов и ни одного Letteros tracking href,
    возвращается без изменений при повторном вызове."""
    html = _EDITOR_ONLY_ATTR_RE.sub("", letteros_html)
    html = _LETTEROS_TRACKING_HREF_RE.sub("", html)
    if not html.startswith(_WRAPPER_PREFIX):
        html = _ROOT_TR_RE.sub(_WRAPPER_PREFIX, html, count=1)
    return html


def adapt_component(
    module: str, element: str | None, letteros_html: str,
    *, match_mode: str = "full", unisender_html: str | None = None,
    extracted_text: str | None = None, unisender_library=None,
) -> AdaptedComponent:
    """Точка входа для вызывающего кода: адаптирует оболочку через
    adapt_wrapper() и явно сообщает статус — можно ли доверять результату как
    canonical-совместимому (SUPPORTED) или для этого конкретного (module,
    element) известно, что технической адаптации оболочки недостаточно
    (REQUIRES_MANUAL_REVIEW, см. KNOWN_ADAPTATION_EXCEPTIONS). Само
    преобразование выполняется одинаково независимо от статуса — статус
    только сообщает вызывающему коду, можно ли доверять результату молча.

    match_mode (см. letteros_recognition.RecognizedComponent.match_mode):
      - "full" (по умолчанию) — letteros_html используется как есть, только
        оболочка адаптируется adapt_wrapper() (прежнее, неизменное поведение);
      - "wrapper_stripped" — вокруг letteros_html (внутренняя <tr> без
        внешней card-обвязки) СОБИРАЕТСЯ полный canonical-блок на основе
        unisender_html (обязателен); картинки идут через существующий
        механизм image-slot (generation._fill_first_local_image());
      - "heading_text_injection" — extracted_text (обязателен) подставляется
        вместо sample-текста в unisender_html (обязателен), letteros_html при
        этом не используется для сборки результата (используется только для
        диагностики вызывающим кодом);
      - "grid_content_injection" — unisender_library (обязателен, целиком —
        нужны несколько canonical-файлов family "Контентные блоки/Вариант
        2-4" сразу) используется для сборки двух item'ов заново; letteros_html
        используется только для повторного извлечения содержимого (см.
        letteros_recognition.extract_grid_items()).

    element=None (module-level распознавание, см.
    letteros_recognition.RecognizedComponent) обрабатывается отдельно:
    KNOWN_ADAPTATION_EXCEPTIONS проверяется по паре (module, element), а
    конкретный element здесь неизвестен -- нельзя ни подтвердить, что это
    исключение, ни безопасно исключить это. Поэтому:
      - если для module вообще нет ни одной записи в
        KNOWN_ADAPTATION_EXCEPTIONS (сегодня это "Шапки") -- риска нет,
        SUPPORTED;
      - если для module есть хотя бы одна запись (сегодня "Подвалы" --
        ("Подвалы", "Школа 1")) -- нельзя ни молча считать SUPPORTED (это
        может быть как раз "Школа 1"), ни без диагностики по контенту
        предполагать, что это именно она (запрещено требованием задачи) --
        REQUIRES_MANUAL_REVIEW с объяснением причины, отличным от текста
        конкретного известного исключения."""
    if match_mode == "wrapper_stripped":
        if unisender_html is None:
            raise ValueError("match_mode='wrapper_stripped' требует unisender_html")
        html = adapt_wrapper(_rebuild_wrapper_stripped(unisender_html, letteros_html))
    elif match_mode == "heading_text_injection":
        if unisender_html is None or extracted_text is None:
            raise ValueError("match_mode='heading_text_injection' требует unisender_html и extracted_text")
        html = adapt_wrapper(_rebuild_heading_text_injection(unisender_html, extracted_text))
    elif match_mode == "grid_content_injection":
        if unisender_library is None:
            raise ValueError("match_mode='grid_content_injection' требует unisender_library")
        html = adapt_wrapper(_rebuild_grid_content_injection(unisender_library, letteros_html))
    else:
        html = adapt_wrapper(letteros_html)

    if element is None:
        if module in _MODULES_WITH_KNOWN_EXCEPTIONS:
            known = sorted(e for m, e in KNOWN_ADAPTATION_EXCEPTIONS if m == module)
            note = (
                f'module-level распознавание "{module}" (element не определён -- '
                f"см. letteros_recognition.MODULE_LEVEL_ELIGIBLE_MODULES): для этого "
                f"модуля в KNOWN_ADAPTATION_EXCEPTIONS есть {len(known)} запись(и) "
                f"({', '.join(known)}), и без конкретного element нельзя безопасно "
                f"исключить, что фактический блок — именно одна из них. Не "
                f"предполагается ни то, ни другое по содержимому — решение оставлено "
                f"человеку."
            )
            return AdaptedComponent(module, element, html, AdaptationStatus.REQUIRES_MANUAL_REVIEW, note)
        return AdaptedComponent(module, element, html, AdaptationStatus.SUPPORTED, None)

    note = KNOWN_ADAPTATION_EXCEPTIONS.get((module, element))
    status = AdaptationStatus.REQUIRES_MANUAL_REVIEW if note else AdaptationStatus.SUPPORTED
    return AdaptedComponent(module, element, html, status, note)
