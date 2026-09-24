"""
Регрессионный тест на исправление routing-аудита FSM (handlers/generation.py):
"на каждом шаге, где ожидается конкретное действие/тип сообщения, нештатный ввод
должен оставаться внутри текущего flow и не попадать в общий catch-all
handlers/contract.py (F.document / handle_text)".

Аудит нашёл, что states, ожидающие только нажатия inline-кнопки (без своего
message-хендлера), и states, ожидающие текст без catch-all на документ,
проваливались в contract.router. Исправление — локальные catch-all/cancel
хендлеры в каждом таком state (тот же принцип, что уже применён к
ScheduleAddStates.waiting_html и ScheduleGenStates.choosing_filter_mode).

Тест поднимает настоящий aiogram Dispatcher с router'ами generation.router и
contract.router (тот же порядок, что и в main.py) и для каждого исправленного
state проверяет:
  - стрей text/document не попадает в contract.handle_document/handle_text
    ("Поддерживаются только...", "Анализирую договор...");
  - state не меняется (или корректно очищается — для "Отмена"/"/cancel");
  - пользователь получает локальное сообщение о том, что сейчас ожидается.

Плюс smoke-проверка, что легитимный путь (нажатие нужной кнопки) по-прежнему
работает хотя бы для одного из исправленных states (choosing_header), и что
ранее исправленные ScheduleAddStates.waiting_html / ScheduleGenStates.choosing_filter_mode
не сломаны (полные сценарии — в отдельных файлах schedule_add_routing_test.py /
schedule_gen_routing_test.py, здесь — только "не регрессировало").

Запуск: python mail_project/test/fsm_wrong_input_routing_test.py
"""

import asyncio
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.methods import GetFile, TelegramMethod  # noqa: E402
from aiogram.methods.base import TelegramType  # noqa: E402
from aiogram.types import File, Update  # noqa: E402

import handlers.contract as contract  # noqa: E402
import handlers.generation as generation  # noqa: E402
from services import auth_service  # noqa: E402

CHAT_ID = 333
USER_ID = 333

FORBIDDEN_MARKERS = ("Поддерживаются только", "Анализирую договор", "Выберите режим проверки")


class FakeSession(BaseSession):
    """Перехватывает все Bot API вызовы, ничего не отправляя по сети."""

    def __init__(self) -> None:
        super().__init__()
        self.content_by_file_id: Dict[str, bytes] = {}
        self.sent_texts: List[str] = []

    async def close(self) -> None:  # pragma: no cover
        pass

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: "int | None" = None
    ) -> Any:
        if isinstance(method, GetFile):
            return File(
                file_id=method.file_id,
                file_unique_id="fake-unique",
                file_path=f"documents/{method.file_id}",
            )
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if text:
            self.sent_texts.append(text)
        method_name = type(method).__name__
        if method_name in ("SendMessage", "EditMessageText", "SendDocument"):
            return _fake_message(text or "")
        return True

    async def stream_content(
        self,
        url: str,
        headers: "dict | None" = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        file_id = url.rsplit("/", 1)[-1]
        content = self.content_by_file_id[file_id]
        yield content


def _fake_message(text: str) -> Dict[str, Any]:
    return {
        "message_id": 1,
        "date": 0,
        "chat": {"id": CHAT_ID, "type": "private"},
        "text": text,
    }


def _text_update(update_id: int, text: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "from": {"id": USER_ID, "is_bot": False, "first_name": "T"},
                "text": text,
            },
        }
    )


def _document_update(update_id: int, file_id: str, file_name: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "from": {"id": USER_ID, "is_bot": False, "first_name": "T"},
                "document": {
                    "file_id": file_id,
                    "file_unique_id": f"{file_id}-u",
                    "file_name": file_name,
                },
            },
        }
    )


def _callback_update(update_id: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"cbq{update_id}",
                "from": {"id": USER_ID, "is_bot": False, "first_name": "T"},
                "chat_instance": "ci1",
                "data": data,
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": 999, "is_bot": True, "first_name": "Bot"},
                    "text": "menu",
                },
            },
        }
    )


