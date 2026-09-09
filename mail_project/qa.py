"""QA v1: сверка готового HTML письма с canonical-компонентами и точечное исправление вёрстки.

Правила — QA_RULES.md (единственный источник правил технического QA).
Источники компонентов — только зафиксированные в build source/manifest.json
и source/unisender_components.zip (тот же source set, на котором работал Generation).

Алгоритм: для каждого top-level canonical-компонента письма (распознаётся по
`letteros-element`/`letteros-module`) HTML сравнивается со своим canonical
HTML структурно — по тегам и атрибутам, без учёта содержательного текста и
значений src/href/alt (кроме Header/Подвалы — они целиком системные и должны
совпадать с каноном побайтово). Разрешённое отличие — целиком отсутствующий
`<... letteros-hide="...">`-элемент (штатный механизм опциональности
компонента, используемый Generation). Любое другое структурное или
CSS-отличие точечно восстанавливается из canonical HTML. Если участок
однозначно сопоставить с каноном невозможно, компонент помечается BLOCKED
и остаётся в исходном виде.
"""

import difflib
import json
import re
import sys
from pathlib import Path

import generation

VOID_TAGS = {"img", "br", "hr", "meta", "input", "link", "area", "base", "col", "embed", "source", "track", "wbr"}

TOKEN_RE = re.compile(
    r'(?P<comment><!--.*?-->)'
    r'|(?P<endtag></\s*(?P<endname>[a-zA-Z][\w:-]*)\s*>)'
    r'|(?P<starttag><(?P<startname>[a-zA-Z][\w:-]*)(?P<attrblob>(?:"[^"]*"|\'[^\']*\'|[^<>"\'])*)>)',
    re.DOTALL,
)
ATTR_RE = re.compile(r'([a-zA-Z_:][\w:.-]*)(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>/]+)))?')

COMPONENT_START_RE = re.compile(
    r'<tr\b[^>]*letteros-element="(?P<element>[^"]*)"[^>]*letteros-module="(?P<module>[^"]*)"[^>]*>'
)


class QABlocked(Exception):
    """Компонент нельзя однозначно сопоставить/исправить без догадки."""


class Node:
    __slots__ = ("tag", "attrs", "children", "self_closing", "start", "end")

    def __init__(self, tag, attrs, self_closing=False):
        self.tag = tag
        self.attrs = attrs
        self.children = []
        self.self_closing = self_closing
        self.start = None
        self.end = None


def _parse_attrs(blob: str):
    attrs = []
    for m in ATTR_RE.finditer(blob):
        name = m.group(1)
        value = m.group(2)
        if value is None:
            value = m.group(3)
        if value is None:
            value = m.group(4)
        attrs.append((name, value))
    return attrs


def tokenize(text: str) -> list:
    root_children = []
    stack = [root_children]
    node_stack = []
    pos = 0
    for m in TOKEN_RE.finditer(text):
        if m.start() > pos:
            # Пробельные текстовые узлы между тегами исключаем из дерева: их количество —
            # побочный эффект удаления letteros-hide строк (два пробела вокруг удалённого
            # элемента "склеиваются" в один), а не содержательное отличие для сравнения.
            if text[pos:m.start()].strip():
                tnode = Node("#text", [])
                tnode.start, tnode.end = pos, m.start()
                stack[-1].append(tnode)
        pos = m.end()

        if m.group("comment"):
            cnode = Node("#comment", [])
            cnode.start, cnode.end = m.start(), m.end()
            stack[-1].append(cnode)
        elif m.group("endtag"):
            name = m.group("endname")
            for k in range(len(node_stack) - 1, -1, -1):
                if node_stack[k].tag == name:
                    node_stack[k].end = m.end()
                    del node_stack[k:]
                    del stack[k + 1:]
                    break
        elif m.group("starttag"):
            name = m.group("startname")
            blob = m.group("attrblob")
            self_closing = blob.rstrip().endswith("/")
            if self_closing:
                blob = blob.rstrip()[:-1]
            node = Node(name, _parse_attrs(blob), self_closing=self_closing)
            node.start = m.start()
            stack[-1].append(node)
            if self_closing or name.lower() in VOID_TAGS:
                node.end = m.end()
            else:
                node_stack.append(node)
                stack.append(node.children)
    if pos < len(text) and text[pos:].strip():
        tnode = Node("#text", [])
        tnode.start, tnode.end = pos, len(text)
        stack[-1].append(tnode)
    return root_children


def parse_root(text: str) -> Node:
    children = tokenize(text)
    real = [c for c in children if not (c.tag == "#text" and text[c.start:c.end].strip() == "")]
    if len(real) != 1 or real[0].start != 0 or real[0].end != len(text):
        raise QABlocked("не удалось разобрать фрагмент компонента как единый HTML-элемент")
    return real[0]


