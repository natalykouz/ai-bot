"""Letteros -> UniSender, этап 2: миграция ЦЕЛОГО письма.

Собирает вместе уже реализованные и протестированные примитивы:
  letteros_recognition.recognize_components() -- какие фрагменты письма
    соответствуют каким canonical Letteros-компонентам, по порядку;
  letteros_adapt.adapt_component() -- техническая адаптация оболочки одного
    такого фрагмента до формы, пригодной для UniSender, без изменения
    содержимого (доказано экспериментом "121 пара" -- 118/121 SUPPORTED);
  generation._parse_schedule_txt() / generation.build_schedule_blocks() --
    существующая, не изменяемая цепочка сборки блоков расписания;
  generation.build_email_html() -- существующая сборка готового документа
    письма из списка блоков (принимает уже готовый список строк как есть,
    расширять не потребовалось).

Извлечение содержимого/сборка вокруг canonical-обвязки — только там, где
recognize_components() это явно пометила через RecognizedComponent.match_mode
(см. её docstring): для "wrapper_stripped" и "heading_text_injection"
letteros_adapt.adapt_component() собирает полный canonical UniSender-блок
вокруг production-контента (а не берёт production HTML как есть). Для module-
level "Мероприятия" (см. letteros_recognition.SCHEDULE_MODULE_NAME) перенос
содержимого не нужен вовсе — старый блок расписания всегда отбрасывается
целиком, независимо от match_mode, и заменяется новым (см. ниже).

Если письмо нельзя безопасно мигрировать целиком -- функция не возвращает
частичный или "подобранный на глаз" результат, а поднимает MigrationError
с конкретной причиной. Возможные причины остановки:
  - в письме есть фрагмент, который recognize_components() не смог
    однозначно сопоставить с библиотекой (result.unresolved непусто);
  - между двумя распознанными блоками есть нераспознанный участок текста
    (recognize_components() не рассматривает весь документ -- только сами
    блоки; пробел между двумя соседними распознанными блоками, содержащий
    что-то, кроме пробельных символов, -- это фрагмент вне известной
    структуры письма);
  - один из распознанных блоков находится в реестре
    letteros_adapt.KNOWN_ADAPTATION_EXCEPTIONS (REQUIRES_MANUAL_REVIEW);
  - нет ни старого блока расписания, ни блока подвала -- невозможно
    однозначно определить, куда вставлять новое расписание;
  - SCHEDULE.txt не проходит разбор существующей цепочкой.
"""

from pathlib import Path

import generation
import letteros_adapt
import letteros_recognition

DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"
DEFAULT_UNISENDER_ZIP_PATH = Path(__file__).resolve().parent / "unisender_components.zip"

SCHEDULE_MODULE = letteros_recognition.SCHEDULE_MODULE_NAME
FOOTER_MODULE = "Подвалы"


class MigrationError(Exception):
    """Миграцию текущего письма нельзя безопасно завершить автоматически.

    module/element/start/end заполняются, если причина привязана к
    конкретному распознанному блоку или конкретному участку исходного текста
    -- вызывающий код может использовать их для сообщения пользователю, не
    разбирая текст исключения."""

    def __init__(self, reason: str, *, module: str | None = None, element: str | None = None,
                 start: int | None = None, end: int | None = None):
        self.reason = reason
        self.module = module
        self.element = element
        self.start = start
        self.end = end
        super().__init__(reason)


class MigrationResult:
    __slots__ = ("html", "components", "schedule_inserted_at", "schedule_dates")

    def __init__(self, html: str, components: list, schedule_inserted_at: int, schedule_dates: int):
        self.html = html
        # [(module, element), ...] в порядке итоговой сборки, БЕЗ модуля
        # "Мероприятия" -- старые блоки расписания в результат не входят.
        self.components = components
        # индекс в components, перед которым вставлены новые блоки расписания
        self.schedule_inserted_at = schedule_inserted_at
        self.schedule_dates = schedule_dates


