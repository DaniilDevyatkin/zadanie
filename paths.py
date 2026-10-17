# -*- coding: utf-8 -*-
"""Единственное место, где вычисляется папка с данными.

Локально это обычная папка `data` рядом с кодом. В контейнере её выносят в том:
переменная окружения `APP_DATA_DIR` (в Dockerfile выставлена в `/data`), и тогда
ключи подписи, хеш пароля администратора и база результатов лежат на volume,
а не внутри образа — они переживают пересборку и перезапуск.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def data_dir(base: Path | None = None) -> Path:
    """Папка с изменяемыми данными. Создаётся, если её ещё нет."""
    raw = os.environ.get("APP_DATA_DIR", "").strip()
    path = Path(raw).expanduser() if raw else (base or BASE_DIR) / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path