class Harness:
    """Общая обвязка Dispatcher+FakeSession для всех сценариев теста."""

    def __init__(self) -> None:
        self.session = FakeSession()
        self.bot = Bot(token="123456:TEST-TOKEN", session=self.session)
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.dp.include_router(generation.router)
        self.dp.include_router(contract.router)
        self.key = StorageKey(bot_id=self.bot.id, chat_id=CHAT_ID, user_id=USER_ID)
        self._uid = 0

    def next_id(self) -> int:
        self._uid += 1
        return self._uid

    async def set_state(self, state) -> None:
        await self.storage.set_state(self.key, state)

    async def set_data(self, **data: Any) -> None:
        await self.storage.set_data(self.key, data)

    async def get_state(self):
        return await self.storage.get_state(self.key)

    async def send_text(self, text: str) -> None:
        self.session.sent_texts.clear()
        await self.dp.feed_update(self.bot, _text_update(self.next_id(), text))

    async def send_document(self, file_id: str, file_name: str, content: bytes = b"x") -> None:
        self.session.sent_texts.clear()
        self.session.content_by_file_id[file_id] = content
        await self.dp.feed_update(self.bot, _document_update(self.next_id(), file_id, file_name))

    async def send_callback(self, data: str) -> None:
        self.session.sent_texts.clear()
        await self.dp.feed_update(self.bot, _callback_update(self.next_id(), data))

    def assert_no_leak(self, context: str) -> None:
        for marker in FORBIDDEN_MARKERS:
            assert not any(marker in t for t in self.session.sent_texts), (
                f"[{context}] ответ содержит маркер contract.py ({marker!r}): "
                f"{self.session.sent_texts!r}"
            )

    async def close(self) -> None:
        await self.bot.session.close()


CALLBACK_ONLY_STATES_TEXT_HINT = [
    (generation.ScheduleGenStates.reviewing_schedule, "действие"),
    (generation.ScheduleGenStates.choosing_fish_branch, "филиал"),
    (generation.ScheduleGenStates.choosing_fish_template, "вид расписания"),
    (generation.ScheduleTxtFishStates.choosing_branch, "филиал"),
    (generation.ScheduleTxtFishStates.choosing_template, "вид расписания"),
    (generation.LetterGenStates.choosing_branch, "филиал"),
    (generation.LetterGenStates.choosing_schedule_choice, "Да» или «Нет"),
    (generation.LetterGenStates.choosing_schedule_template, "вариант расписания"),
    (generation.LetterGenStates.choosing_header, "Шапки"),
    (generation.LetterGenStates.choosing_hero, "Баннеры"),
    (generation.LetterGenStates.choosing_footer, "Подвалы"),
    (generation.LetterGenStates.choosing_add_more, "действие"),
]


async def _check_callback_only_states(h: Harness) -> None:
    for state, hint in CALLBACK_ONLY_STATES_TEXT_HINT:
        await h.set_state(state)

        # Стрей-текст не должен уходить в contract.py.
        await h.send_text("случайный текст вместо кнопки")
        h.assert_no_leak(f"{state} / text")
        assert await h.get_state() == state.state, (
            f"{state}: state должен остаться неизменным после стрей-текста"
        )
        assert any(hint in t for t in h.session.sent_texts), (
            f"{state}: ожидалась подсказка с {hint!r} в ответе, получили "
            f"{h.session.sent_texts!r}"
        )

        # Стрей-документ тоже не должен уходить в contract.py.
        await h.send_document("doc1", "random.pdf")
        h.assert_no_leak(f"{state} / document")
        assert await h.get_state() == state.state, (
            f"{state}: state должен остаться неизменным после стрей-документа"
        )

        # "Отмена" должна отменять именно этот flow локально.
        await h.send_text("Отмена")
        h.assert_no_leak(f"{state} / cancel")
        assert await h.get_state() is None, (
            f"{state}: 'Отмена' должна очищать state, получили {await h.get_state()!r}"
        )
    print(f"OK: {len(CALLBACK_ONLY_STATES_TEXT_HINT)} callback-only state(s) — "
          "стрей text/document остаются в своём flow, 'Отмена' работает локально, "
          "ничего не уходит в contract.py")


DOCUMENT_LEAK_TEXT_STATES = [
    generation.LetterGenStates.waiting_component_name,
    generation.LetterGenStates.waiting_single_field_value,
    generation.LetterGenStates.waiting_content_text,
]


async def _check_text_states_document_leak(h: Harness) -> None:
    for state in DOCUMENT_LEAK_TEXT_STATES:
        await h.set_state(state)
        await h.send_document("doc2", "random.docx")
        h.assert_no_leak(f"{state} / document")
        assert await h.get_state() == state.state, (
            f"{state}: state должен остаться неизменным после стрей-документа"
        )
        assert any("Ожидаю" in t for t in h.session.sent_texts), (
            f"{state}: ожидалась локальная подсказка 'Ожидаю...', получили "
            f"{h.session.sent_texts!r}"
        )
    print(f"OK: {len(DOCUMENT_LEAK_TEXT_STATES)} text-waiting state(s) в LetterGenStates — "
          "стрей-документ остаётся в своём flow, не уходит в contract.py")