def _default_unisender_library() -> generation.ComponentLibrary:
    try:
        return generation.ComponentLibrary(DEFAULT_MANIFEST_PATH, DEFAULT_UNISENDER_ZIP_PATH)
    except generation.GenerationError as exc:
        raise MigrationError(f"каноническая UniSender-библиотека недоступна: {exc}") from exc


def _describe(match: letteros_recognition.RecognizedComponent) -> str:
    """Человекочитаемое имя распознанного блока для сообщений об ошибке --
    учитывает module-level случай (element=None, см.
    letteros_recognition.MODULE_LEVEL_ELIGIBLE_MODULES/SCHEDULE_MODULE_NAME),
    чтобы не печатать буквально "Подвалы/None"."""
    if match.element is None:
        return f"{match.module} (module-level, element не определён)"
    return f"{match.module}/{match.element}"


def _check_no_unresolved(result: letteros_recognition.RecognitionResult) -> None:
    if not result.unresolved:
        return
    u = result.unresolved[0]
    names = ", ".join(f"{m}/{e}" for m, e in u.candidate_names) or "—"
    reason = u.reasons[0] if u.reasons else "—"
    raise MigrationError(
        f"не удалось однозначно распознать участок письма [{u.start}:{u.end}] "
        f"(кандидаты по подписи: {names}; причина: {reason})",
        start=u.start, end=u.end,
    )


def _check_no_gaps(letteros_html: str, matches: list) -> None:
    """recognize_components() не разбирает письмо целиком -- только сами
    распознанные блоки. Пространство МЕЖДУ двумя соседними распознанными
    блоками, если оно не чисто пробельное, -- участок письма вне известной
    структуры (собственный текст/вёрстка, не из canonical-библиотеки).
    Пространство ДО первого и ПОСЛЕ последнего блока не проверяется -- это
    обвязка документа (doctype/head/открывающие теги таблицы и т.п.), которая
    в сборку результата не входит вообще (см. build_email_html())."""
    for a, b in zip(matches, matches[1:]):
        gap = letteros_html[a.end:b.start]
        if gap.strip():
            raise MigrationError(
                f"между распознанными блоками {_describe(a)} и {_describe(b)} "
                f"есть нераспознанный участок письма [{a.end}:{b.start}]",
                start=a.end, end=b.start,
            )


def _split_schedule_and_determine_insertion(matches: list) -> tuple[list, int]:
    """Возвращает (non_schedule_matches, insertion_index): matches без модуля
    "Мероприятия" (в исходном порядке) и индекс в этом списке, куда нужно
    вставить новые блоки расписания.

    Если старых блоков расписания было несколько -- все исключаются, новые
    блоки встают на место первого удалённого (см.
    LEGACY_NEWSLETTER_MIGRATION.md, раздел 4). Если старого расписания не
    было вовсе -- используется согласованное правило "перед подвалом"
    (LEGACY_NEWSLETTER_FLOW.md, раздел 5.4). Если нет ни старого расписания,
    ни подвала -- MigrationError, точка вставки не угадывается."""
    non_schedule = []
    insertion_index = None
    for m in matches:
        if m.module == SCHEDULE_MODULE:
            if insertion_index is None:
                insertion_index = len(non_schedule)
            continue
        non_schedule.append(m)

    if insertion_index is not None:
        return non_schedule, insertion_index

    footer_index = next((i for i, m in enumerate(non_schedule) if m.module == FOOTER_MODULE), None)
    if footer_index is None:
        raise MigrationError(
            "в письме нет ни старого блока расписания, ни блока подвала -- "
            "невозможно однозначно определить точку вставки нового расписания"
        )
    return non_schedule, footer_index


