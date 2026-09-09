"""
HTTP-сервер каталога компонентов.

Отдаёт prompts/generation/catalog/catalog.html (копия mail_project/catalog.html,
файл не меняется) по прямой ссылке, которую можно открыть в браузере — без
Telegram-клиента. prompts/ уже примонтирован в проде как Railway Persistent
Volume (см. DEPLOYMENT.md) — второе хранилище не создаётся, используется тот же.

Работает в том же процессе и event loop, что и Telegram-polling (см. main.py) —
отдельный сервис/деплой не нужен.
"""

import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

CATALOG_FILE = Path(__file__).resolve().parent.parent / "prompts" / "generation" / "catalog" / "catalog.html"


async def _serve_catalog(request: web.Request) -> web.Response:
    if not CATALOG_FILE.is_file():
        return web.Response(status=404, text="Каталог компонентов пока не загружен.")
    return web.FileResponse(CATALOG_FILE)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/catalog", _serve_catalog)
    return app


async def start(host: str, port: int) -> None:
    """Поднимает сервер и сразу возвращает управление — не блокирует event loop,
    дальнейшее обслуживание запросов идёт через тот же asyncio loop, что и polling."""
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"Сервер каталога компонентов запущен: http://{host}:{port}/catalog")
