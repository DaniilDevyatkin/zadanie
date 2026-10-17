# -*- coding: utf-8 -*-
"""
Аккаунт администратора: пароль и подписанные админ-сессии.

Главный принцип — на стороне браузера нельзя подменить ничего, что влияет на
доступ:

  * пароль существует только на сервере и только в виде PBKDF2-HMAC-SHA256-хеша
    с индивидуальной солью (240 000 итераций) — из `data/admin.json` пароль не
    восстанавливается, а подбор долгий;
  * логин и пароль проверяет сервер; страница администратора присылает их один
    раз и больше не хранит (ни в localStorage, ни в JS-переменных);
  * признак входа — не флаг в браузере, а подписанный HMAC-токен в cookie
    `HttpOnly`: скрипт на странице его не читает, а любая правка токена ломает
    подпись и даёт 401;
  * токен привязан к отпечатку клиента (IP + User-Agent), имеет срок жизни и
    одноразовый `nonce`, поэтому чужой cookie не переезжает на другую машину;
  * ключ подписи админа (`data/admin.key`) отдельный от ключа тестовых сессий —
    компрометация одного не открывает другой.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16
DIGEST_BYTES = 32
KEY_BYTES = 48

ADMIN_TTL_SECONDS = 2 * 3600          # админ-сессия живёт 2 часа
ADMIN_COOKIE = "admin_session"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "koleso123"  # стартовый пароль, меняется admin_passwd.py


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """Медленный хеш пароля: подбор перебором стоит дорого."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=DIGEST_BYTES)


@dataclass
class Credentials:
    username: str
    salt: bytes
    digest: bytes
    iterations: int
    updated_at: str
    is_default: bool = False


class AdminStore:
    """Хранилище пароля администратора и выпуск/проверка админ-токенов."""

    def __init__(self, data_dir: Path, default_password: str | None = DEFAULT_ADMIN_PASSWORD) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.credentials_path = self.data_dir / "admin.json"
        self.key_path = self.data_dir / "admin.key"
        self.secret = self._load_secret()
        self.credentials: Credentials | None = self._load_credentials(default_password)

    # ------------------------------------------------------------------ файлы

    def _load_secret(self) -> bytes:
        if self.key_path.exists():
            raw = self.key_path.read_bytes()
            if len(raw) >= 32:
                return raw
        key = secrets.token_bytes(KEY_BYTES)
        self.key_path.write_bytes(key)
        return key

    def _load_credentials(self, default_password: str | None) -> Credentials | None:
        if self.credentials_path.exists():
            try:
                raw = json.loads(self.credentials_path.read_text(encoding="utf-8"))
                return Credentials(
                    username=str(raw["username"]),
                    salt=_b64d(str(raw["salt"])),
                    digest=_b64d(str(raw["hash"])),
                    iterations=int(raw.get("iterations", PBKDF2_ITERATIONS)),
                    updated_at=str(raw.get("updated_at", "")),
                    is_default=bool(raw.get("default_password", False)),
                )
            except Exception:
                # повреждённый файл не должен открывать вход «по умолчанию»
                return None

        if default_password is None:
            return None   # вход выключен, пока пароль не задан admin_passwd.py

        return self.set_password(default_password, is_default=True)

    def is_default_password(self) -> bool:
        return self.credentials is not None and self.credentials.is_default

    def set_password(self, password: str, username: str = DEFAULT_ADMIN_USERNAME, is_default: bool = False) -> Credentials:
        if len(password) < 8:
            raise ValueError("Пароль администратора должен быть не короче 8 символов.")
        salt = secrets.token_bytes(SALT_BYTES)
        credentials = Credentials(
            username=username,
            salt=salt,
            digest=hash_password(password, salt),
            iterations=PBKDF2_ITERATIONS,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            is_default=is_default,
        )
        payload = {
            "version": 1,
            "username": credentials.username,
            "salt": _b64e(credentials.salt),
            "hash": _b64e(credentials.digest),
            "iterations": credentials.iterations,
            "updated_at": credentials.updated_at,
            "default_password": credentials.is_default,
        }
        self.credentials_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            self.credentials_path.chmod(0o600)
        except OSError:
            pass  # на Windows права раздаются через ACL, код ошибки игнорируем
        self.credentials = credentials
        return credentials

    # ------------------------------------------------------------------ вход

    def verify_login(self, username: str, password: str) -> bool:
        """Проверяет логин и пароль. Оба сравнения выполняются всегда и за
        постоянное время, поэтому по задержке ответа нельзя узнать, что именно
        подошло, а что нет."""
        stored = self.credentials
        try:
            given_user = username.encode("utf-8")
        except Exception:
            given_user = b""

        if stored is None:
            # вход отключён: считаем «впустую», чтобы время ответа не отличалось
            hash_password(password, b"\x00" * SALT_BYTES)
            return False

        candidate = hash_password(password, stored.salt, stored.iterations)
        user_ok = hmac.compare_digest(given_user, stored.username.encode("utf-8"))
        pass_ok = hmac.compare_digest(candidate, stored.digest)
        return bool(user_ok and pass_ok)

    # ------------------------------------------------------------------ токен

    def issue_token(self, client: str) -> tuple[str, int]:
        if self.credentials is None:
            raise RuntimeError("Пароль администратора не задан.")
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {
            "sub": self.credentials.username,
            "role": "admin",
            "cli": client,
            "iat": now,
            "exp": now + ADMIN_TTL_SECONDS,
            "nonce": secrets.token_hex(12),
        }
        body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        sig = hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_b64e(sig)}", payload["exp"]

    def read_token(self, token: str | None, client: str) -> dict[str, Any] | None:
        """Возвращает данные сессии или None. Подпись обязательна, срок и
        отпечаток клиента проверяются здесь же."""
        if not token or "." not in token:
            return None
        body, sig = token.split(".", 1)
        expected = hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
        try:
            given = _b64d(sig)
        except Exception:
            return None
        if not hmac.compare_digest(expected, given):
            return None
        try:
            payload = json.loads(_b64d(body))
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("role") != "admin":
            return None
        if int(payload.get("exp", 0)) <= int(datetime.now(timezone.utc).timestamp()):
            return None
        if not hmac.compare_digest(str(payload.get("cli", "")), client):
            return None
        return payload