def _adapt_all(non_schedule_matches: list, unisender_library: generation.ComponentLibrary) -> list:
    adapted_html = []
    for m in non_schedule_matches:
        kwargs = {}
        if m.match_mode in ("wrapper_stripped", "heading_text_injection"):
            try:
                kwargs["unisender_html"] = unisender_library.html(m.module, m.element)
            except generation.GenerationError as exc:
                raise MigrationError(
                    f"{_describe(m)}: canonical UniSender-компонент недоступен: {exc}",
                    module=m.module, element=m.element, start=m.start, end=m.end,
                ) from exc
        if m.match_mode == "heading_text_injection":
            text = letteros_recognition.extract_heading_text(m.html)
            if text is None:
                raise MigrationError(
                    f"{_describe(m)}: не удалось повторно извлечь текст заголовка",
                    module=m.module, element=m.element, start=m.start, end=m.end,
                )
            kwargs["extracted_text"] = text
        if m.match_mode == "grid_content_injection":
            kwargs["unisender_library"] = unisender_library

        adapted = letteros_adapt.adapt_component(m.module, m.element, m.html, match_mode=m.match_mode, **kwargs)
        if adapted.status == letteros_adapt.AdaptationStatus.REQUIRES_MANUAL_REVIEW:
            raise MigrationError(
                f"{_describe(m)} требует ручной проверки перед миграцией: {adapted.note}",
                module=m.module, element=m.element, start=m.start, end=m.end,
            )
        adapted_html.append(adapted.html)
    return adapted_html


def _build_schedule_blocks(schedule_txt_path: Path, unisender_library: generation.ComponentLibrary,
                            schedule_element: str | None) -> list:
    try:
        groups = generation._parse_schedule_txt(Path(schedule_txt_path))
    except generation.GenerationError as exc:
        raise MigrationError(f"SCHEDULE.txt: {exc}") from exc
    if not groups:
        raise MigrationError("SCHEDULE.txt не содержит ни одного события")
    try:
        return generation.build_schedule_blocks(unisender_library, groups, schedule_element)
    except generation.GenerationError as exc:
        raise MigrationError(f"расписание: {exc}") from exc


def migrate_letter(
    letteros_html: str,
    schedule_txt_path: Path,
    *,
    letteros_library: dict | None = None,
    unisender_library: generation.ComponentLibrary | None = None,
    schedule_element: str | None = None,
    subject: str | None = None,
) -> MigrationResult:
    """Letteros HTML -> готовый UniSender HTML целиком.

    1) распознать последовательность известных компонентов
       (letteros_recognition.recognize_components());
    2) остановиться, если есть нераспознанный/неоднозначный фрагмент или
       разрыв между распознанными блоками (см. _check_no_unresolved/_check_no_gaps);
    3) исключить старые блоки расписания, определить точку вставки новых
       (_split_schedule_and_determine_insertion);
    4) технически адаптировать каждый оставшийся блок
       (letteros_adapt.adapt_component()); остановиться на первом же
       REQUIRES_MANUAL_REVIEW;
    5) собрать новые блоки расписания существующей неизменной цепочкой
       (SCHEDULE.txt -> generation._parse_schedule_txt() ->
       generation.build_schedule_blocks());
    6) вставить их на определённое в шаге 3 место и собрать документ
       (generation.build_email_html()).

    Поднимает MigrationError, если любой из шагов не может быть выполнен
    безопасно -- никогда не возвращает частичный или подобранный "на глаз"
    результат."""
    letteros_library = letteros_library or letteros_recognition.load_letteros_library()
    unisender_library = unisender_library or _default_unisender_library()

    result = letteros_recognition.recognize_components(letteros_html, letteros_library)
    _check_no_unresolved(result)
    if not result.matches:
        raise MigrationError("в письме не найдено ни одного известного canonical-компонента")
    _check_no_gaps(letteros_html, result.matches)

    non_schedule, insertion_index = _split_schedule_and_determine_insertion(result.matches)
    adapted_blocks = _adapt_all(non_schedule, unisender_library)
    schedule_blocks = _build_schedule_blocks(schedule_txt_path, unisender_library, schedule_element)

    final_blocks = adapted_blocks[:insertion_index] + schedule_blocks + adapted_blocks[insertion_index:]
    html = generation.build_email_html(final_blocks, subject=subject)

    components = [(m.module, m.element) for m in non_schedule]
    return MigrationResult(html, components, insertion_index, len(schedule_blocks))