async def _check_qa_cancel(h: Harness) -> None:
    await h.set_state(generation.QAStates.waiting_html)
    await h.send_text("Отмена")
    h.assert_no_leak("QAStates.waiting_html / Отмена")
    assert await h.get_state() is None, "QAStates.waiting_html: 'Отмена' должна очищать state"
    assert any("Отменено" in t for t in h.session.sent_texts), (
        f"QAStates.waiting_html: ожидалось 'Отменено.', получили {h.session.sent_texts!r}"
    )

    await h.set_state(generation.QAStates.waiting_html)
    await h.send_text("/cancel")
    h.assert_no_leak("QAStates.waiting_html / /cancel")
    assert await h.get_state() is None, "QAStates.waiting_html: /cancel должна очищать state"
    assert any("Отменено" in t for t in h.session.sent_texts)

    # Сохраняем прежнее поведение для реально нештатного ввода (не cancel).
    await h.set_state(generation.QAStates.waiting_html)
    await h.send_text("случайный текст")
    h.assert_no_leak("QAStates.waiting_html / wrong text")
    assert await h.get_state() == generation.QAStates.waiting_html.state
    assert any("Ожидаю HTML-файл" in t for t in h.session.sent_texts)
    print("OK: QAStates.waiting_html — 'Отмена'/'/cancel' теперь реально отменяют flow "
          "(раньше ловились тем же catch-all, что и обычный нештатный ввод)")


async def _check_schedule_swap_invalid_html_stays(h: Harness) -> None:
    await h.set_state(generation.ScheduleSwapStates.waiting_html)
    await h.send_document("bad1", "bad.html", b"<html><body>not a template</body></html>")
    h.assert_no_leak("ScheduleSwapStates.waiting_html / invalid html #1")
    assert await h.get_state() == generation.ScheduleSwapStates.waiting_html.state, (
        f"после невалидного HTML state должен остаться waiting_html, получили "
        f"{await h.get_state()!r}"
    )
    assert any("не подходит" in t for t in h.session.sent_texts)

    # Второй невалидный файл подряд — тоже должен остаться в своём flow.
    await h.send_document("bad2", "bad2.html", b"<html><body>still not a template</body></html>")
    h.assert_no_leak("ScheduleSwapStates.waiting_html / invalid html #2")
    assert await h.get_state() == generation.ScheduleSwapStates.waiting_html.state
    print("OK: ScheduleSwapStates.waiting_html — невалидный HTML больше не сбрасывает state "
          "(по аналогии с ScheduleAddStates.waiting_html), повторная попытка не уходит в contract.py")


async def _check_positive_path_choosing_header_still_works(h: Harness) -> None:
    """Smoke-проверка, что новый catch-all не перехватывает легитимный callback:
    нажатие кнопки компонента Шапки по-прежнему переводит flow в choosing_hero."""
    await h.set_state(generation.LetterGenStates.choosing_header)
    await h.set_data(
        branch="Москва",
        build_id="20260101-999",
        _header_list=["Шапка / Вариант 1", "Шапка / Вариант 2"],
    )
    await h.send_callback("gen:header:0")
    state_after = await h.get_state()
    assert state_after == generation.LetterGenStates.choosing_hero.state, (
        f"нажатие кнопки Шапки должно перевести flow в choosing_hero, получили {state_after!r}"
    )
    print("OK: LetterGenStates.choosing_header — легитимный callback (выбор Шапки) "
          "по-прежнему переводит flow в choosing_hero, catch-all не мешает")


async def _check_previously_fixed_states_not_regressed(h: Harness) -> None:
    """ScheduleAddStates.waiting_html и ScheduleGenStates.choosing_filter_mode были
    исправлены ранее — здесь только быстрая проверка, что они не сломаны этой правкой."""
    await h.set_state(generation.ScheduleAddStates.waiting_html)
    await h.send_document("bad3", "bad.html", b"<html><body>nope</body></html>")
    h.assert_no_leak("ScheduleAddStates.waiting_html regression")
    assert await h.get_state() == generation.ScheduleAddStates.waiting_html.state

    await h.set_state(generation.ScheduleGenStates.choosing_filter_mode)
    await h.send_document("xlsx-stray", "events.xlsx")
    h.assert_no_leak("ScheduleGenStates.choosing_filter_mode regression")
    assert await h.get_state() == generation.ScheduleGenStates.choosing_filter_mode.state
    print("OK: ранее исправленные ScheduleAddStates.waiting_html и "
          "ScheduleGenStates.choosing_filter_mode не регрессировали")


async def _main() -> None:
    h = Harness()
    checks = [
        _check_callback_only_states,
        _check_text_states_document_leak,
        _check_qa_cancel,
        _check_schedule_swap_invalid_html_stays,
        _check_positive_path_choosing_header_still_works,
        _check_previously_fixed_states_not_regressed,
    ]
    try:
        with patch.object(auth_service, "is_allowed", return_value=True):
            for check in checks:
                await check(h)
    finally:
        await h.close()
    print(f"\nВсе {len(checks)} проверок пройдены.")


if __name__ == "__main__":
    asyncio.run(_main())