def node_signature(node: Node):
    if node.tag in ("#text", "#comment"):
        return (node.tag,)
    # letteros-hide однозначно идентифицирует конкретную опциональную строку компонента
    # (например "дату" vs "заголовок" vs "событие 5") — без её значения одинаковые по
    # набору атрибутов соседние <tr letteros-hide="..."> неразличимы для выравнивания.
    hide_value = next((v for n, v in node.attrs if n == "letteros-hide"), None)
    return (node.tag, tuple(sorted(name for name, _ in node.attrs)), hide_value)


def _is_hide_optional(node: Node) -> bool:
    return node.tag not in ("#text", "#comment") and any(name == "letteros-hide" for name, _ in node.attrs)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _align_children(canon_children, inst_children):
    csig = [node_signature(c) for c in canon_children]
    isig = [node_signature(c) for c in inst_children]
    sm = difflib.SequenceMatcher(a=csig, b=isig, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace" and (i2 - i1) != (j2 - j1):
            yield "blocked", range(i1, i2), range(j1, j2)
        elif op == "replace":
            yield "equal", range(i1, i2), range(j1, j2)
        else:
            yield op, range(i1, i2), range(j1, j2)


def fix_node(cn: Node, ct: str, inn: Node, it: str, findings: list, path: str, module: str):
    """Возвращает (html_фрагмент, был_ли_изменён)."""
    if cn.tag == "#text":
        return it[inn.start:inn.end], False

    if cn.tag == "#comment":
        craw = ct[cn.start:cn.end]
        iraw = it[inn.start:inn.end]
        # CRLF/CR vs LF — разница в переводах строк, а не в содержимом MSO-комментария;
        # сравниваем после нормализации, но при совпадении возвращаем iraw как есть
        # (загруженный HTML не переписывается ради одних переводов строк).
        if _normalize_newlines(iraw) != _normalize_newlines(craw):
            findings.append(f"{path}: комментарий/условная разметка (MSO) отличается от canonical — восстановлена")
            return craw, True
        return iraw, False

    if cn.tag != inn.tag:
        findings.append(f"{path}: тег <{inn.tag}> заменён на <{cn.tag}> — восстановлен из canonical")
        return ct[cn.start:cn.end], True

    variable = set()
    if module not in ("Шапки", "Подвалы"):
        if cn.tag == "img":
            variable = {"src", "alt"}
        elif cn.tag == "a":
            variable = {"href"}

    inst_attr_map = dict(inn.attrs)
    canon_attr_map = dict(cn.attrs)
    attrs_changed = False
    new_attrs = []
    for name, cval in cn.attrs:
        if name in variable:
            ival = inst_attr_map.get(name)
            new_attrs.append((name, ival if ival is not None else cval))
            continue
        ival = inst_attr_map.get(name)
        if ival != cval:
            attrs_changed = True
            findings.append(
                f'{path}: атрибут "{name}" тега <{cn.tag}> изменён без необходимости '
                f'({ival!r} -> {cval!r}) — восстановлено каноническое значение'
            )
        new_attrs.append((name, cval))
    extra = [n for n in inst_attr_map if n not in canon_attr_map and n not in variable]
    if extra:
        attrs_changed = True
        findings.append(f'{path}: у тега <{cn.tag}> найдены посторонние атрибуты {extra} — удалены')

    child_segments = []
    any_child_changed = False
    for kind, c_range, i_range in _align_children(cn.children, inn.children):
        if kind == "equal":
            for ci, ii in zip(c_range, i_range):
                seg, changed = fix_node(cn.children[ci], ct, inn.children[ii], it, findings, f"{path}>{cn.tag}", module)
                child_segments.append(seg)
                any_child_changed = any_child_changed or changed
        elif kind == "delete":
            for ci in c_range:
                node = cn.children[ci]
                if _is_hide_optional(node):
                    continue
                findings.append(f"{path}>{cn.tag}: отсутствует обязательный элемент canonical-компонента — восстановлен")
                child_segments.append(ct[node.start:node.end])
                any_child_changed = True
        elif kind == "insert":
            for _ in i_range:
                findings.append(f"{path}>{cn.tag}: обнаружен посторонний элемент, отсутствующий в canonical — удалён")
                any_child_changed = True
        elif kind == "blocked":
            raise QABlocked(
                f"{path}>{cn.tag}: структура не сопоставляется с canonical однозначно (нельзя исправить без догадки)"
            )

    if not attrs_changed and not extra and not any_child_changed:
        return it[inn.start:inn.end], False

    open_tag = f"<{cn.tag}" + "".join(f' {n}="{v}"' for n, v in new_attrs)
    if cn.self_closing:
        return open_tag + "/>", True
    inner = "".join(child_segments)
    return open_tag + ">" + inner + f"</{cn.tag}>", True


def find_components(text: str) -> list:
    result = []
    pos = 0
    while True:
        m = COMPONENT_START_RE.search(text, pos)
        if m is None:
            break
        start = m.start()
        end = generation._scan_balanced_tr_end(text, start)
        result.append((m.group("module"), m.group("element"), start, end))
        pos = end
    return result


def _check_and_fix(inst_text: str, library: generation.ComponentLibrary):
    """Основной алгоритм QA v1 (см. модульный docstring и QA_RULES.md) — сверка text с
    library покомпонентно и точечное исправление. Не знает про build_dir/BUILD_ID —
    используется и для build (run()), и для произвольного HTML (run_standalone()).
    Возвращает (final_html, fixes, blocked, status)."""
    components = find_components(inst_text)
    if not components:
        raise QABlocked("в HTML не найдено ни одного canonical-компонента (letteros-element/letteros-module) для проверки.")

    output_parts = []
    cursor = 0
    fixes = []
    blocked = []

    for module, element, c_start, c_end in components:
        output_parts.append(inst_text[cursor:c_start])
        cursor = c_end
        inst_fragment = inst_text[c_start:c_end]
        label = f"{module}/{element}"

        try:
            library.entry(module, element)
            canon_html = library.html(module, element)
        except generation.GenerationError as exc:
            blocked.append(f"{label}: {exc}")
            output_parts.append(inst_fragment)
            continue

        try:
            canon_root = parse_root(canon_html)
            inst_root = parse_root(inst_fragment)
            component_findings = []
            fixed_fragment, _ = fix_node(canon_root, canon_html, inst_root, inst_fragment, component_findings, label, module)
        except QABlocked as exc:
            blocked.append(f"{label}: {exc}")
            output_parts.append(inst_fragment)
            continue
        except Exception as exc:  # защитный барьер: не падать, а сообщить о нерешаемом случае
            blocked.append(f"{label}: не удалось сопоставить с canonical HTML ({exc}) — исправление невозможно без догадки")
            output_parts.append(inst_fragment)
            continue

        output_parts.append(fixed_fragment)
        fixes.extend(component_findings)

    output_parts.append(inst_text[cursor:])
    final_html = "".join(output_parts)

    if blocked:
        status = "BLOCKED"
    elif fixes:
        status = "FIXED"
    else:
        status = "PASS"

    return final_html, fixes, blocked, status


def run(build_dir: Path) -> None:
    build_id = build_dir.name
    email_path = build_dir / "generation" / "email.html"
    if not email_path.is_file():
        print(f"Ошибка: не найден {email_path}. Сначала выполните: python build_manager.py generate {build_id} <content_file>")
        sys.exit(1)
    inst_text = email_path.read_text(encoding="utf-8")

    source_dir = build_dir / "source"
    try:
        library = generation.ComponentLibrary(source_dir / "manifest.json", source_dir / "unisender_components.zip")
    except generation.GenerationError as exc:
        print(f"Ошибка: {exc}")
        sys.exit(1)

    try:
        final_html, fixes, blocked, status = _check_and_fix(inst_text, library)
    except QABlocked as exc:
        print(f"Ошибка: {exc}")
        sys.exit(1)

    qa_dir = build_dir / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    final_path = qa_dir / "final.html"
    final_path.write_text(final_html, encoding="utf-8")

    report = {
        "build_id": build_id,
        "source_html": str(email_path),
        "final_html": str(final_path),
        "defects_found": len(fixes) + len(blocked),
        "fixes_applied": len(fixes),
        "fixes": fixes,
        "blocked": blocked,
        "status": status,
    }
    report_path = qa_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"QA завершён: {status}")
    print(f"  Дефектов: {report['defects_found']}, исправлено: {report['fixes_applied']}, заблокировано: {len(blocked)}")
    print(f"  final.html: {final_path}")
    print(f"  report.json: {report_path}")


def run_standalone(html_text: str, manifest_path: Path, zip_path: Path) -> dict:
    """QA v1 для произвольного HTML — без BUILD_ID/build_dir, не вызывает Build Manager.
    Canonical-источники (manifest_path/zip_path) передаются вызывающей стороной —
    Telegram flow «Проверка качества письма» берёт актуальный manifest.json и
    unisender_components.zip проекта напрямую, а не source set конкретной сборки.

    Ничего не пишет на диск (в отличие от run()) — возвращает результат вызывающей
    стороне. Поднимает QABlocked, если проверку провести нельзя (нет компонентов
    в HTML, отсутствует manifest/zip и т.п.)."""
    try:
        library = generation.ComponentLibrary(manifest_path, zip_path)
    except generation.GenerationError as exc:
        raise QABlocked(str(exc))

    final_html, fixes, blocked, status = _check_and_fix(html_text, library)

    return {
        "final_html": final_html,
        "defects_found": len(fixes) + len(blocked),
        "fixes_applied": len(fixes),
        "fixes": fixes,
        "blocked": blocked,
        "status": status,
    }
