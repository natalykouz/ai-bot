"""
Регрессионный тест на state-маршрутизацию flow «Генерация расписания»
(handlers/generation.py: ScheduleGenStates.choosing_filter_mode).

Баг: после успешной загрузки XLSX flow переходит в ScheduleGenStates.choosing_filter_mode
и ждёт нажатия inline-кнопки с режимом отбора. Для этого state был зарегистрирован
только callback_query-хендлер (schedgen:mode:*) и message-хендлеры "Отмена"/"/cancel" —
catch-all для произвольного сообщения отсутствовал. Поэтому любой документ/текст,
присланный вместо нажатия кнопки (например, повторно отправленный XLSX), не матчился
ни одним хендлером generation.router и проваливался в общий обработчик документов
handlers/contract.py:handle_document ("Поддерживаются только .txt, .docx, .pdf"), а
следующий файл обрабатывался уже как договор.

Тест поднимает настоящий aiogram Dispatcher с router'ами generation.router и
contract.router (тот же порядок, что и в main.py), проходит реальный путь:
  mail:schedule (callback) -> XLSX -> choosing_filter_mode -> стрей-документ
и проверяет, что стрей-документ обрабатывается catch-all хендлером самого flow
(schedule_gen_mode_wrong_input), а НЕ contract.handle_document, и что state
остаётся ScheduleGenStates.choosing_filter_mode.

Запуск: python mail_project/test/schedule_gen_routing_test.py
"""

import asyncio
import shutil
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
from services import generation_service as gensvc  # noqa: E402

CHAT_ID = 222
USER_ID = 222


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


def test_stray_document_in_choosing_filter_mode_stays_in_schedule_gen_flow() -> None:
    session = FakeSession()
    bot = Bot(token="123456:TEST-TOKEN", session=session)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(generation.router)
    dp.include_router(contract.router)

    key = StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=USER_ID)
    build_id_holder: List[str] = []

    async def run() -> None:
        # 1. Выбор "Генерация расписания" -> waiting_xlsx.
        await dp.feed_update(bot, _callback_update(1, "mail:schedule"))
        assert await storage.get_state(key) == generation.ScheduleGenStates.waiting_xlsx.state

        # 2. Первый XLSX -> build создаётся, flow переходит в choosing_filter_mode
        #    и ждёт нажатия inline-кнопки с режимом отбора.
        session.content_by_file_id["xlsx-1"] = b"fake xlsx bytes"
        await dp.feed_update(bot, _document_update(2, 2, "xlsx-1", "events.xlsx"))
        data = await storage.get_data(key)
        if "build_id" in data:
            build_id_holder.append(data["build_id"])
        state_after_xlsx = await storage.get_state(key)
        assert state_after_xlsx == generation.ScheduleGenStates.choosing_filter_mode.state, (
            f"после XLSX flow должен перейти к choosing_filter_mode, а получили: "
            f"{state_after_xlsx!r}"
        )

        # 3. Вместо нажатия кнопки пользователь присылает документ (например, тот же
        #    XLSX ещё раз). Раньше это проваливалось в contract.handle_document.
        session.sent_texts.clear()
        session.content_by_file_id["xlsx-2"] = b"fake xlsx bytes again"
        await dp.feed_update(bot, _document_update(3, 3, "xlsx-2", "events.xlsx"))

        state_after_stray = await storage.get_state(key)
        assert state_after_stray == generation.ScheduleGenStates.choosing_filter_mode.state, (
            f"стрей-документ не должен менять state choosing_filter_mode, а получили: "
            f"{state_after_stray!r}"
        )
        assert not any("Поддерживаются только" in t for t in session.sent_texts), (
            "стрей-документ на шаге choosing_filter_mode не должен попадать "
            "в contract.handle_document"
        )
        assert not any("Анализирую договор" in t for t in session.sent_texts), (
            "стрей-документ не должен обрабатываться как договор"
        )
        assert any("кнопкой" in t for t in session.sent_texts), (
            "ожидалась подсказка выбрать режим кнопкой (catch-all "
            "schedule_gen_mode_wrong_input)"
        )

        # 4. Нормальный путь (нажатие кнопки) по-прежнему работает после стрей-документа.
        session.sent_texts.clear()
        await dp.feed_update(bot, _callback_update(4, "schedgen:mode:percent"))
        state_after_mode = await storage.get_state(key)
        assert state_after_mode == generation.ScheduleGenStates.waiting_filter_threshold.state, (
            f"нажатие кнопки режима должно перевести flow в waiting_filter_threshold, "
            f"а получили: {state_after_mode!r}"
        )

        await bot.session.close()

    # Обход auth-gate (AuthMiddleware, общий для generation.router и contract.router) —
    # предмет теста это routing document/callback-хендлеров внутри уже авторизованной
    # сессии, а не логика допуска по паролю.
    try:
        with patch.object(auth_service, "is_allowed", return_value=True):
            asyncio.run(run())
    finally:
        # schedule_gen_receive_xlsx реально создаёт BUILD_ID через build_manager.py
        # (не мокается) — подчищаем каталог сборки, чтобы тест не оставлял мусор
        # в mail_project/builds/.
        for build_id in build_id_holder:
            shutil.rmtree(gensvc.build_dir_for(build_id), ignore_errors=True)
    print(
        "OK: стрей-документ на шаге choosing_filter_mode держит flow «Генерация "
        "расписания» на месте (catch-all), а не проваливается в contract.handle_document; "
        "нажатие кнопки режима по-прежнему переводит flow дальше"
    )


if __name__ == "__main__":
    tests = [
        test_stray_document_in_choosing_filter_mode_stays_in_schedule_gen_flow,
    ]
    for test in tests:
        test()
    print(f"\nВсе {len(tests)} проверок пройдены.")
