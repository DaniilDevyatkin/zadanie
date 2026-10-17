# -*- coding: utf-8 -*-
"""
Смена пароля администратора.

Запуск (из папки проекта):

    python admin_passwd.py

Пароль вводится скрытно и не попадает в историю команд. Файл data/admin.json
перезаписывается новым хешем; старые админ-сессии становятся недействительными
только после перезапуска сервера (ключ подписи не меняется).
"""

from __future__ import annotations

import getpass
import sys

from admin_auth import AdminStore, DEFAULT_ADMIN_USERNAME
from paths import data_dir


def main() -> int:
    store = AdminStore(data_dir(), default_password=None)

    username = input(f"Логин администратора [{DEFAULT_ADMIN_USERNAME}]: ").strip() or DEFAULT_ADMIN_USERNAME
    first = getpass.getpass("Новый пароль (не короче 8 символов): ")
    second = getpass.getpass("Повторите пароль: ")

    if first != second:
        print("Пароли не совпадают.", file=sys.stderr)
        return 1
    if len(first) < 8:
        print("Пароль слишком короткий: минимум 8 символов.", file=sys.stderr)
        return 1

    store.set_password(first, username=username)
    print(f"Готово. Логин: {username}. Файл: {store.credentials_path}")
    print("Перезапустите сервер, чтобы изменения вступили в силу.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
