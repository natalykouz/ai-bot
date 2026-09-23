"""
Автономная проверка сервисного слоя flow «Добавить расписание в готовое письмо»
(handlers/generation.py: ScheduleAddStates + services/generation_service.py:
append_schedule_blocks) — без запуска бота, прямыми вызовами функций, как и
остальные ad-hoc проверки в mail_project/test/.

Запуск: python mail_project/test/schedule_add_service_test.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from services import generation_service as gensvc  # noqa: E402
from mail_project import qa  # noqa: E402

FIXTURE_NO_SCHEDULE = ROOT / "mail_project" / "test" / "mgi_editor_template_with_blocks_test.html"
SAMPLE_SCHEDULE_TXT = ROOT / "mail_project" / "builds" / "20260904-001" / "schedule" / "SCHEDULE.txt"


def test_is_full_unisender_template_true():
    html_text = FIXTURE_NO_SCHEDULE.read_text(encoding="utf-8")
    assert gensvc.is_full_unisender_template(html_text) is True
    print("OK: is_full_unisender_template распознаёт фикстуру без расписания как полный шаблон")


def test_schedule_blocks_status_missing():
    html_text = FIXTURE_NO_SCHEDULE.read_text(encoding="utf-8")
    assert gensvc.schedule_blocks_status(html_text) == "missing"
    print("OK: schedule_blocks_status == 'missing' на письме без Schedule-блоков")


def test_schedule_blocks_status_already_present_blocks_add_flow():
    html_text = FIXTURE_NO_SCHEDULE.read_text(encoding="utf-8")
    groups = _run(gensvc.build_schedule_replacement_blocks(SAMPLE_SCHEDULE_TXT.read_bytes()))
    assert groups, "ожидались построенные Schedule-блоки из примера SCHEDULE.txt"
    with_schedule = gensvc.append_schedule_blocks(html_text, groups)

    status = gensvc.schedule_blocks_status(with_schedule)
    assert status == "ok"
    print("OK: после добавления письмо распознаётся как содержащее Schedule (status == 'ok') "
          "-> повторная попытка 'Добавить расписание' на этом же файле должна быть остановлена")


def test_append_schedule_blocks_preserves_order_and_appends_at_end():
    html_text = FIXTURE_NO_SCHEDULE.read_text(encoding="utf-8")
    before_components = qa.find_components(html_text)
    assert [c[1] for c in before_components] == ["Заголовок 34px", "Зелёная кнопка"]

    groups = _run(gensvc.build_schedule_replacement_blocks(SAMPLE_SCHEDULE_TXT.read_bytes()))
    new_html = gensvc.append_schedule_blocks(html_text, groups)

    after_components = qa.find_components(new_html)
    after_modules_elements = [(c[0], c[1]) for c in after_components]

    # исходные два блока остаются первыми и в том же порядке
    assert after_modules_elements[0] == ("Текстовые блоки", "Заголовок 34px")
    assert after_modules_elements[1] == ("Кнопки", "Зелёная кнопка")
    # новые Schedule-блоки идут строго после них, до конца письма
    assert all(module == "Мероприятия" for module, _element in after_modules_elements[2:])
    assert len(after_modules_elements) == 2 + len(groups)

    # остальной HTML (шапка документа, закрывающие теги) не тронут
    assert new_html.startswith("<!DOCTYPE html>")
    assert new_html.rstrip().endswith("</html>")
    print(f"OK: append_schedule_blocks добавил {len(groups)} блок(ов) расписания в конец письма, "
          "порядок существующих блоков не изменён")


def test_build_schedule_replacement_blocks_default_element():
    groups = _run(gensvc.build_schedule_replacement_blocks(SAMPLE_SCHEDULE_TXT.read_bytes()))
    assert isinstance(groups, list) and len(groups) > 0
    for block_html in groups:
        assert 'letteros-module="Мероприятия"' in block_html
        assert 'letteros-element="Расписание' in block_html
    print(f"OK: build_schedule_replacement_blocks(element=None) строит {len(groups)} canonical "
          "Schedule-блок(ов) по умолчанию, без явно заданного element")


def test_empty_schedule_txt_raises():
    try:
        _run(gensvc.build_schedule_replacement_blocks(b"SCHEDULE\n"))
    except gensvc.GenerationServiceError as exc:
        print(f"OK: пустой SCHEDULE.txt отклонён с понятной ошибкой: {exc}")
        return
    raise AssertionError("ожидалась GenerationServiceError на SCHEDULE.txt без событий")


def _run(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    tests = [
        test_is_full_unisender_template_true,
        test_schedule_blocks_status_missing,
        test_append_schedule_blocks_preserves_order_and_appends_at_end,
        test_schedule_blocks_status_already_present_blocks_add_flow,
        test_build_schedule_replacement_blocks_default_element,
        test_empty_schedule_txt_raises,
    ]
    for test in tests:
        test()
    print(f"\nВсе {len(tests)} проверок пройдены.")
