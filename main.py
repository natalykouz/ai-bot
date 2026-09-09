"""
Точка входа — запуск Telegram-бота.
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from handlers import contract, generation
from services import catalog_server

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    # Инициализация бота с HTML-разметкой по умолчанию
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Создание диспетчера с хранилищем состояний в памяти (для FSM)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрация роутеров.
    # generation.router — первым: у contract.router есть catch-all без фильтра состояния
    # (handle_text, F.text & ~F.text.startswith("/")), который иначе перехватит нажатие
    # кнопки "Генерация рассылки" и любой текстовый ввод внутри её FSM раньше, чем до них
    # дойдёт очередь.
    dp.include_router(generation.router)
    dp.include_router(contract.router)

    logger.info("Бот запускается...")

    # Сервер каталога компонентов — тот же процесс/event loop, что и polling ниже.
    # PORT задаёт Railway при включении публичного домена для сервиса.
    await catalog_server.start("0.0.0.0", int(os.getenv("PORT", "8080")))

    # Удаляем накопившиеся обновления и запускаем polling
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
