"""
Регрессионный тест на state-маршрутизацию flow «Добавить расписание в готовое письмо»
(handlers/generation.py: ScheduleAddStates.waiting_html).

Баг: после невалидного HTML хендлер делал state.clear(), из-за чего повторно
присланный документ (в т.ч. валидный .html) уже не попадал под фильтр
ScheduleAddStates.waiting_html и проваливался в общий обработчик документов
handlers/contract.py:handle_document ("Поддерживаются только .txt, .docx, .pdf").

Тест поднимает настоящий aiogram Dispatcher с router'ами generation.router и
contract.router (тот же порядок, что и в main.py), гоняет через него реальные
Update-объекты с документами и проверяет, что:
  1. невалидный HTML отклоняется с "этот файл не подходит", state остаётся waiting_html;
  2. второй (валидный) HTML обрабатывается тем же ScheduleAddStates-хендлером
     (переход к waiting_schedule_txt), а НЕ contract.handle_document.

Запуск: python mail_project/test/schedule_add_routing_test.py
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
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.methods import GetFile, TelegramMethod  # noqa: E402
from aiogram.methods.base import TelegramType  # noqa: E402
from aiogram.types import File, Update  # noqa: E402

import handlers.contract as contract  # noqa: E402
import handlers.generation as generation  # noqa: E402
from services import auth_service  # noqa: E402

FIXTURE_VALID = ROOT / "mail_project" / "test" / "mgi_editor_template_with_blocks_test.html"

CHAT_ID = 111
USER_ID = 111


class FakeSession(BaseSession):
    """Перехватывает все Bot API вызовы, ничего не отправляя по сети."""

    def __init__(self) -> None:
        super().__init__()
        self.content_by_file_id: Dict[str, bytes] = {}
        self.sent_texts: List[str] = []

    async def close(self) -> None:  # pragma: no cover - нет ресурсов для закрытия
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
        if method_name in ("SendMessage", "EditMessageText"):
            return _fake_message(text or "")
        if method_name == "SendDocument":
            return _fake_message(text or "")
        if method_name == "AnswerCallbackQuery":
            return True
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


def _document_update(update_id: int, message_id: int, file_id: str, file_name: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
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


def test_invalid_then_valid_html_stays_in_schedule_add_flow() -> None:
    session = FakeSession()
    bot = Bot(token="123456:TEST-TOKEN", session=session)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(generation.router)
    dp.include_router(contract.router)

    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=USER_ID)

    async def run() -> None:
        await storage.set_state(key, generation.ScheduleAddStates.waiting_html)

        # 1. Невалидный HTML (не полный UniSender-шаблон из блоков).
        session.content_by_file_id["bad-html"] = b"<html><body>not a real template</body></html>"
        await dp.feed_update(bot, _document_update(1, 1, "bad-html", "bad.html"))

        state_after_invalid = await storage.get_state(key)
        assert state_after_invalid == generation.ScheduleAddStates.waiting_html.state, (
            f"после невалидного HTML state должен остаться waiting_html, а получили: "
            f"{state_after_invalid!r}"
        )
        assert any("не подходит" in t for t in session.sent_texts), (
            "ожидалось сообщение о том, что файл не подходит"
        )
        assert not any("Поддерживаются только" in t for t in session.sent_texts), (
            "невалидный .html не должен попадать в contract.handle_document"
        )

        # 2. .txt на этом же шаге тоже не должен утекать в другой document-flow.
        session.sent_texts.clear()
        await dp.feed_update(bot, _document_update(2, 2, "some-txt", "notes.txt"))
        state_after_txt = await storage.get_state(key)
        assert state_after_txt == generation.ScheduleAddStates.waiting_html.state
        assert not any("Поддерживаются только" in t for t in session.sent_texts), (
            ".txt на шаге ожидания HTML не должен попадать в contract.handle_document"
        )

        # 3. Второй присланный файл — валидный HTML — должен обработаться этим же flow.
        session.sent_texts.clear()
        session.content_by_file_id["good-html"] = FIXTURE_VALID.read_bytes()
        await dp.feed_update(bot, _document_update(3, 3, "good-html", "good.html"))

        state_after_valid = await storage.get_state(key)
        assert state_after_valid == generation.ScheduleAddStates.waiting_schedule_txt.state, (
            f"после валидного HTML flow должен перейти к waiting_schedule_txt, а получили: "
            f"{state_after_valid!r}"
        )
        assert not any("Поддерживаются только" in t for t in session.sent_texts), (
            "валидный .html не должен попадать в contract.handle_document"
        )
        assert any("SCHEDULE.txt" in t for t in session.sent_texts), (
            "ожидалось приглашение прислать SCHEDULE.txt"
        )

        await bot.session.close()

    # Обход auth-gate (AuthMiddleware, общий для generation.router и contract.router) —
    # предмет теста это routing document-хендлеров внутри уже авторизованной сессии,
    # а не логика допуска по паролю.
    with patch.object(auth_service, "is_allowed", return_value=True):
        asyncio.run(run())
    print(
        "OK: невалидный HTML держит flow «Добавить расписание» в waiting_html, "
        "повторный валидный HTML обрабатывается тем же flow, а не contract.handle_document"
    )


if __name__ == "__main__":
    tests = [
        test_invalid_then_valid_html_stays_in_schedule_add_flow,
    ]
    for test in tests:
        test()
    print(f"\nВсе {len(tests)} проверок пройдены.")
