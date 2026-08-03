"""
Контроль доступа к боту по паролю.

Состояние (хеш пароля, admin_ids, allowed_users) хранится в data/auth.json —
файл переживает перезапуски бота и правится только через команды в Telegram,
без правки кода или .env.

При первом запуске (если data/auth.json ещё нет) состояние создаётся
из переменных окружения ADMIN_IDS и BOT_PASSWORD.
"""

import hashlib
import json
import os
import secrets

AUTH_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "auth.json")


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def _bootstrap() -> dict:
    """Создаёт начальное состояние из .env при первом запуске."""
    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    admin_ids = [int(x) for x in admin_ids_raw.split(",") if x.strip().isdigit()]

    initial_password = os.getenv("BOT_PASSWORD", "")
    salt = secrets.token_hex(16)

    return {
        "salt": salt,
        "password_hash": _hash_password(initial_password, salt) if initial_password else "",
        "admin_ids": admin_ids,
        "allowed_users": [],
    }


def _load() -> dict:
    if not os.path.exists(AUTH_FILE):
        data = _bootstrap()
        _save(data)
        return data
    with open(AUTH_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_admin(user_id: int) -> bool:
    return user_id in _load()["admin_ids"]


def is_allowed(user_id: int) -> bool:
    data = _load()
    return user_id in data["admin_ids"] or user_id in data["allowed_users"]


def check_password(password: str) -> bool:
    data = _load()
    if not data["password_hash"]:
        return False
    return _hash_password(password, data["salt"]) == data["password_hash"]


def grant_access(user_id: int) -> None:
    data = _load()
    if user_id not in data["allowed_users"]:
        data["allowed_users"].append(user_id)
        _save(data)


def set_password(new_password: str) -> None:
    data = _load()
    salt = secrets.token_hex(16)
    data["salt"] = salt
    data["password_hash"] = _hash_password(new_password, salt)
    _save(data)
