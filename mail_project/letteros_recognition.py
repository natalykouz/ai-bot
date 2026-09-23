"""Letteros -> UniSender, этап 1: распознавание canonical Letteros-компонентов
в HTML старого письма — только идентификация, без извлечения контента и без сборки.

Источник canonical Letteros-компонентов — mail_project/letteros_components.zip
(121 компонент, то же множество module/element, что и unisender_components.zip —
см. LEGACY_NEWSLETTER_MIGRATION.md). Имя Letteros-компонента напрямую задаёт
соответствующий canonical UniSender-компонент — отдельный mapping не нужен.

Почему нельзя искать по letteros-element/letteros-module, как это делает
qa.find_components(): при экспорте/отправке письма Letteros полностью убирает
все свои editor-only атрибуты (letteros-element/letteros-module/letteros-hide и
т.д.) — подтверждено на реальном письме проекта (mail_project/test/Копия — Копия
— Футер Школа.html: ноль вхождений "letteros-"). Остаётся только сама разметка
блока (теги, CSS, HTML-комментарии, href/src/текст), почти без изменений —
кроме мелкой CSS-нормализации (например, "padding: 15px 15px" -> "padding: 15px").

Алгоритм — два прохода:
1) дешёвый поиск кандидатов по устойчивой "визуальной подписи" обёртки
   canonical-компонента (align/bgcolor/padding/border-radius её первого <td>);
2) обязательная структурная проверка найденного кандидата целиком деревом тегов
   (переиспользует тот же принцип, что и qa.py: сравнение по сигнатуре тега +
   набора атрибутов, с допуском на отсутствующие letteros-hide-опциональные
   узлы) — чтобы случайное совпадение подписи не считалось компонентом.

Если кандидат по подписи найден, но структурная проверка не прошла — компонент
не распознаётся (не подбирается "на глаз"), причина фиксируется в результате.

Исключение — module-level recognition для модулей "Шапки"/"Подвалы"
(MODULE_LEVEL_ELIGIBLE_MODULES): диагностика на 4 реальных Letteros-письмах
проекта показала, что все элементы внутри каждого из этих двух модулей
структурно идентичны друг другу (отличаются только src/href/текстом), из-за
чего структурная проверка для них никогда не даёт ровно один подтверждённый
canonical element — ни на реальных, ни на некоторых синтетических данных.
При этом "подпись" обёртки для этих двух модулей однозначна и не пересекается
ни с одним другим модулем библиотеки. Поэтому в этом (и только в этом) случае
компонент признаётся распознанным на уровне модуля: module задан, element =
None, исходный HTML-фрагмент сохраняется как есть. Для остальных модулей
никаких послаблений нет — там по-прежнему требуется ровно один подтверждённый
кандидат, иначе результат остаётся в unresolved.
"""

import difflib
import itertools
import re
import zipfile
from pathlib import Path

import generation
from qa import Node, QABlocked, _is_hide_optional, _parse_attrs, parse_root

LETTEROS_COMPONENTS_ZIP = Path(__file__).resolve().parent / "letteros_components.zip"

# Атрибуты редактора Letteros — присутствуют в letteros_components.zip (то есть в
# canonical-исходнике), но систематически отсутствуют в реально отправленном
# письме (проверено на mail_project/test/Копия — Копия — Футер Школа.html).
# letteros-element/letteros-module сюда же — они есть только на обёртке
# canonical-компонента и не переживают экспорт.
LETTEROS_EDITOR_ONLY_ATTRS = {
    "letteros-element", "letteros-module",
    "letteros-align", "letteros-background", "letteros-background-outlook",
    "letteros-border-color", "letteros-border-radius", "letteros-resizer",
    "letteros-tag", "letteros-no-utm",
}

# Пустые нестандартные атрибуты, найденные при анализе библиотеки (2 из 121
# файлов: "Контентные блоки/Блок с Telegram.html" и "...Telegram-каналом
# онлайн-лектория.html" — `telegram=""`/`telegram-=""` на обёртке <tr>).
# Похоже на артефакт редактора, а не содержательный признак — исключается из
# сравнения по тому же принципу, что и letteros-*.
#
# bis_size/bis_id — артефакт сохранения страницы из браузера (задокументирован
# в LEGACY_NEWSLETTER_MIGRATION.md как встречающийся на <a>/<img> в некоторых
# real fixtures — "Мск"/"ГИ special"), не встречается ни в одном из 121
# canonical-компонентов ни в одной из двух библиотек.
KNOWN_EDITOR_ARTIFACT_ATTRS = {"telegram", "telegram-", "bis_size", "bis_id"}

_IGNORED_ATTR_NAMES = LETTEROS_EDITOR_ONLY_ATTRS | KNOWN_EDITOR_ARTIFACT_ATTRS

# letteros-hide тоже не переживает экспорт (проверено — 0 вхождений "letteros-"
# в реальном письме, включая letteros-hide), поэтому как ИМЯ атрибута он не
# сравнивается наравне с остальным editor-only шумом. Но, в отличие от него,
# его ЗНАЧЕНИЕ содержательно — это сигнал штатной опциональности конкретной
# строки/блока (используется отдельно в _shape_signature()/_is_hide_optional(),
# не через это множество).
_ATTRS_ABSENT_ON_INSTANCE = _IGNORED_ATTR_NAMES | {"letteros-hide"}

# Атрибуты, значение которых — содержимое конкретного письма, а не структура
# компонента: при структурной проверке не сравниваются вовсе (как в qa.py).
# ("td", "background") — второй (после <img src>) канал картинки: табличный
# background-фоллбэк. Подтверждено на всех 121 canonical-парах
# letteros_components.zip <-> unisender_components.zip (эксперимент "121 пара").
_VARIABLE_ATTRS = {("img", "src"), ("img", "alt"), ("a", "href"), ("td", "background")}

# CSS-свойство, значение которого — тоже картинка-контент (третий канал после
# <img src> и <td background>), не структура — не сравнивается при проверке style.
_VARIABLE_STYLE_PROPS = {"background-image"}

# "position: relative" — технический production-шум Letteros-экспорта, не
# содержательный признак (см. диагностика к этому изменению): один и тот же
# canonical-компонент (например, "Разделители/Отступ между блоками 16px")
# встречается в одном и том же реальном письме и с этим свойством, и без —
# на идентичном по смыслу содержимом. Тот же паттерн — на шапках, блоках
# расписания и других обёртках во всех 4 реальных письмах проекта. В самой
# canonical-библиотеке (обе версии) "position: relative" на живых тегах не
# встречается ни разу — единственное вхождение (Текстовые блоки/Маркированный
# список) находится внутри MSO-комментария на теге <v:oval>, который в эту
# функцию (сравнение атрибута style обычного тега) не попадает вовсе.
#
# В отличие от _VARIABLE_STYLE_PROPS выше, это НЕ "игнорировать свойство
# всегда" — position: absolute/fixed/sticky, и position с любым значением на
# стороне canonical, по-прежнему сравниваются строго. Поэтому это не
# добавляется в _VARIABLE_STYLE_PROPS (тот набор снимает свойство с ОБЕИХ
# сторон безусловно — здесь же снимается только с одной стороны и только при
# конкретном значении, см. _style_equal()).
_LETTEROS_NOISE_POSITION_VALUE = "relative"

# Четвёртый канал: VML-фоллбэк картинки для Outlook (<v:image src="...">) живёт
# внутри MSO-условного HTML-комментария, который иначе сравнивается как единый
# непрозрачный блок текста. Комментарий, содержащий такой тег, — тоже контент.
_VML_IMAGE_COMMENT_RE = re.compile(r"<v:image\b", re.IGNORECASE)

_TR_OPEN_RE = re.compile(r"<tr\b[^>]*>", re.IGNORECASE)
_TD_OPEN_RE = re.compile(r"<td\b[^>]*>", re.IGNORECASE)
_ONLY_WHITESPACE_RE = re.compile(r"\A\s*\Z")
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_STYLE_PROP_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;]+)")

# Свойства, для которых в данных наблюдалась только одна нормализация —
# схлопывание одинакового shorthand-значения ("15px 15px" -> "15px"). Другие
# формы box-shorthand (разные стороны) не встречались и не обрабатываются —
# осознанно не расширяем сверх того, что подтверждено реальными файлами.
_BOX_SHORTHAND_PROPS = {"padding", "margin", "border-radius"}

# Модули, для которых recognize_components() допускает module-level fallback
# (module определён, element = None), если structural match не выбрал ровно
# один canonical element. Список составлен не эвристически, а по факту
# диагностики на всех 4 реальных production-письмах проекта:
#   - все элементы внутри "Шапки" и все элементы внутри "Подвалы" структурно
#     идентичны друг другу (отличаются только src/href/текстом — уже
#     игнорируемым "переменным" содержимым), поэтому structural match для
#     них либо подтверждает сразу несколько кандидатов, либо ни одного —
#     никогда не может дать единственный, даже на чистых данных;
#   - при этом "подпись" обёртки (align/bgcolor/background-color/padding/
#     border-radius) для ЭТИХ ДВУХ модулей однозначна: ни одна из 5
#     fingerprint-групп, которым принадлежит хоть один элемент "Шапки" или
#     "Подвалы", не пересекается ни с одним элементом любого из 10 остальных
#     модулей библиотеки (проверено по всем 121 canonical-компонентам).
# Ни для одного другого модуля эта проверка не проводилась и не
# подтверждена — расширять этот набор без отдельной диагностики нельзя.
MODULE_LEVEL_ELIGIBLE_MODULES = {"Шапки", "Подвалы"}

# Имя модуля "Мероприятия" -- используется отдельным module-level fallback'ом
# (класс 2, см. _find_schedule_header_signature()/_build_schedule_header_
# signatures() ниже), НЕ через MODULE_LEVEL_ELIGIBLE_MODULES/fingerprint:
# подпись обёртки для "Мероприятия" пересекается с другими модулями
# (например, "Дополнительные Баннеры" -- см. диагностику), поэтому обычный
# "единственный модуль среди кандидатов по fingerprint" здесь не работает.
SCHEDULE_MODULE_NAME = "Мероприятия"


class RecognitionError(Exception):
    """Библиотека canonical Letteros-компонентов недоступна или повреждена."""


class RecognizedComponent:
    """element может быть None -- это значит "module-level" распознавание
    (см. MODULE_LEVEL_ELIGIBLE_MODULES): модуль определён надёжно, но
    конкретный canonical element внутри него сознательно не выбирается,
    потому что structural match не может выбрать между ними, а сами они
    внутри модуля структурно неразличимы (отличаются только содержимым)."""

    __slots__ = ("module", "element", "order", "start", "end", "html", "match_basis", "match_mode")

    def __init__(self, module: str, element: str | None, order, start, end, html, match_basis, match_mode="full"):
        self.module = module
        self.element = element
        self.order = order
        self.start = start
        self.end = end
        self.html = html
        self.match_basis = match_basis
        # "full" -- обычное совпадение, production HTML используется как есть
        #   (adapt_wrapper() меняет только оболочку, см. letteros_adapt.py);
        # "wrapper_stripped" -- найден по внутренней <tr> без внешней card-
        #   обвязки (класс 1); letteros_adapt должен СОБРАТЬ полный canonical
        #   UniSender-блок вокруг production-контента, а не сохранить html
        #   как есть;
        # "heading_text_injection" -- найден по font-size заголовка (класс
        #   3); letteros_adapt должен взять canonical UniSender HTML и
        #   заменить в нём sample-текст на текст, извлечённый из html.
        self.match_mode = match_mode

    def __repr__(self):
        element_repr = self.element if self.element is not None else "<module-level>"
        return f"RecognizedComponent({self.module}/{element_repr}, order={self.order}, {self.start}:{self.end})"


class UnresolvedCandidate:
    """Позиция, где найдено совпадение по "подписи" обёртки (align/bgcolor/
    padding/border-radius), но структурная проверка не подтвердила ни один
    из кандидатов — компонент не распознан, причина зафиксирована явно."""

    __slots__ = ("start", "end", "candidate_names", "reasons")

    def __init__(self, start, end, candidate_names, reasons):
        self.start = start
        self.end = end
        self.candidate_names = candidate_names
        self.reasons = reasons

    def __repr__(self):
        return f"UnresolvedCandidate({self.start}:{self.end}, candidates={self.candidate_names})"


class RecognitionResult:
    __slots__ = ("matches", "unresolved")

    def __init__(self, matches, unresolved):
        self.matches = matches
        self.unresolved = unresolved


class LetterosEntry:
    __slots__ = (
        "module", "element", "html", "root", "fingerprint", "stripped_html", "stripped_fingerprint",
        "form_model",
    )

    def __init__(
        self, module, element, html, root, fingerprint, stripped_html=None, stripped_fingerprint=None,
        form_model=None,
    ):
        self.module = module
        self.element = element
        self.html = html
        self.root = root
        self.fingerprint = fingerprint
        # см. extract_wrapper_stripped_inner() -- None, если компонент не
        # имеет фигуры "card-обвязка + MSO + ровно одна внутренняя <tr>".
        self.stripped_html = stripped_html
        self.stripped_fingerprint = stripped_fingerprint
        # см. build_form_model() -- единая FORM/CONTENT/TOLERATED/OPTIONAL
        # классификация всего дерева этого компонента (RECOGNITION_ARCHITECTURE_
        # AUDIT.md): читается candidate index'ом (build_candidate_index()),
        # structural_match()/_nodes_match() и адаптацией (letteros_adapt.py,
        # strip_content_leaf_formatting()) -- основной, реально используемый
        # путь для "полных" (match_mode="full") совпадений и их обвязки.
        # Раньше здесь отдельно хранился ещё content_leaf_spans (только
        # CONTENT-часть той же классификации) -- убран, полностью поглощён
        # form_model (ничто в recognition/adaptation больше не читало
        # content_leaf_spans отдельно от него, см. RECOGNITION_ARCHITECTURE_
        # AUDIT.md, шаг 4).
        self.form_model = form_model


# --- нормализация значений для сравнения -----------------------------------

def _normalize_color_token(value: str) -> str:
    return _HEX_COLOR_RE.sub(lambda m: m.group(0).lower(), value)


def _normalize_box_value(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    parts = value.split()
    if len(parts) > 1 and len(set(parts)) == 1:
        return parts[0]
    return value


def _parse_style(style: str) -> dict:
    props = {}
    for m in _STYLE_PROP_RE.finditer(style or ""):
        name = m.group(1).strip().lower()
        value = _normalize_color_token(m.group(2).strip())
        if name in _BOX_SHORTHAND_PROPS:
            value = _normalize_box_value(value)
        props[name] = value
    return props


def _style_equal(
    a: str, b: str, extra_variable_style_props: frozenset = frozenset(),
    node_style_roles: dict | None = None,
) -> bool:
    """node_style_roles -- {css-свойство: FormRole} ИМЕННО этого canonical-
    узла (см. FormModel/NodeClassification.style_roles ниже в файле):
    свойства с ролью CONTENT/TOLERATED снимаются с обеих сторон так же, как и
    _VARIABLE_STYLE_PROPS/extra_variable_style_props -- единый путь для
    padding внешней card-обвязки "Текстовые блоки/Текст NNpx" (TOLERATED,
    см. build_form_model()) в дополнение к уже глобальным content-каналам."""
    pa, pb = _parse_style(a), _parse_style(b)
    for prop in _VARIABLE_STYLE_PROPS | extra_variable_style_props:
        pa.pop(prop, None)
        pb.pop(prop, None)
    if node_style_roles:
        for prop, role in node_style_roles.items():
            if role in (FormRole.CONTENT, FormRole.TOLERATED):
                pa.pop(prop, None)
                pb.pop(prop, None)
    # "position: relative" — см. _LETTEROS_NOISE_POSITION_VALUE. НЕ выражено
    # через FormModel/node_style_roles выше: FormModel сегодня классифицирует
    # только CSS-свойства, которые canonical-узел САМ объявляет в своём style
    # (см. _classify_node()), а этот случай — ровно противоположный (canonical
    # НЕ объявляет "position" ни у одного из 121 компонентов ни в одной из
    # двух библиотек, см. диагностику) — представить это как "роль узла"
    # некорректно без изменения самой структуры FormModel (вне рамок этого
    # шага, см. RECOGNITION_ARCHITECTURE_AUDIT.md). Сохранён как есть —
    # единственный оставшийся здесь механизм старого образца. Снимается
    # только когда canonical (a) вообще не задаёт position И инстанс (b)
    # задаёт его РОВНО как "relative" — ни на йоту шире:
    #   - canonical без position + инстанс без position -> уже равны, эта
    #     ветка не нужна;
    #   - canonical без position + инстанс "relative" -> снимаем, считаем
    #     равными (сам запрошенный случай);
    #   - canonical без position + инстанс "absolute"/"fixed"/"sticky"/... ->
    #     условие "== relative" ложно, position остаётся у инстанса,
    #     сравнение словарей не совпадёт (как и должно);
    #   - canonical ЗАДАЁТ position (любое значение) -> условие "position not
    #     in pa" ложно, эта ветка не срабатывает вообще, сравнение строгое.
    if "position" not in pa and pb.get("position") == _LETTEROS_NOISE_POSITION_VALUE:
        del pb["position"]
    return pa == pb


def _attr_value_equal(
    tag: str, name: str, canon_val: str, inst_val: str,
    extra_variable_attrs: frozenset = frozenset(),
    extra_variable_style_props: frozenset = frozenset(),
    node_attr_roles: dict | None = None,
    node_style_roles: dict | None = None,
) -> bool:
    """node_attr_roles/node_style_roles -- роли атрибутов/CSS-свойств ИМЕННО
    этого canonical-узла из FormModel (см. NodeClassification ниже в файле).
    None по умолчанию -- поведение не меняется для вызовов без FormModel
    (см. _resolve_wrapper_stripped_match(), которая сравнивает entry.
    stripped_html -- для него FormModel пока не строится, другое пространство
    смещений, см. build_form_model())."""
    # Атрибут без "=значение" (например, валидный в HTML `nowrap` без
    # значения) парсится qa._parse_attrs() как value=None, а не "" — приводим
    # к строке до сравнения, это не нормализация содержимого, а просто защита
    # от падения на легальной, ничего не значащей для сравнения форме записи.
    canon_val = canon_val or ""
    inst_val = inst_val or ""
    if (tag, name) in _VARIABLE_ATTRS or (tag, name) in extra_variable_attrs:
        return True
    if node_attr_roles and node_attr_roles.get(name) in (FormRole.CONTENT, FormRole.TOLERATED):
        return True
    if name == "style":
        return _style_equal(canon_val, inst_val, extra_variable_style_props, node_style_roles)
    if name == "bgcolor":
        return _normalize_color_token(canon_val.strip().lower()) == _normalize_color_token(inst_val.strip().lower())
    return canon_val.strip() == inst_val.strip()


# --- сигнатура узла для выравнивания детей (адаптация qa.node_signature) ---

def _shape_signature(node: Node):
    """Как qa.node_signature(), но без учёта letteros-* editor-only атрибутов —
    они систематически отсутствуют на стороне инстанса (см. docstring модуля),
    поэтому не должны влиять на выравнивание одинаковых по смыслу узлов."""
    if node.tag in ("#text", "#comment"):
        return (node.tag,)
    hide_value = next((v for n, v in node.attrs if n == "letteros-hide"), None)
    names = tuple(sorted(n for n, _ in node.attrs if n not in _ATTRS_ABSENT_ON_INSTANCE))
    return (node.tag, names, hide_value)


def _align(canon_children, inst_children):
    csig = [_shape_signature(c) for c in canon_children]
    isig = [_shape_signature(c) for c in inst_children]
    sm = difflib.SequenceMatcher(a=csig, b=isig, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace" and (i2 - i1) != (j2 - j1):
            yield "mismatch", range(i1, i2), range(j1, j2)
        elif op == "replace":
            yield "equal", range(i1, i2), range(j1, j2)
        else:
            yield op, range(i1, i2), range(j1, j2)


_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _normalize_comment_whitespace(text: str) -> str:
    """MSO-условные комментарии в canonical-библиотеке отформатированы табами/
    переводами строк для читаемости; в реально отправленном письме та же
    разметка минифицирована в один пробел (подтверждено на реальном письме —
    содержимое комментария идентично, отличаются только пробельные символы).
    Комментарий такого рода не чувствителен к форматированию пробелов, поэтому
    схлопывание — не угадывание содержимого, а нормализация его записи."""
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()


_TBODY_TAG_RE = re.compile(r"</?tbody\s*>", re.IGNORECASE)


def _strip_implicit_tbody(html: str) -> str:
    """Только для стороны инстанса (реального письма), никогда для canonical:
    `letteros_components.zip` не содержит <tbody> ни разу — компоненты
    написаны вручную как <table><tr>... без него.

    Подтверждено на всех 4 реальных production-письмах проекта (см. диагностику
    к этому изменению): <tbody> встречается только в форме без атрибутов
    (`<tbody>`), количество открывающих и закрывающих тегов в каждом файле
    совпадает (123/123, 80/80, 159/159, 45/45) — то есть это не содержательная
    разметка, а типичная для HTML5-DOM вставка неявного <tbody> вокруг строк
    <table>, добавляемая тем, через что письмо прошло перед сохранением
    (почтовый клиент/браузер печатают DOM, а не исходную разметку). Снимается
    только сама пара тегов — их содержимое не трогается."""
    return _TBODY_TAG_RE.sub("", html)


# --- структурное сравнение (не "чинит", только подтверждает/отклоняет) -----

def _nodes_match(
    cn: Node, ct: str, inn: Node, it: str, diffs: list, path: str,
    extra_variable_attrs: frozenset = frozenset(),
    extra_variable_style_props: frozenset = frozenset(),
    form_model: "FormModel | None" = None,
) -> bool:
    if cn.tag != inn.tag:
        diffs.append(f"{path}: <{cn.tag}> ожидался, найден <{inn.tag}>")
        return False

    if cn.tag == "#text":
        return True  # содержимое текста — не структура, не сравнивается

    if cn.tag == "#comment":
        c_raw = ct[cn.start:cn.end]
        i_raw = it[inn.start:inn.end]
        if _VML_IMAGE_COMMENT_RE.search(c_raw) or _VML_IMAGE_COMMENT_RE.search(i_raw):
            return True  # VML-фоллбэк картинки для Outlook — контент, не структура
        c = _normalize_comment_whitespace(c_raw)
        i = _normalize_comment_whitespace(i_raw)
        if c != i:
            diffs.append(f"{path}: комментарий отличается от canonical")
            return False
        return True

    canon_attrs = {n: v for n, v in cn.attrs if n not in _ATTRS_ABSENT_ON_INSTANCE}
    # Симметричная фильтрация: на стороне реального письма этих имён и так
    # никогда нет (см. модульный docstring) — фильтр здесь не влияет на
    # распознавание в проде, но делает функцию корректной и для другого
    # случая, где обе стороны — «полноценные» canonical-компоненты (например,
    # результат letteros_adapt.adapt_wrapper(), который сознательно СОХРАНЯЕТ
    # letteros-element/module/hide/no-utm) — без этого такие валидные
    # совпадения ложно считались бы различием только по имени атрибута.
    inst_attrs = {n: v for n, v in inn.attrs if n not in _ATTRS_ABSENT_ON_INSTANCE}
    # Роли атрибутов/CSS-свойств ИМЕННО этого canonical-узла (FormModel, см.
    # ниже в файле) -- None, если form_model не передан вызывающим кодом
    # (см. docstring structural_match()) или если для cn нет классификации.
    classification = form_model.get(cn) if form_model is not None else None
    node_attr_roles = classification.attr_roles if classification is not None else None
    node_style_roles = classification.style_roles if classification is not None else None
    if "class" not in canon_attrs and inst_attrs.get("class") == "":
        # В canonical-библиотеке (обе версии, letteros_components.zip и
        # unisender_components.zip) class встречается только со значениями
        # {mob_100, mob_br, mob_fs_34, mob_fs_48} — ни разу пустым (проверено
        # по всем 121 компонентам). В реальных письмах пустой class="" —
        # частый (29-50 раз на письмо во всех 4 проверенных файлах) и
        # встречается именно там, где в каноне этого атрибута нет вовсе.
        # Значимый class (в т.ч. из списка выше) по-прежнему сравнивается
        # точно — здесь снимается только пустое значение там, где в каноне
        # атрибута нет совсем.
        del inst_attrs["class"]
    if "style" not in canon_attrs and "style" in inst_attrs:
        # Тот же принцип, что и для class="" выше, но для style: canon может
        # вообще не задавать style на этом теге, а инстанс — добавлять
        # style="position: relative;" целиком (не как одно из нескольких
        # CSS-свойств уже существующего style, а как единственный
        # присутствующий атрибут). _style_equal() ниже сравнивает ЗНАЧЕНИЯ
        # style, когда style есть у ОБЕИХ сторон — сюда он не попадает,
        # потому что здесь style у canon нет вовсе, различается сам набор
        # имён атрибутов. Снимаем "position: relative" (и только его) из
        # разобранного style инстанса; если после этого в style инстанса
        # ничего не остаётся — считаем, что style у инстанса тоже
        # отсутствовал, и убираем атрибут целиком. Если остаётся что-то ещё
        # (любое другое реальное CSS-свойство) — style не убирается, набор
        # атрибутов остаётся разным, как и должно быть.
        inst_style_props = _parse_style(inst_attrs["style"])
        if inst_style_props.get("position") == _LETTEROS_NOISE_POSITION_VALUE:
            del inst_style_props["position"]
        if not inst_style_props:
            del inst_attrs["style"]
    if set(canon_attrs) != set(inst_attrs):
        diffs.append(
            f"{path}<{cn.tag}>: набор атрибутов отличается ({sorted(canon_attrs)} vs {sorted(inst_attrs)})"
        )
        return False
    for name, cval in canon_attrs.items():
        if not _attr_value_equal(
            cn.tag, name, cval, inst_attrs[name], extra_variable_attrs, extra_variable_style_props,
            node_attr_roles, node_style_roles,
        ):
            diffs.append(f"{path}<{cn.tag} {name}>: {cval!r} != {inst_attrs[name]!r}")
            return False

    if classification is not None and classification.whole_role == FormRole.CONTENT:
        # FORM этого узла (tag + обязательные атрибуты) уже подтверждена выше.
        # canonical сам объявляет эту позицию текстовой (единственный ребёнок --
        # #text) -- дети инстанса (чистый текст, <strong>/<span> и т.п.) это
        # CONTENT, не FORM, дальше по дереву не сравниваются вовсе. Раньше --
        # прямая проверка (cn.start, cn.end) in content_leaf_spans; теперь то
        # же самое, но через FormModel (см. build_form_model()).
        return True

    ok = True
    for kind, c_range, i_range in _align(cn.children, inn.children):
        if kind == "equal":
            for ci, ii in zip(c_range, i_range):
                if not _nodes_match(
                    cn.children[ci], ct, inn.children[ii], it, diffs, f"{path}>{cn.tag}",
                    extra_variable_attrs, extra_variable_style_props, form_model,
                ):
                    ok = False
        elif kind == "delete":
            for ci in c_range:
                node = cn.children[ci]
                # OPTIONAL -- через FormModel, если она известна и содержит
                # классификацию этого узла; иначе (form_model не передан,
                # или это узел стороны, для которой FormModel не строится --
                # см. docstring structural_match()) -- прежняя прямая проверка
                # letteros-hide, ровно тот же факт, что уже использует и сама
                # FormModel (см. _classify_node()).
                node_classification = form_model.get(node) if form_model is not None else None
                optional = (
                    node_classification.whole_role == FormRole.OPTIONAL
                    if node_classification is not None
                    else _is_hide_optional(node)
                )
                if not optional:
                    diffs.append(f"{path}>{cn.tag}: отсутствует обязательный узел canonical-компонента")
                    ok = False
        elif kind in ("insert", "mismatch"):
            diffs.append(f"{path}>{cn.tag}: структура не совпадает с canonical-компонентом ({kind})")
            ok = False
    return ok


def structural_match(
    canon_html: str, inst_html: str,
    extra_variable_attrs: frozenset = frozenset(),
    extra_variable_style_props: frozenset = frozenset(),
    form_model: "FormModel | None" = None,
) -> tuple[bool, list]:
    """Публичная точка входа для сравнения одного фрагмента с одним canonical
    Letteros-компонентом. Возвращает (совпадает, список_причин_несовпадения).

    Перед сравнением из inst_html снимается неявный <tbody> (см.
    _strip_implicit_tbody); при сравнении атрибутов узла снимается пустой
    class="" там, где canonical-компонент вообще не задаёт class; при
    сравнении CSS в style снимается добавленный инстансом "position:
    relative" там, где canonical вообще не задаёт position (см.
    _style_equal/_LETTEROS_NOISE_POSITION_VALUE) — единственная оставшаяся
    здесь нормализация старого образца, НЕ выражённая через FormModel (см.
    комментарий внутри _style_equal() -- почему). Остальные наблюдавшиеся на
    реальных письмах отличия сознательно НЕ нормализуются глобально -- см.
    диагностику.

    extra_variable_attrs/extra_variable_style_props — точечное, по умолчанию
    пустое расширение "переменных" каналов (как _VARIABLE_ATTRS/
    _VARIABLE_STYLE_PROPS выше, но не глобальное): используется только вызовом
    из _resolve_wrapper_stripped_match() для уже подтверждённого узкого
    случая (image-slot компонентов при wrapper-stripped распознавании, см.
    _IMAGE_SLOT_EXTRA_VARIABLE_*) — не меняет поведение при вызове без них.
    Этот путь пока НЕ подключён к FormModel (см. ниже) -- он сравнивает
    entry.stripped_html, а не entry.html, то есть другое пространство
    смещений, для которого FormModel сегодня не строится.

    form_model — LetterosEntry.form_model этого же canonical-компонента (см.
    build_form_model() ниже в файле): единая FORM/CONTENT/TOLERATED/OPTIONAL
    классификация его дерева -- заменяет собой то, что раньше передавалось
    отдельно как content_leaf_spans (CONTENT для content-leaf узлов), и
    дополнительно учитывает per-узловые TOLERATED-исключения (сегодня одно —
    padding внешней card-обвязки "Текстовые блоки/Текст NNpx", см.
    build_form_model()). По умолчанию None (поведение не меняется, если не
    передано — используется вызовами, для которых FormModel ещё не построена,
    см. extra_variable_attrs выше)."""
    diffs: list = []
    inst_html = _strip_implicit_tbody(inst_html)
    try:
        canon_root = parse_root(canon_html)
        inst_root = parse_root(inst_html)
    except QABlocked as exc:
        return False, [f"не удалось разобрать фрагмент как единый HTML-элемент: {exc}"]
    ok = _nodes_match(
        canon_root, canon_html, inst_root, inst_html, diffs, "",
        extra_variable_attrs, extra_variable_style_props, form_model,
    )
    return ok, diffs


# --- загрузка canonical Letteros-библиотеки и построение индекса подписей --

def _wrapper_td(root: Node):
    elements = [c for c in root.children if c.tag not in ("#text", "#comment")]
    if len(elements) != 1 or elements[0].tag != "td":
        return None
    return elements[0]


# --- FORM/CONTENT: "content-leaf" узлы (canonical-узел, чей единственный
# ребёнок -- #text) -- определение перенесено в _classify_node()/
# build_form_model() (см. ниже в файле, секция FormModel): там же, где и
# остальная FORM/CONTENT/TOLERATED/OPTIONAL классификация узла, а не отдельной
# функцией/полем. Раньше здесь была отдельная _find_content_leaf_spans() и
# LetterosEntry.content_leaf_spans — обе убраны (RECOGNITION_ARCHITECTURE_
# AUDIT.md, шаг 4): recognition/adaptation больше нигде их не читали, вся
# работа уже шла через LetterosEntry.form_model.


# --- CONTENT extraction/injection для content-leaf узлов --------------------
#
# Второй технический шаг архитектуры FORM -> CONTENT: если canonical-узел на
# CONTENT-позиции (по FormModel) уже структурно подтверждён (structural_match()
# с тем же form_model вернул True), его production-содержимое можно
# безопасно прочитать как ЧИСТЫЙ ТЕКСТ (extract_text()) и подставить назад в
# production-фрагмент ВМЕСТО оригинального поддерева -- убирая ручное inline-
# форматирование (<strong>/<span>/...), не трогая остальной HTML (картинки,
# ссылки, прочие атрибуты остаются производственными, как и раньше в match_
# mode="full").
#
# Разрешённый "безопасный" контент -- явный, небольшой enum inline-тегов
# форматирования (по аналогии с _VARIABLE_ATTRS/LETTEROS_EDITOR_ONLY_ATTRS
# выше -- ничего не разрешается без явного перечисления). Если внутри
# content-leaf позиции встречается что-то за пределами этого списка (img/
# table/a/td и т.п.) -- это НЕ считается текстом, ничего не угадывается и не
# извлекается частично (см. strip_content_leaf_formatting()).
_CONTENT_LEAF_INLINE_TAGS = {"strong", "b", "span", "em", "i", "u", "br"}


def _is_safe_content_subtree(node: Node) -> bool:
    for child in node.children:
        if child.tag == "#text":
            continue
        if child.tag not in _CONTENT_LEAF_INLINE_TAGS:
            return False
        if not _is_safe_content_subtree(child):
            return False
    return True


def strip_content_leaf_formatting(canon_html: str, inst_html: str, form_model: "FormModel") -> str | None:
    """Параллельный обход canon_html/inst_html (тот же принцип выравнивания
    детей, что и в _nodes_match/_align -- предполагается, что structural_match
    с ЭТИМ ЖЕ form_model уже вернул True для этой пары): на каждой
    CONTENT-позиции canonical (form_model.get(cn).whole_role == FormRole.
    CONTENT -- то же самое, что раньше проверялось через content_leaf_spans
    напрямую, теперь единообразно с _nodes_match) заменяет содержимое
    соответствующего узла production на извлечённый чистый текст
    (extract_text(), без <strong>/<span>/... -- см. _is_safe_content_subtree());
    всё остальное в inst_html (картинки, ссылки, прочие узлы/атрибуты)
    остаётся без изменений.

    Возвращает None, если хотя бы одна CONTENT-позиция содержит в production
    что-то за пределами простого текста и разрешённых inline-обёрток (img/
    table/a/... ) -- ничего не подставляется частично, вызывающий код должен
    считать компонент не готовым к автоматической CONTENT-инъекции.

    Как и structural_match(), для выравнивания разбирает inst_html после
    снятия неявного <tbody> (_strip_implicit_tbody) -- явные <tbody>, если они
    были в production, в результате не сохраняются; это инертный для
    HTML-писем артефакт (см. _strip_implicit_tbody docstring), а не
    содержательное изменение."""
    working_html = _strip_implicit_tbody(inst_html)
    try:
        canon_root = parse_root(canon_html)
        inst_root = parse_root(working_html)
    except QABlocked:
        return None

    replacements: list[tuple[int, int, str]] = []

    def visit(cn: Node, inn: Node) -> bool:
        if cn.tag in ("#text", "#comment"):
            return True
        classification = form_model.get(cn)
        if classification is not None and classification.whole_role == FormRole.CONTENT:
            if not inn.children:
                return True  # пусто с обеих сторон -- заменять нечего
            if not _is_safe_content_subtree(inn):
                return False
            text = extract_text(inn, working_html).strip()
            replacements.append((inn.children[0].start, inn.children[-1].end, text))
            return True
        for kind, c_range, i_range in _align(cn.children, inn.children):
            if kind != "equal":
                continue  # структура уже подтверждена structural_match() ранее
            for ci, ii in zip(c_range, i_range):
                if not visit(cn.children[ci], inn.children[ii]):
                    return False
        return True

    if not visit(canon_root, inst_root):
        return None

    for start, end, text in sorted(replacements, key=lambda r: r[0], reverse=True):
        working_html = working_html[:start] + text + working_html[end:]
    return working_html


# --- "production-вариант без внешней card-обвязки" (класс 1) ---------------
#
# Систематический паттерн, подтверждённый диагностикой на 4 real fixtures
# (кнопка, картинка, event-тизер): canonical-компонент часто устроен как
#   <tr><td [bgcolor/padding-обвязка]>
#     <!--[if MSO]--> <table ...> <![endif]-->   (необязательно)
#     <table ...> {ВНУТРЕННЯЯ_TR} </table>
#     <!--[if MSO]--> ... <![endif]-->            (необязательно)
#   </td></tr>
# а реальное production-письмо содержит только {ВНУТРЕННЯЯ_TR} — без внешней
# bgcolor/padding-обвязки и MSO ghost-table. Извлечение механическое (не
# угадывание): работает только когда единственный <td> обёртки сводится
# ровно к одной <table>, чьи прямые дети — только <tr> (без иного контента
# на этом уровне).

def _single_wrapped_table_rows(html: str):
    """<tr><td>[#comment]? <table>...ROWS...</table> [#comment]?</td></tr> ->
    (html_фактически_разобранный, список дочерних <tr> этой единственной
    <table> (>=1)), иначе None, если фигура не такая (другой набор детей у
    <td>, не ровно одна <table>, или её прямые дети — не только <tr>).

    Возвращает html вместе со списком, а не просто список, потому что html
    предварительно пропускается через _strip_implicit_tbody() (как и в
    structural_match() — реальные письма содержат неявный <tbody>, которого
    нет в canonical-библиотеке); Node.start/end у результата — смещения
    именно в ЭТОМ (возможно, отличающемся от входного) html, поэтому срез
    вызывающая сторона обязана брать из него, а не из исходного аргумента."""
    html = _strip_implicit_tbody(html)
    try:
        root = parse_root(html)
    except QABlocked:
        return None
    td = _wrapper_td(root)
    if td is None:
        return None
    meaningful = [c for c in td.children if c.tag != "#text"]
    if any(c.tag not in ("table", "#comment") for c in meaningful):
        return None
    tables = [c for c in meaningful if c.tag == "table"]
    if len(tables) != 1:
        return None
    table_children = [c for c in tables[0].children if c.tag != "#text"]
    if not table_children or any(c.tag != "tr" for c in table_children):
        return None
    return html, table_children


def extract_wrapper_stripped_inner(html: str) -> str | None:
    """canonical-компонент в форме "card-обвязка + MSO + ОДНА внутренняя
    <tr>" -> HTML-срез этой внутренней <tr> (форма без обвязки, в которой
    реально приходит production-фрагмент). None, если фигура компонента
    другая (внутренних <tr> не ровно одна) — тогда механизм не применяется."""
    result = split_wrapper_stripped(html)
    if result is None:
        return None
    _prefix, inner, _suffix = result
    return inner


def split_wrapper_stripped(html: str) -> tuple[str, str, str] | None:
    """Как extract_wrapper_stripped_inner(), но возвращает (префикс, внутренняя
    <tr>, суффикс) — три среза, на которые распадается html целиком (prefix +
    inner + suffix == html после снятия неявного <tbody>, см.
    _single_wrapped_table_rows()). Нужно letteros_adapt.py, чтобы СОБРАТЬ
    полный canonical UniSender-блок вокруг production-контента (класс 1) —
    подставить production-содержимое вместо inner, сохранив prefix/suffix
    (внешнюю card-обвязку/MSO) из canonical UniSender-компонента как есть."""
    result = _single_wrapped_table_rows(html)
    if result is None:
        return None
    parsed_html, rows = result
    if len(rows) != 1:
        return None
    tr = rows[0]
    return parsed_html[:tr.start], parsed_html[tr.start:tr.end], parsed_html[tr.end:]


def extract_first_row_html(html: str) -> str | None:
    """Как extract_wrapper_stripped_inner(), но не требует ровно одной
    внутренней строки — берёт первую <tr> единственной <table> обёртки, даже
    если строк несколько. Используется для module-level "Мероприятия" (класс
    2) — там как раз важна только некопирующаяся "шапка" (день/дата), а число
    строк-событий после неё переменное и не проверяется."""
    result = _single_wrapped_table_rows(html)
    if result is None:
        return None
    parsed_html, rows = result
    tr = rows[0]
    return parsed_html[tr.start:tr.end]


def _single_child_td(tr_html: str) -> Node | None:
    """<tr><td ...>...</td></tr> -> сам узел <td> (для чтения его style/attrs
    и извлечения текста) — None, если фрагмент не сводится к одному <td>."""
    try:
        root = parse_root(tr_html)
    except QABlocked:
        return None
    return _wrapper_td(root)


def extract_text(node: Node, html: str) -> str:
    """Весь текст внутри узла (рекурсивно, без учёта тегов/комментариев) --
    "чистый" контент для content-aware переноса (класс 3)."""
    if node.tag == "#text":
        return html[node.start:node.end]
    if node.tag == "#comment":
        return ""
    return "".join(extract_text(c, html) for c in node.children)


# Геометрия конкретного изображения (ширина/скругление) в production всегда
# отличается от canonical по значению, но НЕ содержательна для распознавания
# image-slot компонента: итоговый размер/border-radius в любом случае берётся
# из canonical-слота при последующей сборке (см. IMAGE_SLOTS.md §9-10) --
# сравнивать их при wrapper-stripped распознавании не нужно. В отличие от
# _VARIABLE_ATTRS/_VARIABLE_STYLE_PROPS (глобальные), это применяется только
# точечно (см. recognize_components()), когда кандидат уже определён как
# "единственная картинка в опциональной ссылке" (_is_single_image_slot_shape).
_IMAGE_SLOT_EXTRA_VARIABLE_ATTRS = frozenset({("img", "width")})
_IMAGE_SLOT_EXTRA_VARIABLE_STYLE_PROPS = frozenset({"max-width", "width", "border-radius"})

_IMG_TAG_RE = re.compile(r"<img\b", re.IGNORECASE)
_A_OPEN_TAG_RE = re.compile(r"<a\b", re.IGNORECASE)
_MSO_COMMENT_MARKER = "<!--[if"


def _is_single_image_slot_shape(html: str) -> bool:
    """Ровно одна картинка, максимум одна ссылка, без MSO-комментариев --
    устойчивый структурный признак "это просто image-slot", отличающий такие
    фрагменты от многокартиночных MSO-карточек и т.п. (см. диагностику)."""
    if _MSO_COMMENT_MARKER in html:
        return False
    if len(_IMG_TAG_RE.findall(html)) != 1:
        return False
    if len(_A_OPEN_TAG_RE.findall(html)) > 1:
        return False
    return True


def _resolve_wrapper_stripped_match(fragment_html: str, stripped_index: dict, library: dict, max_depth: int = 3):
    """Пытается найти wrapper_stripped-совпадение (класс 1) по fingerprint'у
    самого fragment_html; если это не даёт результата, но fragment_html сам по
    себе имеет форму "просто проходная обёртка вокруг одной вложенной <tr>"
    (extract_wrapper_stripped_inner(), НЕЗАВИСИМО от того, есть ли у этой
    обёртки card-стилизация) -- пробует найти совпадение уже во вложенной
    <tr>, и так рекурсивно вглубь (до max_depth). Подтверждено на реальных
    письмах (Казань/Мск/ГИ special, см. диагностику): recognize_components()
    иногда находит один и тот же <img> дважды -- как внешний фрагмент (с
    обёрткой <table>) и как вложенный в него внутренний <tr> без неё; без
    этой рекурсии совпадение находится только на внутреннем уровне, а текст
    самой проходной обёртки (открывающий/закрывающий <table>) остаётся вне
    диапазона [start:end] найденного match и считается "разрывом" между
    соседними блоками (см. letteros_migrate._check_no_gaps()).

    Возвращает (module, element, matched_html) или None. matched_html --
    именно найденный (возможно, более глубокий) уровень, который пойдёт в
    letteros_adapt на пересборку; текст промежуточных проходных обёрток в
    matched_html не входит и в canonical-блок не переносится (он не несёт
    содержания — это просто <table>, которую в любом случае не из чего
    восстанавливать на UniSender-стороне)."""
    current = fragment_html
    for _ in range(max_depth):
        td = _single_child_td(current)
        if td is None:
            return None
        fingerprint = _fingerprint_from_td_attrs(dict(td.attrs))
        candidate_keys = stripped_index.get(fingerprint) or []
        if candidate_keys:
            confirmed = []
            for key in candidate_keys:
                entry = library[key]
                if _is_single_image_slot_shape(entry.stripped_html):
                    ok, _diffs = structural_match(
                        entry.stripped_html, current,
                        _IMAGE_SLOT_EXTRA_VARIABLE_ATTRS, _IMAGE_SLOT_EXTRA_VARIABLE_STYLE_PROPS,
                    )
                else:
                    ok, _diffs = structural_match(entry.stripped_html, current)
                if ok:
                    confirmed.append(key)
            if len(confirmed) == 1:
                module, element = confirmed[0]
                return module, element, current
            if len(confirmed) > 1:
                return None  # неоднозначно на этом уровне -- вглубь не лезем, не угадываем

        deeper = extract_wrapper_stripped_inner(current)
        if deeper is None or deeper == current:
            return None
        current = deeper
    return None


# --- module-level "Мероприятия" (класс 2) -----------------------------------
#
# Расписание нельзя сопоставить с конкретным canonical "Расписание N" --
# количество событий/дней в production переменное, а canonical-библиотека
# содержит фиксированные примеры (см. диагностику). Для миграции это и не
# нужно: старый блок "Мероприятия" всегда отбрасывается целиком и заменяется
# новым расписанием из SCHEDULE.txt (letteros_migrate._split_schedule_and_
# determine_insertion()) -- задача recognition здесь только "это Мероприятия".
#
# Устойчивый (не копирующийся по числу событий) признак -- первая строка
# любого "Расписание N": две строки день-недели/дата с конкретными
# font-size/color. Sравниваются только эти два CSS-значения -- ЖИРНОСТЬ
# текста (font-weight в style у canonical vs <strong> у production, тот же
# "ручной формат" эффект, что и в заголовках класса 3) сознательно не
# проверяется здесь: иначе строгий structural_match отклонял бы day-шапку
# по той же причине, что и заголовки.

def _schedule_day_header_signature(tr_html: str) -> tuple | None:
    td = _single_child_td(tr_html)
    if td is None:
        return None
    tables = [c for c in td.children if c.tag == "table"]
    if len(tables) != 1:
        return None
    rows = [c for c in tables[0].children if c.tag == "tr"]
    if len(rows) < 2:
        return None
    sig = []
    for row in rows[:2]:
        row_td = _single_child_td(tr_html[row.start:row.end])
        if row_td is None:
            return None
        style = dict(row_td.attrs).get("style") or ""
        props = _parse_style(style)
        font_size = props.get("font-size")
        color = props.get("color")
        if not font_size or not color:
            return None
        sig.append((font_size, _normalize_color_token(color.strip().lower())))
    return tuple(sig)


def _build_schedule_header_signatures(library: dict) -> set:
    signatures = set()
    for key, entry in library.items():
        if key[0] != "Мероприятия":
            continue
        stripped = extract_wrapper_stripped_inner(entry.html)
        candidates = [entry.html] if stripped is None else [entry.html, stripped]
        for candidate_html in candidates:
            header = extract_first_row_html(candidate_html)
            if header is None:
                continue
            sig = _schedule_day_header_signature(header)
            if sig is not None:
                signatures.add(sig)
    return signatures


def _find_schedule_header_signature(fragment_html: str, max_depth: int = 3):
    """Спускается вглубь фрагмента (первая <tr> очередного уровня обёртки),
    на каждом уровне проверяя сигнатуру day-шапки -- покрывает и уже "голый"
    (без внешней обвязки) production-фрагмент, и фрагмент, где внешняя card-
    обвязка ещё цела (см. docstring выше)."""
    current = fragment_html
    for _ in range(max_depth):
        header = extract_first_row_html(current)
        if header is None:
            return None
        sig = _schedule_day_header_signature(header)
        if sig is not None:
            return sig
        current = header
    return None


# --- content-aware заголовки "Текстовые блоки/Заголовок NNpx" (класс 3) ----
#
# Production-письма нередко форматируют заголовок вручную не так, как
# canonical (align, <strong>/<span> вместо CSS font-weight, лишний class от
# другого варианта, другой line-height/letter-spacing -- см. диагностику).
# Вместо ослабления structural_match для всей библиотеки: узнаём конкретный
# "Заголовок NNpx" по единственному устойчивому признаку -- font-size (он не
# указывается вручную поверх компонента, в отличие от жирности/выравнивания),
# затем переносим ЧИСТЫЙ ИЗВЛЕЧЁННЫЙ ТЕКСТ (без <strong>/<span>) в canonical
# UniSender-структуру этого конкретного варианта (см. letteros_adapt.py).
_HEADING_ELEMENT_RE = re.compile(r"^Заголовок (\d+)px$")


def _build_heading_font_sizes(library: dict) -> dict:
    sizes = {}
    for key, entry in library.items():
        module, element = key
        if module != "Текстовые блоки" or not _HEADING_ELEMENT_RE.match(element):
            continue
        stripped = extract_wrapper_stripped_inner(entry.html)
        if stripped is None:
            continue
        td = _single_child_td(stripped)
        if td is None:
            continue
        style = dict(td.attrs).get("style") or ""
        font_size = _parse_style(style).get("font-size")
        if font_size:
            sizes[key] = font_size
    return sizes


def extract_heading_text(fragment_html: str) -> str | None:
    """Публичный помощник для letteros_migrate.py: повторно извлекает чистый
    текст заголовка из уже распознанного (match_mode="heading_text_injection")
    production-фрагмента — та же логика, что и внутри
    _try_heading_text_injection(), но без повторного подбора font-size (он уже
    известен -- element зафиксирован в RecognizedComponent)."""
    stripped = extract_wrapper_stripped_inner(fragment_html)
    if stripped is None:
        return None
    td = _single_child_td(stripped)
    if td is None:
        return None
    text = extract_text(td, stripped).strip()
    return text or None


def _try_heading_text_injection(fragment_html: str, heading_font_sizes: dict):
    """Возвращает (module, element, извлечённый_текст) при однозначном
    совпадении по font-size, иначе None."""
    stripped = extract_wrapper_stripped_inner(fragment_html)
    if stripped is None:
        return None
    td = _single_child_td(stripped)
    if td is None:
        return None
    style = dict(td.attrs).get("style") or ""
    font_size = _parse_style(style).get("font-size")
    if not font_size:
        return None
    matching = [key for key, size in heading_font_sizes.items() if size == font_size]
    if len(matching) != 1:
        return None
    module, element = matching[0]
    text = extract_text(td, stripped).strip()
    if not text:
        return None
    return module, element, text


# --- content-aware 2-колоночный grid "Контентные блоки/Вариант 2-4" (класс 4) --
#
# Диагностика подтвердила: canonical "Вариант 2/3/4" — ОДНА structural family,
# побайтово идентичная, кроме accent-цвета card/tag-pill/CTA-иконки правого
# item'а (белый/белый -- Вариант 2, белый/коралл -- Вариант 3, белый/teal --
# Вариант 4; левый item всегда белый). Production, в отличие от canonical:
#   - не фиксирует, на каком item'е (левом/правом) accent;
#   - содержит 0/1/2 tag-pill на item, тогда как canonical -- всегда 2.
# У обоих tag-pild letteros-hide есть в ОБЕИХ библиотеках (Letteros и
# UniSender) -- значит это не "недостающий компонент", а штатная
# опциональность (см. qa.py docstring/generation._remove_hide_row()). Но
# structural_match() не может её подтвердить: значение letteros-hide --
# единственное, что различает одинаковые по тегу/атрибутам соседние
# опциональные узлы, а в production letteros-hide отсутствует по определению.
#
# Поэтому распознавание здесь — НЕ structural_match(), а content-aware
# извлечение (тот же принцип, что и для заголовков, класс 3): по устойчивым
# HTML-маркерам "<!-- item -->"/"<!-- item END-->" (физически присутствуют и в
# canonical, и в production) вычленяется содержимое каждого item'а, затем при
# пересборке (letteros_adapt.py) каждый item строится из СУЩЕСТВУЮЩЕГО
# canonical item-шаблона нужного accent'а -- новый компонент не создаётся.
GRID_MODULE_NAME = "Контентные блоки"

# accent card bgcolor -> ((canonical module, element), откуда взят item-шаблон
# этого accent'а, "left"/"right" -- позиция внутри ЭТОГО canonical-файла).
# Белый существует только на левой позиции Варианта 2 (правая тоже белая, но
# левая уже используется как источник); teal -- только на правой позиции
# Варианта 4; коралл -- только на правой позиции Варианта 3 (см. диагностику:
# левый item во всех трёх canonical-вариантах всегда белый).
GRID_ACCENT_TEMPLATES = {
    "#ffffff": (("Контентные блоки", "Вариант 2"), "left"),
    "#479f98": (("Контентные блоки", "Вариант 4"), "right"),
    "#dd776f": (("Контентные блоки", "Вариант 3"), "right"),
}

_GRID_ITEMS_MARKER_RE = re.compile(r"<!--\s*items\s*-->")
_GRID_ITEM_BLOCK_RE = re.compile(r"<!--\s*item\s*-->(.*?)<!--\s*item END-?-->", re.DOTALL)
GRID_ITEM_OPEN_MARKER = "<!-- item -->"
GRID_ITEM_CLOSE_MARKER = "<!-- item END-->"
# Связка "второй item в MSO-таблице переключается на другую <td>-колонку" --
# невидимый для Outlook-независимых клиентов служебный HTML-комментарий,
# который в canonical-файле физически лежит ВНУТРИ второго item'а (сразу
# после его "<!-- item -->"), но по смыслу относится не к содержимому item'а,
# а к ПОЗИЦИИ (это второй столбец), поэтому split_grid_items() выносит его в
# "connector" отдельно от content — иначе при переиспользовании item-шаблона
# другого accent'а на ДРУГОЙ позиции (см. GRID_ACCENT_TEMPLATES) переключатель
# колонки "уехал" бы вместе с содержимым не на то место.
_GRID_MSO_COLUMN_SWITCH_RE = re.compile(
    r"\s*<!--\[if \(gte mso 9\)\|\(IE\)\]>.*?<!\[endif\]-->\s*", re.DOTALL | re.IGNORECASE,
)

_GRID_ITEM_CARD_BGCOLOR_RE = re.compile(
    r'bgcolor="(#[0-9A-Fa-f]{6})"[^>]*style="[^"]*padding:\s*28px 15px;\s*border-radius:\s*12px',
    re.IGNORECASE,
)
_GRID_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_GRID_IMG_SRC_RE = re.compile(r'\bsrc="([^"]*)"')
_GRID_IMG_ALT_RE = re.compile(r'\balt="([^"]*)"')
_GRID_TAG_PILL_RE = re.compile(
    r'<td\b[^>]*style="[^"]*border-radius:\s*30px[^"]*"[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE,
)
_GRID_HEADING_TD_RE = re.compile(
    r'<td\b[^>]*style="[^"]*font-size:\s*18px[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE,
)
_GRID_TEXT_TD_RE = re.compile(
    r'<td\b[^>]*style="[^"]*font-size:\s*16px;[^"]*line-height:\s*24px[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE,
)
_GRID_CTA_A_RE = re.compile(
    r'line-height:\s*19px[^"]*"[^>]*>\s*<a\b[^>]*\bhref="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE,
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _strip_tags_to_text(html_fragment: str) -> str:
    return _TAG_STRIP_RE.sub("", html_fragment).strip()


def _extract_grid_item_content(item_html: str) -> dict | None:
    """Извлекает содержимое одного item'а grid'а (accent, картинка, 0-2
    tag-pill'а, заголовок, текст, CTA). None, если обязательные части
    (картинка/заголовок/текст/CTA) не найдены или accent неизвестен —
    осторожность важнее полноты, признак просто не применяется."""
    bgcolor_m = _GRID_ITEM_CARD_BGCOLOR_RE.search(item_html)
    if bgcolor_m is None:
        return None
    accent = bgcolor_m.group(1).lower()
    if accent not in GRID_ACCENT_TEMPLATES:
        return None

    img_m = _GRID_IMG_TAG_RE.search(item_html)
    if img_m is None:
        return None
    src_m = _GRID_IMG_SRC_RE.search(img_m.group(0))
    if src_m is None or not src_m.group(1):
        return None
    alt_m = _GRID_IMG_ALT_RE.search(img_m.group(0))

    heading_m = _GRID_HEADING_TD_RE.search(item_html)
    text_m = _GRID_TEXT_TD_RE.search(item_html)
    cta_m = _GRID_CTA_A_RE.search(item_html)
    if heading_m is None or text_m is None or cta_m is None:
        return None

    tags = [_strip_tags_to_text(m.group(1)) for m in _GRID_TAG_PILL_RE.finditer(item_html)]
    tags = [t for t in tags if t]
    if len(tags) > 2:
        return None

    return {
        "accent": accent,
        "img_src": src_m.group(1),
        "img_alt": alt_m.group(1) if alt_m else "",
        "tags": tags,
        "heading": _strip_tags_to_text(heading_m.group(1)),
        "text": _strip_tags_to_text(text_m.group(1)),
        "cta_href": cta_m.group(1),
        "cta_text": _strip_tags_to_text(cta_m.group(2)),
    }


def extract_grid_items(fragment_html: str) -> list[dict] | None:
    """Публичная точка входа (используется recognize_components() и
    letteros_adapt.py): список из ровно двух item-словарей (в порядке
    появления — левый/правый) или None, если фрагмент не является
    2-колоночным grid'ом семейства "Контентные блоки/Вариант 2-4"."""
    if _GRID_ITEMS_MARKER_RE.search(fragment_html) is None:
        return None
    blocks = _GRID_ITEM_BLOCK_RE.findall(fragment_html)
    if len(blocks) != 2:
        return None
    items = [_extract_grid_item_content(b) for b in blocks]
    if any(item is None for item in items):
        return None
    return items


def split_grid_items(html: str) -> tuple[str, str, str, str, str] | None:
    """Как split_wrapper_stripped(), но для grid'а: (prefix, item1_содержимое,
    connector, item2_содержимое, suffix). item-содержимое — БЕЗ маркеров
    "<!-- item -->"/"<!-- item END-->" и без MSO-переключателя колонки (см.
    _GRID_MSO_COLUMN_SWITCH_RE) — переключатель перенесён в connector, чтобы
    оставаться на правильной позиции независимо от того, откуда взято
    содержимое item'а (letteros_adapt.py переиспользует шаблоны item'ов из
    разных canonical-файлов на разных позициях, см. GRID_ACCENT_TEMPLATES).
    Сборка обратно: prefix + GRID_ITEM_OPEN_MARKER + item1 +
    GRID_ITEM_CLOSE_MARKER + connector + GRID_ITEM_OPEN_MARKER + item2 +
    GRID_ITEM_CLOSE_MARKER + suffix."""
    matches = list(_GRID_ITEM_BLOCK_RE.finditer(html))
    if len(matches) != 2:
        return None
    m1, m2 = matches
    item1 = m1.group(1)
    item2_raw = m2.group(1)
    mso_m = _GRID_MSO_COLUMN_SWITCH_RE.match(item2_raw)
    if mso_m:
        connector = html[m1.end(0):m2.start(0)] + mso_m.group(0)
        item2 = item2_raw[mso_m.end():]
    else:
        connector = html[m1.end(0):m2.start(0)]
        item2 = item2_raw
    return html[:m1.start(0)], item1, connector, item2, html[m2.end(0):]


def _fingerprint_from_td_attrs(attrs: dict) -> tuple:
    align = (attrs.get("align") or "").strip().lower()
    bgcolor = _normalize_color_token((attrs.get("bgcolor") or "").strip().lower())
    style_props = _parse_style(attrs.get("style") or "")
    bgcolor_style = style_props.get("background-color", "")
    padding = style_props.get("padding", "")
    border_radius = style_props.get("border-radius", "")
    return (align, bgcolor, bgcolor_style, padding, border_radius)


def load_letteros_library(zip_path: Path = LETTEROS_COMPONENTS_ZIP) -> dict:
    """module/element -> LetterosEntry, для всех компонентов letteros_components.zip."""
    if not zip_path.is_file():
        raise RecognitionError(f"letteros_components.zip отсутствует: {zip_path}")

    entries = {}
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name == "INDEX.md" or not name.endswith(".html"):
                continue
            module, _, rest = name.partition("/")
            element = rest[:-len(".html")]
            html = z.read(name).decode("utf-8")
            try:
                root = parse_root(html)
            except QABlocked as exc:
                raise RecognitionError(f"{name}: не удалось разобрать canonical-компонент ({exc})")
            td = _wrapper_td(root)
            if td is None:
                raise RecognitionError(f"{name}: обёртка компонента не сводится к одному <td> (нарушение допущения)")
            fingerprint = _fingerprint_from_td_attrs(dict(td.attrs))
            stripped_html = extract_wrapper_stripped_inner(html)
            stripped_fingerprint = None
            if stripped_html is not None:
                stripped_td = _single_child_td(stripped_html)
                if stripped_td is not None:
                    stripped_fingerprint = _fingerprint_from_td_attrs(dict(stripped_td.attrs))
                else:
                    stripped_html = None
            form_model = build_form_model(module, element, root, html)
            entries[(module, element)] = LetterosEntry(
                module, element, html, root, fingerprint, stripped_html, stripped_fingerprint,
                form_model,
            )
    return entries


# Семейство "Текстовые блоки/Текст NNpx" -- используется build_form_model()
# ниже, чтобы объявить padding внешней card-обвязки этих компонентов TOLERATED
# (см. диагностику: 31 реальный gap на 5 production-письмах, align/bgcolor/
# border-radius этой же обвязки ни разу не отличались от canonical). Больше
# нигде не используется -- сама вариативность padding теперь целиком данные
# FormModel, а не отдельный индекс/resolver (см. build_candidate_index() ниже
# в файле).
_TEXT_NNPX_ELEMENT_RE = re.compile(r"^Текст \d+px$")


# =============================================================================
# FORM/CONTENT/TOLERATED/OPTIONAL -- единая canonical-side классификация
# (RECOGNITION_ARCHITECTURE_AUDIT.md, "ПЛАН РЕАЛИЗАЦИИ", шаг 1 из 5).
#
# ЭТО ТОЛЬКО ОПИСАНИЕ ФОРМЫ canonical-компонента, вычисляемое исключительно из
# canonical HTML при загрузке библиотеки -- ничего из production здесь не
# участвует. Ни один из существующих механизмов (fingerprint-индексы,
# structural_match/_nodes_match, content_leaf_spans, _VARIABLE_ATTRS/
# _VARIABLE_STYLE_PROPS, letteros-hide, image-slot, text-padding resolver и
# т.д.) эту классификацию пока не читает и не меняет своего поведения --
# они остаются единственным реально работающим путём recognition/adaptation.
# Задача этого шага -- только свести уже ДОКАЗАННЫЕ правила в одну структуру
# данных рядом со старыми механизмами, для последующей (отдельной) замены
# индексации/structural_match/adapt на неё.
#
# Роль назначается на двух уровнях:
#   - whole_role узла целиком -- OPTIONAL (узел может отсутствовать целиком,
#     как и сегодня определяет letteros-hide/_is_hide_optional), CONTENT
#     (узел -- content-leaf, всё, что ниже, уже не FORM, как и сегодня
#     определяет content_leaf_spans), иначе FORM (узел и его дети по
#     умолчанию участвуют в идентификации формы);
#   - attr_roles/style_roles -- роль ОТДЕЛЬНЫХ атрибутов/CSS-свойств этого
#     узла (используется только когда whole_role == FORM -- у CONTENT/
#     OPTIONAL узлов сами атрибуты уже не имеют значения для формы):
#       CONTENT    -- глобально известные переменные каналы (соответствуют
#                     _VARIABLE_ATTRS/_VARIABLE_STYLE_PROPS/VML-комментарию);
#       TOLERATED  -- ТОЛЬКО два уже доказанных диагностикой случая (не
#                     расширять без отдельного доказательства на реальных
#                     письмах, см. RECOGNITION_ARCHITECTURE_AUDIT.md):
#                       - style-свойство "position" (см.
#                         _LETTEROS_NOISE_POSITION_VALUE -- сама
#                         классификация не учитывает конкретное значение
#                         "relative", это по-прежнему делает _style_equal());
#                       - style-свойство "padding" ИМЕННО на внешней
#                         card-обвязке компонентов "Текстовые блоки/Текст
#                         NNpx" (накладывается ниже, в build_form_model());
#       FORM       -- всё остальное, включая padding/bgcolor/border-radius
#                     ВЛОЖЕННЫХ "плашек"/highlight-box у "Авторы"/
#                     "Мероприятия"/"Контентные блоки" -- там эти же самые
#                     имена атрибутов/свойств реально различают разные
#                     canonical-элементы (см. диагностику) и НЕ становятся
#                     TOLERATED/CONTENT только потому, что где-то в другом
#                     компоненте таким стал одноимённый признак.
# =============================================================================

class FormRole:
    """Роль canonical-узла/атрибута/CSS-свойства в модели. Простые строки-
    константы, не отдельный enum-класс -- по тому же принципу, что и
    letteros_adapt.AdaptationStatus."""
    FORM = "FORM"
    CONTENT = "CONTENT"
    TOLERATED = "TOLERATED"
    OPTIONAL = "OPTIONAL"


class NodeClassification:
    """Роль ОДНОГО canonical-узла. span -- (start, end) в html этой записи
    (тот же формат смещений, что и content_leaf_spans/RecognizedComponent),
    поэтому напрямую сопоставим с offsets, которые уже использует
    _nodes_match() для сравнения того же узла."""
    __slots__ = ("span", "tag", "whole_role", "attr_roles", "style_roles")

    def __init__(self, span: tuple, tag: str, whole_role: str, attr_roles: dict, style_roles: dict):
        self.span = span
        self.tag = tag
        self.whole_role = whole_role
        self.attr_roles = attr_roles    # {имя_атрибута: FormRole}
        self.style_roles = style_roles  # {css_свойство: FormRole}

    def __repr__(self):
        return f"NodeClassification(<{self.tag}> {self.span}, whole={self.whole_role})"


class FormModel:
    """Полная классификация одного canonical-компонента -- по одному
    NodeClassification на каждый обычный HTML-узел его дерева (см.
    build_form_model()). Хранится как LetterosEntry.form_model."""
    __slots__ = ("by_span",)

    def __init__(self, by_span: dict):
        self.by_span = by_span  # (start, end) -> NodeClassification

    def get(self, node: Node) -> "NodeClassification | None":
        return self.by_span.get((node.start, node.end))

    def __repr__(self):
        return f"FormModel({len(self.by_span)} узлов)"


# Глобальные (не зависящие от конкретного canonical-компонента) CONTENT-роли
# -- переиспользуют уже доказанные и работающие множества, не дублируют их
# литералами.
_CONTENT_ATTR_ROLES = {key: FormRole.CONTENT for key in _VARIABLE_ATTRS}
_CONTENT_STYLE_PROP_ROLES = {prop: FormRole.CONTENT for prop in _VARIABLE_STYLE_PROPS}

# Единственные два доказанных TOLERATED-случая (см. заголовок секции выше) --
# "position" применяется глобально (роль не зависит от компонента, как и
# сегодняшний _LETTEROS_NOISE_POSITION_VALUE), "padding" внешней обвязки --
# только для семейства "Текстовые блоки/Текст NNpx" (см. диагностику,
# RECOGNITION_ARCHITECTURE_AUDIT.md).
_GLOBAL_TOLERATED_STYLE_PROPS = frozenset({"position"})
_TOLERATED_OUTER_PADDING_MODULE = "Текстовые блоки"
_TOLERATED_OUTER_PADDING_ELEMENT_RE = _TEXT_NNPX_ELEMENT_RE


def _classify_node(node: Node) -> NodeClassification | None:
    """whole_role/attr_roles/style_roles ровно одного узла -- без учёта
    module/element-специфичных TOLERATED-переопределений (их накладывает
    build_form_model() после первого прохода, точечно). None для #text/
    #comment -- у #text роли нет (её текст либо внутри content-leaf узла,
    либо не структура вовсе, см. _nodes_match()); #comment классифицируется
    отдельно build_form_model() (VML-маркер -- тоже CONTENT, тот же принцип,
    что и в _nodes_match())."""
    if node.tag in ("#text", "#comment"):
        return None

    hide_value = next((v for n, v in node.attrs if n == "letteros-hide"), None)
    if hide_value is not None:
        whole_role = FormRole.OPTIONAL
    elif len(node.children) == 1 and node.children[0].tag == "#text":
        whole_role = FormRole.CONTENT
    else:
        whole_role = FormRole.FORM

    attr_roles: dict = {}
    style_roles: dict = {}
    for name, value in node.attrs:
        if name in _ATTRS_ABSENT_ON_INSTANCE:
            continue  # editor-only -- не переживает экспорт, роли не нужно
        attr_roles[name] = _CONTENT_ATTR_ROLES.get((node.tag, name), FormRole.FORM)
        if name == "style":
            for prop in _parse_style(value):
                if prop in _CONTENT_STYLE_PROP_ROLES:
                    style_roles[prop] = FormRole.CONTENT
                elif prop in _GLOBAL_TOLERATED_STYLE_PROPS:
                    style_roles[prop] = FormRole.TOLERATED
                else:
                    style_roles[prop] = FormRole.FORM

    return NodeClassification((node.start, node.end), node.tag, whole_role, attr_roles, style_roles)


def build_form_model(module: str, element: str, root: Node, html: str) -> FormModel:
    """Строит FormModel для одного canonical-компонента (Letteros ИЛИ
    UniSender -- функция не зависит от того, какая это библиотека, только от
    самого дерева и его исходного html). Обходит дерево целиком, классифицируя
    каждый узел через _classify_node(); затем накладывает единственное сегодня
    доказанное per-компонент TOLERATED-переопределение (padding внешней
    card-обвязки "Текстовые блоки/Текст NNpx" -- см. заголовок секции)."""
    by_span: dict = {}

    def visit(node: Node) -> None:
        if node.tag == "#comment":
            comment_text = html[node.start:node.end]
            role = FormRole.CONTENT if _VML_IMAGE_COMMENT_RE.search(comment_text) else FormRole.FORM
            # VML-комментарий -- редкость на этом уровне обхода (обычно
            # встречается внутри MSO-условного блока картинки); большинство
            # #comment узлов остаются FORM (сравниваются as-is, см.
            # _normalize_comment_whitespace() в _nodes_match()).
            by_span[(node.start, node.end)] = NodeClassification(
                (node.start, node.end), node.tag, role, {}, {},
            )
        else:
            c = _classify_node(node)
            if c is not None:
                by_span[c.span] = c
        for child in node.children:
            visit(child)

    visit(root)

    if module == _TOLERATED_OUTER_PADDING_MODULE and _TOLERATED_OUTER_PADDING_ELEMENT_RE.match(element or ""):
        wrapper = _wrapper_td(root)
        if wrapper is not None:
            classification = by_span.get((wrapper.start, wrapper.end))
            if classification is not None and "padding" in classification.style_roles:
                classification.style_roles["padding"] = FormRole.TOLERATED

    return FormModel(by_span)


# =============================================================================
# Единый generic candidate index (RECOGNITION_ARCHITECTURE_AUDIT.md, "ПЛАН
# РЕАЛИЗАЦИИ", шаг 2 из 5) -- заменяет собой прежние ДВА отдельных индекса
# ("полный" fingerprint и отдельный "text-padding-agnostic" -- см. предыдущую
# версию этого файла): один и тот же принцип для ЛЮБОГО canonical-компонента,
# а не два отдельных, написанных вручную под конкретный случай.
#
# Ключ -- тот же 5-tuple, что и раньше вычисляла _fingerprint_from_td_attrs()
# (align/bgcolor/bgcolor-style/padding/border-radius внешнего <td>), но
# позиции, чья роль у ЭТОГО КОНКРЕТНОГО компонента (по его FormModel, см.
# build_form_model() выше) -- CONTENT или TOLERATED, заменяются на общий
# wildcard-сентинел. Само решение "эта позиция не участвует в идентификации
# формы" целиком приходит из FormModel (данные), а не из кода этой функции
# (код один и тот же для padding-у-"Текст-NNpx" и для любого другого будущего
# TOLERATED-случая, который появится в build_form_model() -- новый код индекса
# добавлять не потребуется). Для подавляющего большинства компонентов
# (без TOLERATED-позиций у обвязки) это ровно тот же ключ, что раньше строил
# fingerprint-индекс.
# =============================================================================

# Позиции 5-tuple fingerprint (см. _fingerprint_from_td_attrs()) -> откуда
# брать роль этой позиции у КОНКРЕТНОГО компонента: ("attr", имя) — из
# NodeClassification.attr_roles внешнего <td>; ("style", свойство) — из
# NodeClassification.style_roles.
_FINGERPRINT_POSITION_SOURCES = (
    ("attr", "align"),
    ("attr", "bgcolor"),
    ("style", "background-color"),
    ("style", "padding"),
    ("style", "border-radius"),
)

# Сентинел "эта позиция ключа — wildcard" (позиция CONTENT/TOLERATED у этого
# компонента, значение не участвует в поиске кандидатов). Отдельный object(),
# не None/"" -- эти значения уже легитимно встречаются как настоящие значения
# полей fingerprint (например, пустой bgcolor-style).
_WILDCARD = object()


def _wrapper_classification(entry: LetterosEntry) -> "NodeClassification | None":
    if entry.form_model is None:
        return None
    wrapper = _wrapper_td(entry.root)
    if wrapper is None:
        return None
    return entry.form_model.get(wrapper)


def _tolerated_fingerprint_positions(classification: "NodeClassification | None") -> frozenset:
    """Индексы (0..4) позиций 5-tuple, чья роль у ЭТОГО componента --
    CONTENT или TOLERATED (см. _FINGERPRINT_POSITION_SOURCES) -- то есть не
    должна участвовать в ключе кандидатного индекса. Пусто, если
    классификация неизвестна (нет FormModel) или ни одна позиция не
    объявлена вариативной -- тогда ключ этого компонента совпадает с его
    обычным entry.fingerprint целиком, как и раньше."""
    if classification is None:
        return frozenset()
    positions = set()
    for i, (kind, name) in enumerate(_FINGERPRINT_POSITION_SOURCES):
        roles = classification.attr_roles if kind == "attr" else classification.style_roles
        if roles.get(name) in (FormRole.CONTENT, FormRole.TOLERATED):
            positions.add(i)
    return frozenset(positions)


def _project_fingerprint(key: tuple, wildcard_positions: frozenset) -> tuple:
    return tuple(_WILDCARD if i in wildcard_positions else v for i, v in enumerate(key))


def build_candidate_index(library: dict) -> tuple[dict, list]:
    """Единый generic candidate index. Возвращает (index, wildcard_patterns):

    index -- {проекция 5-tuple: [(module, element), ...]}. Каждый компонент
    регистрируется под ВСЕМИ проекциями своего ключа -- под точным
    entry.fingerprint И под каждой проекцией с любым непустым подмножеством
    его собственных tolerated-позиций, замененных на wildcard (полный набор
    подмножеств; на практике сегодня либо 0, либо 1 tolerated-позиция на
    компонент -- "Текстовые блоки/Текст NNpx" -- значит не более 2 записей на
    компонент, без комбинаторного взрыва).

    wildcard_patterns -- отсортированный список всех НЕПУСТЫХ множеств
    позиций, реально встретившихся хотя бы у одного компонента библиотеки --
    нужен production-стороне (см. find_candidates()), чтобы знать, какие
    проекции своего собственного (всегда точного, "полного") ключа вообще
    имеет смысл пробовать. Это данные, извлечённые из библиотеки, а не
    захардкоженный список атрибутов."""
    index: dict = {}
    wildcard_patterns: set = set()
    for key, entry in library.items():
        classification = _wrapper_classification(entry)
        tolerated = _tolerated_fingerprint_positions(classification)
        for r in range(len(tolerated) + 1):
            for subset in itertools.combinations(sorted(tolerated), r):
                subset = frozenset(subset)
                projected = _project_fingerprint(entry.fingerprint, subset)
                index.setdefault(projected, []).append(key)
                if subset:
                    wildcard_patterns.add(subset)
    return index, sorted(wildcard_patterns, key=lambda s: (len(s), sorted(s)))


def find_candidates(fragment_attrs: dict, index: dict, wildcard_patterns: list) -> list:
    """production-сторона единого candidate index. fragment_attrs -- атрибуты
    внешнего <td> production-фрагмента (как их уже строит recognize_components()).

    Сначала пробует точный fingerprint фрагмента. Если он даёт хотя бы одного
    кандидата — используется только он (как и раньше делал обычный fingerprint-
    индекс: точное совпадение всей обвязки достаточно специфично само по себе,
    расширять его дополнительными TOLERATED-проекциями не нужно и рискованно —
    см. ниже). Только если точный ключ не даёт НИ ОДНОГО кандидата, пробуется,
    по очереди, КАЖДАЯ проекция, реально встретившаяся хотя бы у одного
    canonical-компонента библиотеки (wildcard_patterns, см.
    build_candidate_index()) — так же, как раньше text-padding-agnostic резолвер
    вызывался ТОЛЬКО когда обычный fingerprint был пуст, а не всегда.

    Это принципиально: TOLERATED-позиция (например, padding у "Текст NNpx")
    — это возможность подтвердить компонент по остальной, строгой части
    обвязки, когда ТОЧНОГО совпадения нет вовсе, а НЕ дополнительный, всегда
    примешиваемый источник кандидатов — иначе для очень широкого класса
    production-фрагментов (общего вида align/bgcolor/border-radius, но с
    padding, никак не связанным с текстовыми блоками) "Текст 14/16px"
    попадали бы в кандидаты без всякой причины, раздувая unresolved вместо
    того, чтобы фрагмент оставался молча непройденным gap'ом, как раньше.

    Это только prefilter — не решает, какой компонент это на самом деле;
    окончательное решение по-прежнему принимает structural_match(...,
    form_model=entry.form_model) в recognize_components().

    Возвращает (candidates, is_exact). is_exact=False (кандидаты найдены
    только через wildcard-проекцию) — сигнал вызывающему коду, что
    ПОДПИСЬ обёртки совпала не по-настоящему, а только "с точностью до
    TOLERATED-отличия" (см. выше): этого достаточно для попытки confirm через
    structural_match(form_model=...), но НЕДОСТАТОЧНО, чтобы включать классы
    1-4 (wrapper_stripped/heading/grid/schedule module-level) — они рассчитаны
    на действительно точное совпадение обвязки, а TOLERATED-проекция может
    случайно совпасть по align/bgcolor/border-radius с СОВЕРШЕННО другой,
    структурно не связанной обвязкой (например, у другого "сгруппированного"
    card-варианта с иным padding) — включение классов 1-4 в этом случае
    рискует найти вложенный фрагмент ДРУГОГО компонента на неверной внешней
    границе (см. диагностику регрессии на реальном letteros-фиксте)."""
    exact_key = _fingerprint_from_td_attrs(fragment_attrs)
    exact_candidates = index.get(exact_key) or []
    if exact_candidates:
        return list(exact_candidates), True

    candidates: list = []
    for pattern in wildcard_patterns:
        for candidate in index.get(_project_fingerprint(exact_key, pattern)) or []:
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates, False


def _build_stripped_fingerprint_index(library: dict) -> dict:
    """Класс 1 (см. extract_wrapper_stripped_inner()): индекс по "подписи"
    ВНУТРЕННЕЙ <tr> (без внешней card-обвязки) -- по этой же подписи
    находится и уже "голый" production-фрагмент."""
    index: dict = {}
    for key, entry in library.items():
        if entry.stripped_fingerprint is None:
            continue
        index.setdefault(entry.stripped_fingerprint, []).append(key)
    return index


# --- поиск кандидатов в произвольном HTML -----------------------------------

def _immediate_td_open_tag(text: str, after_pos: int, max_gap: int = 200):
    m = _TD_OPEN_RE.search(text, after_pos, after_pos + max_gap)
    if m is None:
        return None
    gap = text[after_pos:m.start()]
    if not _ONLY_WHITESPACE_RE.match(gap):
        return None
    return m


def _iter_tr_with_immediate_td(text: str):
    pos = 0
    while True:
        m = _TR_OPEN_RE.search(text, pos)
        if m is None:
            return
        td_m = _immediate_td_open_tag(text, m.end())
        if td_m is not None:
            yield m.start(), td_m
        pos = m.end()


def recognize_components(html_text: str, library: dict | None = None) -> RecognitionResult:
    """Этап 1 Letteros -> UniSender: находит в html_text фрагменты, однозначно
    соответствующие canonical Letteros-компонентам из letteros_components.zip.

    Не извлекает контент, не подставляет в UniSender-компоненты, не заменяет
    расписание, не собирает письмо — только идентификация (см. docstring модуля).

    Возвращает RecognitionResult:
      - matches: подтверждённые компоненты по порядку появления в письме.
        Обычно element — конкретное имя canonical-компонента; но для модулей
        из MODULE_LEVEL_ELIGIBLE_MODULES ("Шапки", "Подвалы") допускается
        module-level результат (element=None) — см. docstring класса
        RecognizedComponent и комментарий у MODULE_LEVEL_ELIGIBLE_MODULES;
      - unresolved: позиции, где подпись обёртки совпала с одним и более
        canonical-компонентом, но структурная проверка не подтвердила совпадение
        ни с одним из них (и module-level fallback тоже не применим — либо
        кандидаты из разных модулей, либо модуль не входит в
        MODULE_LEVEL_ELIGIBLE_MODULES) — компонент не распознан, причины
        зафиксированы.
    """
    if library is None:
        library = load_letteros_library()
    candidate_index, wildcard_patterns = build_candidate_index(library)
    stripped_index = _build_stripped_fingerprint_index(library)
    heading_font_sizes = _build_heading_font_sizes(library)
    schedule_header_signatures = _build_schedule_header_signatures(library)

    matches: list[RecognizedComponent] = []
    unresolved: list[UnresolvedCandidate] = []
    order = 0
    pos = 0
    while pos < len(html_text):
        m = _TR_OPEN_RE.search(html_text, pos)
        if m is None:
            break
        start = m.start()
        td_m = _immediate_td_open_tag(html_text, m.end())
        if td_m is None:
            pos = m.end()
            continue

        fragment_attrs = dict(_parse_attrs(td_m.group(0)[len("<td"):-1]))
        candidate_keys, candidates_are_exact = find_candidates(fragment_attrs, candidate_index, wildcard_patterns)
        if not candidate_keys:
            # Классы 1-3 ниже (stripped-index/заголовки/расписание) сознательно
            # НЕ пытаются сработать здесь: без совпадения по кандидатному
            # индексу (обвязка целиком, с учётом TOLERATED/CONTENT-позиций
            # каждого компонента из его FormModel -- см. build_candidate_index())
            # слишком высок риск найти структурно похожий, но не тот, ВЛОЖЕННЫЙ
            # фрагмент ДРУГОГО canonical-компонента (подтверждено на
            # "Контентные блоки/Место" -- её собственная внутренняя строка-
            # заголовок структурно совпадает с "Заголовок 28px" без обвязки, но
            # это не одно и то же). Во всех реальных STOP-кейсах (кнопка/
            # картинка/событие/заголовки/padding "Текст NNpx", см. диагностику)
            # кандидатный индекс уже был непустым -- это ограничение их не
            # затрагивает.
            pos = m.end()
            continue

        end = generation._scan_balanced_tr_end(html_text, start)
        fragment = html_text[start:end]

        confirmed = []
        all_diffs = {}
        for key in candidate_keys:
            entry = library[key]
            ok, diffs = structural_match(entry.html, fragment, form_model=entry.form_model)
            if ok:
                confirmed.append(key)
            else:
                all_diffs[key] = diffs

        if len(confirmed) == 1:
            module, element = confirmed[0]
            basis = (
                f"подпись обёртки (align/bgcolor/padding/border-radius) совпала с "
                f"{len(candidate_keys)} кандидат(ом/ами) из letteros_components; "
                f"структурная проверка дерева тегов подтвердила ровно один: {module}/{element}"
            )
            matches.append(RecognizedComponent(module, element, order, start, end, fragment, basis))
            order += 1
            pos = end  # компоненты не вкладываются друг в друга — пропускаем всё содержимое
            continue

        # Классы module-level/1/3/4/2 ниже требуют candidates_are_exact: они
        # рассчитаны на ДЕЙСТВИТЕЛЬНО точное совпадение подписи обвязки, а не
        # на "совпадает с точностью до TOLERATED-отличия" (см. find_candidates()).
        # TOLERATED-проекция (сегодня — padding у "Текст NNpx") может случайно
        # совпасть по align/bgcolor/border-radius с СОВЕРШЕННО другой,
        # структурно не связанной обвязкой (например, другим "сгруппированным"
        # card-вариантом с иным padding) — попытка классов 1/3/4/2 в этом
        # случае рискует найти вложенный фрагмент ДРУГОГО компонента на
        # неверной внешней границе (найдено регрессионным тестом на реальном
        # letteros-фикстуре: без этого ограничения class 1 "проваливался"
        # внутрь чужой card-обвязки, случайно похожей по этим трём признакам).
        # Обычный confirm-цикл через structural_match() выше по-прежнему
        # работает для НЕ-exact кандидатов — только эти дополнительные классы
        # его не подхватывают.
        if candidates_are_exact:
            candidate_modules = {m_ for m_, _e in candidate_keys}
            if len(candidate_modules) == 1:
                only_module = next(iter(candidate_modules))
                if only_module in MODULE_LEVEL_ELIGIBLE_MODULES:
                    basis = (
                        f"подпись обёртки совпала с {len(candidate_keys)} кандидат(ом/ами) из "
                        f"letteros_components, все из модуля {only_module}; структурная проверка "
                        f"подтвердила {len(confirmed)} из {len(candidate_keys)} (не ровно один), но "
                        f"эта подпись не пересекается ни с одним другим модулем библиотеки (см. "
                        f"MODULE_LEVEL_ELIGIBLE_MODULES) — распознано как module-level компонент, "
                        f"element осознанно не выбран"
                    )
                    matches.append(RecognizedComponent(only_module, None, order, start, end, fragment, basis))
                    order += 1
                    pos = end
                    continue

            # --- класс 1: production-вариант без внешней card-обвязки ------
            resolved = _resolve_wrapper_stripped_match(fragment, stripped_index, library)
            if resolved is not None:
                module, element, matched_html = resolved
                basis = (
                    f"внешняя card-обвязка/MSO ghost-table у canonical-компонента {module}/{element} "
                    f"в production отсутствует (известный технический паттерн экспорта Letteros); "
                    f"внутренняя <tr> структурно подтверждена без обвязки"
                )
                matches.append(RecognizedComponent(
                    module, element, order, start, end, matched_html, basis, match_mode="wrapper_stripped",
                ))
                order += 1
                pos = end
                continue

            # --- класс 3: заголовки "Текстовые блоки/Заголовок NNpx" -------
            if heading_font_sizes:
                heading_match = _try_heading_text_injection(fragment, heading_font_sizes)
                if heading_match is not None:
                    module, element, _text = heading_match
                    basis = (
                        f"font-size внутреннего <td> однозначно совпал с canonical {module}/{element}; "
                        f"остальное оформление (align/class/line-height/<strong> вместо font-weight) — "
                        f"production-drift, не переносится — переносится только извлечённый текст"
                    )
                    matches.append(RecognizedComponent(
                        module, element, order, start, end, fragment, basis, match_mode="heading_text_injection",
                    ))
                    order += 1
                    pos = end
                    continue

            # --- класс 4: 2-колоночный grid "Контентные блоки/Вариант 2-4" -
            if any(k[0] == GRID_MODULE_NAME for k in candidate_keys):
                grid_items = extract_grid_items(fragment)
                if grid_items is not None:
                    basis = (
                        "2-колоночный grid с ровно двумя item-блоками (<!-- item -->/<!-- item END-->), "
                        "у каждого accent card подтверждён как один из трёх известных canonical-вариантов "
                        "(белый/teal/коралл — GRID_ACCENT_TEMPLATES); element не выбирается — порядок "
                        "accent'ов (лево/право) и число tag-pill в production не фиксированы, в отличие "
                        "от canonical"
                    )
                    matches.append(RecognizedComponent(
                        GRID_MODULE_NAME, None, order, start, end, fragment, basis, match_mode="grid_content_injection",
                    ))
                    order += 1
                    pos = end
                    continue

            # --- класс 2: module-level "Мероприятия" ------------------------
            if schedule_header_signatures:
                sig = _find_schedule_header_signature(fragment)
                if sig is not None and sig in schedule_header_signatures:
                    basis = (
                        "day-шапка расписания (font-size/color первых двух строк) совпала с одним из "
                        "canonical \"Мероприятия/Расписание N\"; конкретный element не выбирается — "
                        "число событий/дней в production переменное, старый блок расписания при миграции "
                        "всё равно отбрасывается целиком (см. letteros_migrate.py)"
                    )
                    matches.append(RecognizedComponent(
                        SCHEDULE_MODULE_NAME, None, order, start, end, fragment, basis,
                    ))
                    order += 1
                    pos = end
                    continue

        if len(confirmed) > 1:
            names = ", ".join(f"{m_}/{e_}" for m_, e_ in confirmed)
            unresolved.append(UnresolvedCandidate(
                start, end, confirmed,
                [f"структурная проверка одновременно подтвердила несколько кандидатов: {names} — неоднозначно, не выбираем сами"],
            ))
        else:
            reasons = []
            for key in candidate_keys:
                m_, e_ = key
                reasons.append(f"{m_}/{e_}: " + "; ".join(all_diffs[key][:3]))
            unresolved.append(UnresolvedCandidate(start, end, candidate_keys, reasons))

        # Кандидат не подтверждён структурно — не считаем это компонентом и не
        # пропускаем его содержимое целиком (могло быть ложное совпадение подписи
        # на вложенном <tr>, реальный компонент может начинаться глубже).
        pos = m.end()

    # Побочный эффект того же "не пропускаем содержимое": внешний <tr>,
    # вложенный внутрь которого позже нашёлся подтверждённый match (см. выше),
    # мог сам раньше попасть в unresolved как более широкий (и поэтому
    # структурно не подтверждённый) кандидат на ту же позицию. Убираем такие
    # unresolved-записи, чей диапазон целиком содержит уже подтверждённый
    # match — иначе один и тот же реальный фрагмент считался бы одновременно
    # и распознанным, и блокирующим миграцию.
    unresolved = [
        u for u in unresolved
        if not any(u.start <= m_.start and m_.end <= u.end for m_ in matches)
    ]

    return RecognitionResult(matches, unresolved)
