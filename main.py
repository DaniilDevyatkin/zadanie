# -*- coding: utf-8 -*-
"""
Кабинет заданий — FastAPI + SQLite.

Идея безопасности прохождения:
  * правильные ответы живут только на сервере (questions.py);
  * варианты ответов перемешиваются для каждой сессии и «зашиваются» в
    подписанный HMAC-токен, поэтому подсмотреть порядок на клиенте нельзя;
  * сервер считает результат сам и отдаёт клиенту только итог (баллы, процент),
    без разбора ошибок — «посмотреть, где ошибся» невозможно даже через API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# В slim-образах python и в Windows таблица типов не знает про шрифты: без этих
# строк .woff2 уходил бы как text/plain. Ставим правильные типы сами.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/ttf", ".ttf")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from admin_auth import ADMIN_COOKIE, ADMIN_TTL_SECONDS, AdminStore
from paths import BASE_DIR, data_dir
from questions import TESTS

# --------------------------------------------------------------------------- #
#  Пути и конфигурация
# --------------------------------------------------------------------------- #

STATIC_DIR = BASE_DIR / "static"
DATA_DIR = data_dir()

DB_PATH = DATA_DIR / "attempts.sqlite3"
KEY_PATH = DATA_DIR / "session.key"

SESSION_TTL_SECONDS = 6 * 3600      # подписанная сессия теста живёт 6 часов
MIN_SECONDS_PER_TEST = 20           # быстрее 40 вопросов за 20 сек не решают
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё \-\.]{1,58}$")
RATE_LIMIT_WINDOW = 600             # 10 минут
RATE_LIMIT_MAX = 15                 # не больше 15 отправок за окно с одного клиента

# Стартовый пароль администратора можно задать переменной окружения ADMIN_PASSWORD —
# это удобно при развёртывании в Docker. Переменная действует только при первом
# запуске, пока файла с хешем ещё нет; дальше пароль живёт в data/admin.json и
# меняется командой `python admin_passwd.py`.
_ADMIN_ENV_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

if _ADMIN_ENV_PASSWORD:
    ADMIN = AdminStore(DATA_DIR, default_password=None)
    if ADMIN.credentials is None:
        if len(_ADMIN_ENV_PASSWORD) < 8:
            raise RuntimeError("ADMIN_PASSWORD короче 8 символов — такую переменную окружения не приму.")
        ADMIN.set_password(_ADMIN_ENV_PASSWORD)
        print("[admin] пароль администратора взят из ADMIN_PASSWORD", flush=True)
else:
    ADMIN = AdminStore(DATA_DIR)

ADMIN_LOCK_WINDOW = 600             # окно подсчёта неудачных входов
ADMIN_LOCK_MAX = 5                  # после 5 неудач вход закрыт на 10 минут
LETTERS = "АБВГДЕЖ"               # буквы вариантов: А, Б, В, Г, ...

app = FastAPI(title="Кабинет заданий 3ИС", docs_url=None, redoc_url=None, openapi_url=None)


# --------------------------------------------------------------------------- #
#  Ключ подписи + подписанные сессии
# --------------------------------------------------------------------------- #

def _load_secret() -> bytes:
    if KEY_PATH.exists():
        raw = KEY_PATH.read_bytes()
        if len(raw) >= 32:
            return raw
    key = secrets.token_bytes(48)
    KEY_PATH.write_bytes(key)
    return key


SECRET = _load_secret()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict[str, Any]) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def _verify(token: str) -> dict[str, Any]:
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Некорректная сессия теста. Обновите страницу.")

    expected = hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest()
    try:
        given = _b64d(sig)
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректная сессия теста. Обновите страницу.")

    if not hmac.compare_digest(expected, given):
        raise HTTPException(status_code=400, detail="Подпись сессии недействительна. Обновите страницу.")

    try:
        payload = json.loads(_b64d(body))
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректная сессия теста. Обновите страницу.")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Некорректная сессия теста.")

    age = time.time() - float(payload.get("issued_at", 0))
    if age > SESSION_TTL_SECONDS:
        raise HTTPException(status_code=410, detail="Сессия теста истекла. Обновите страницу и начните заново.")
    if age < 0:
        raise HTTPException(status_code=400, detail="Некорректная сессия теста.")
    payload["age"] = age
    return payload


# --------------------------------------------------------------------------- #
#  SQLite
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id       TEXT    NOT NULL,
    test_title    TEXT    NOT NULL,
    student       TEXT    NOT NULL,
    student_key   TEXT    NOT NULL,
    score         INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    percent       REAL    NOT NULL,
    passing_percent REAL  NOT NULL,
    passed        INTEGER NOT NULL,
    answered      INTEGER NOT NULL,
    duration_sec  INTEGER NOT NULL,
    client_hash   TEXT    NOT NULL,
    flags         TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_key ON attempts (test_id, student_key);

-- ответы студента: видны только в панели администратора
CREATE TABLE IF NOT EXISTS admin_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    client_hash TEXT    NOT NULL,
    username    TEXT    NOT NULL,
    success     INTEGER NOT NULL,
    note        TEXT    NOT NULL DEFAULT ''
);
"""

# колонки, добавленные позже (для баз, созданных предыдущей версией)
MIGRATIONS = (
    ("attempts", "answers_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("attempts", "attempt_number", "INTEGER NOT NULL DEFAULT 1"),
)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


init_db()


# --------------------------------------------------------------------------- #
#  Схемы запросов
# --------------------------------------------------------------------------- #

class AnswerIn(BaseModel):
    qid: str = Field(min_length=1, max_length=32)
    choice: int = Field(ge=0, le=9)


class AttemptIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    token: str = Field(min_length=16, max_length=8000)
    answers: list[AnswerIn] = Field(default_factory=list, max_length=200)
    duration_sec: int = Field(default=0, ge=0, le=6 * 3600)
    # ловушка для автоматизированных отправок: настоящий студент это поле не заполняет
    website: str = Field(default="", max_length=200)
    # счётчики защиты со страницы: снимки экрана, уходы со страницы и т.п.
    events: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  Вспомогательное
# --------------------------------------------------------------------------- #

def _normalize_name(raw: str) -> str:
    name = " ".join(raw.replace("\u00a0", " ").split())
    return name


def _student_key(name: str) -> str:
    return hashlib.sha256(name.lower().replace("ё", "е").encode("utf-8")).hexdigest()[:32]


def _client_hash(request: Request) -> str:
    ip = request.client.host if request.client else "?"
    ua = request.headers.get("user-agent", "")
    return hashlib.sha256(f"{ip}|{ua}".encode("utf-8")).hexdigest()[:16]


_RATE: dict[str, list[float]] = {}


def _rate_ok(client: str) -> bool:
    now = time.time()
    hits = [t for t in _RATE.get(client, []) if now - t < RATE_LIMIT_WINDOW]
    if len(hits) >= RATE_LIMIT_MAX:
        _RATE[client] = hits
        return False
    hits.append(now)
    _RATE[client] = hits
    return True


# --- защита входа администратора от подбора пароля ------------------------- #

_ADMIN_FAILS: dict[str, list[float]] = {}


def _admin_login_allowed(client: str) -> bool:
    now = time.time()
    fails = [t for t in _ADMIN_FAILS.get(client, []) if now - t < ADMIN_LOCK_WINDOW]
    _ADMIN_FAILS[client] = fails
    return len(fails) < ADMIN_LOCK_MAX


def _admin_note_failure(client: str) -> None:
    now = time.time()
    fails = [t for t in _ADMIN_FAILS.get(client, []) if now - t < ADMIN_LOCK_WINDOW]
    fails.append(now)
    _ADMIN_FAILS[client] = fails


def _admin_clear_failures(client: str) -> None:
    _ADMIN_FAILS.pop(client, None)


def _log_admin(request: Request, username: str, success: bool, note: str = "") -> None:
    try:
        with closing(db()) as conn:
            conn.execute(
                "INSERT INTO admin_log (created_at, client_hash, username, success, note) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    _client_hash(request),
                    username[:64],
                    1 if success else 0,
                    note[:200],
                ),
            )
            conn.commit()
    except sqlite3.Error:
        pass  # журнал не должен ломать вход


def _require_admin(request: Request) -> dict[str, Any]:
    """Пускает дальше только владельца валидного подписанного cookie.

    Ни одного решения о доступе на стороне браузера нет: правку cookie ломает
    подпись, чужой cookie не сходится по отпечатку клиента, истёкший — по сроку."""
    session = ADMIN.read_token(request.cookies.get(ADMIN_COOKIE), _client_hash(request))
    if not session:
        raise HTTPException(status_code=401, detail="Нужен вход администратора.")
    return session


def _letter(index: int) -> str:
    """Буква варианта ответа в исходном порядке: 0 → А, 1 → Б и так далее."""
    return LETTERS[index] if isinstance(index, int) and 0 <= index < len(LETTERS) else "—"


def _question_index(test: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {q["id"]: q for q in test["questions"]}


def _public_test(test: dict[str, Any]) -> dict[str, Any]:
    topics: list[str] = []
    for q in test["questions"]:
        if q["topic"] not in topics:
            topics.append(q["topic"])
    return {
        "id": test["id"],
        "code": test["code"],
        "title": test["title"],
        "subtitle": test["subtitle"],
        "summary": test["summary"],
        "accent": test["accent"],
        "icon": test["icon"],
        "questionCount": len(test["questions"]),
        "passingPercent": test["passing_percent"],
        "topics": topics,
    }


def _get_test(test_id: str) -> dict[str, Any]:
    test = TESTS.get(test_id)
    if not test:
        raise HTTPException(status_code=404, detail="Задание не найдено.")
    return test


def _build_session(test: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Перемешивает варианты ответа для каждой сессии и подписывает порядок."""
    orders: dict[str, list[int]] = {}
    items: list[dict[str, Any]] = []
    rng = secrets.SystemRandom()

    for q in test["questions"]:
        order = list(range(len(q["options"])))
        rng.shuffle(order)
        orders[q["id"]] = order
        items.append({
            "id": q["id"],
            "topic": q["topic"],
            "text": q["text"],
            "options": [q["options"][i] for i in order],
        })

    token = _sign({
        "test_id": test["id"],
        "orders": orders,
        "issued_at": int(time.time()),
        "nonce": secrets.token_hex(8),
    })
    return items, token


# --------------------------------------------------------------------------- #
#  API
# --------------------------------------------------------------------------- #

@app.get("/api/branches")
def api_branches() -> dict[str, Any]:
    return {"branches": [_public_test(t) for t in TESTS.values()]}


@app.get("/api/tests/{test_id}")
def api_test(test_id: str) -> JSONResponse:
    test = _get_test(test_id)
    items, token = _build_session(test)
    public = _public_test(test)
    return JSONResponse(
        {
            "test": public,
            "questions": items,   # только текст и варианты, без правильных ответов
            "token": token,
            "instruction": "Выберите один вариант ответа в каждом вопросе. Разбор ошибок не предусмотрен.",
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/tests/{test_id}/submit")
def api_submit(test_id: str, payload: AttemptIn, request: Request) -> JSONResponse:
    test = _get_test(test_id)
    client = _client_hash(request)

    if payload.website.strip():
        raise HTTPException(status_code=400, detail="Отправка заблокирована: обнаружена автоматизация.")

    if not _rate_ok(client):
        raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуйте позже.")

    name = _normalize_name(payload.name)
    if not NAME_RE.match(name) or len(name) < 2:
        raise HTTPException(status_code=422, detail="Укажите фамилию и имя буквами, например: Иванов Иван.")

    session = _verify(payload.token)
    if session.get("test_id") != test_id:
        raise HTTPException(status_code=400, detail="Сессия не соответствует заданию. Обновите страницу.")

    age = int(session["age"])
    if age + 3 < MIN_SECONDS_PER_TEST and payload.answers:
        raise HTTPException(
            status_code=429,
            detail="Тест пройден нереально быстро — отправка отклонена. Начните заново и отвечайте спокойно.",
        )

    orders: dict[str, list[int]] = session.get("orders") or {}
    given: dict[str, int] = {}
    for a in payload.answers:
        if a.qid in given:
            continue
        given[a.qid] = a.choice

    score = 0
    answered = 0
    detail: list[dict[str, Any]] = []
    for q in test["questions"]:
        order = orders.get(q["id"])
        if not order or len(order) != len(q["options"]):
            continue
        chosen = given.get(q["id"])
        if chosen is None or not (0 <= chosen < len(order)):
            detail.append({"qid": q["id"], "chosen": None, "shown": None, "ok": False})
            continue
        answered += 1
        chosen_index = order[chosen]          # индекс варианта в исходном порядке
        is_right = chosen_index == q["correct"]
        if is_right:
            score += 1
        detail.append({"qid": q["id"], "chosen": chosen_index, "shown": chosen, "ok": is_right})

    total = len(test["questions"])
    percent = round(score * 100 / total, 1) if total else 0.0
    passing = float(test["passing_percent"])
    passed = percent >= passing

    flags: list[str] = []
    if answered < total:
        flags.append(f"без ответа: {total - answered}")
    if payload.duration_sec and age and abs(payload.duration_sec - age) > 120:
        flags.append("расхождение таймера")

    # сигналы со страницы: снимки экрана, уходы из окна теста, DevTools, копирование
    for key, label in (
        ("screenshot", "снимок экрана"),
        ("focus", "уход со страницы"),
        ("devtools", "инструменты разработчика"),
        ("copy", "попытки копирования"),
    ):
        try:
            count = int(payload.events.get(key, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            flags.append(f"{label}: {min(count, 999)}")

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    key = _student_key(name)

    with closing(db()) as conn:
        prev = conn.execute(
            "SELECT COUNT(*) AS c FROM attempts WHERE test_id = ? AND student_key = ?",
            (test_id, key),
        ).fetchone()["c"]
        attempt_number = int(prev) + 1
        cur = conn.execute(
            """
            INSERT INTO attempts (test_id, test_title, student, student_key, score, total,
                                  percent, passing_percent, passed, answered, duration_sec,
                                  client_hash, flags, created_at, answers_json, attempt_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (test_id, test["title"], name, key, score, total, percent, passing,
             1 if passed else 0, answered, int(payload.duration_sec or age),
             client, "; ".join(flags), finished_at,
             json.dumps(detail, ensure_ascii=False), attempt_number),
        )
        conn.commit()
        attempt_id = cur.lastrowid

    return JSONResponse(
        {
            "attemptId": attempt_id,
            "attemptNumber": attempt_number,
            "student": name,
            "testId": test_id,
            "testTitle": test["title"],
            "testCode": test["code"],
            "score": score,
            "total": total,
            "percent": percent,
            "passingPercent": passing,
            "passed": passed,
            "verdict": "ЗАЧЁТ" if passed else "НЕ ЗАЧЁТ",
            "answered": answered,
            "durationSec": int(payload.duration_sec or age),
            "finishedAt": finished_at,
            "reviewAvailable": False,   # разбор ошибок недоступен по условию
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  Панель администратора
# --------------------------------------------------------------------------- #

class AdminLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


def _admin_request_ok(request: Request) -> None:
    """Отсекает «чужие» формы: обычный HTML-формы такой заголовок поставить не
    могут, а вместе с SameSite=Strict это закрывает CSRF."""
    if request.headers.get("x-admin-request") != "1":
        raise HTTPException(status_code=400, detail="Некорректный запрос.")


@app.post("/admin/api/login")
def admin_login(payload: AdminLoginIn, request: Request) -> JSONResponse:
    _admin_request_ok(request)
    client = _client_hash(request)

    if not _admin_login_allowed(client):
        _log_admin(request, payload.username, False, "вход заблокирован: слишком много попыток")
        raise HTTPException(
            status_code=429,
            detail="Слишком много неудачных попыток входа. Повторите через 10 минут.",
        )

    if not ADMIN.verify_login(payload.username, payload.password):
        _admin_note_failure(client)
        _log_admin(request, payload.username, False, "неверный логин или пароль")
        # одна и та же формулировка: не подсказываем, что именно не подошло
        raise HTTPException(status_code=401, detail="Неверный логин или пароль.")

    _admin_clear_failures(client)
    _log_admin(request, payload.username, True, "")
    token, expires_at = ADMIN.issue_token(client)

    response = JSONResponse({"ok": True, "username": ADMIN.credentials.username, "expiresAt": expires_at})
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=ADMIN_TTL_SECONDS,
        httponly=True,                 # скрипт на странице cookie не прочитает
        samesite="strict",             # cookie не уходит на чужие сайты
        secure=request.url.scheme == "https",
        path="/admin",                 # отправляется только на админские адреса
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/admin/api/logout")
def admin_logout(request: Request) -> JSONResponse:
    _admin_request_ok(request)
    _log_admin(request, "—", True, "выход")
    response = JSONResponse({"ok": True})
    response.delete_cookie(ADMIN_COOKIE, path="/admin")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/admin/api/session")
def admin_session(request: Request) -> JSONResponse:
    session = ADMIN.read_token(request.cookies.get(ADMIN_COOKIE), _client_hash(request))
    return JSONResponse(
        {
            "authenticated": bool(session),
            "username": session.get("sub") if session else None,
            "expiresAt": session.get("exp") if session else None,
            "passwordIsDefault": ADMIN.is_default_password(),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin/api/summary")
def admin_summary(request: Request) -> JSONResponse:
    _require_admin(request)
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT test_id, test_title,
                   COUNT(*) AS attempts,
                   COUNT(DISTINCT student_key) AS students,
                   SUM(passed) AS passed,
                   SUM(CASE WHEN flags <> '' THEN 1 ELSE 0 END) AS flagged
            FROM attempts GROUP BY test_id, test_title
            """
        ).fetchall()
        totals = conn.execute(
            """
            SELECT COUNT(*) AS attempts, COUNT(DISTINCT student_key) AS students,
                   COALESCE(SUM(passed), 0) AS passed,
                   SUM(CASE WHEN flags <> '' THEN 1 ELSE 0 END) AS flagged
            FROM attempts
            """
        ).fetchone()

    tests: list[dict[str, Any]] = []
    for test in TESTS.values():
        row = next((r for r in rows if r["test_id"] == test["id"]), None)
        attempts = int(row["attempts"]) if row else 0
        passed = int(row["passed"] or 0) if row else 0
        tests.append({
            "id": test["id"],
            "code": test["code"],
            "title": test["title"],
            "questions": len(test["questions"]),
            "passingPercent": test["passing_percent"],
            "attempts": attempts,
            "students": int(row["students"]) if row else 0,
            "passed": passed,
            "failed": attempts - passed,
            "flagged": int(row["flagged"] or 0) if row else 0,
        })

    return JSONResponse(
        {
            "tests": tests,
            "totals": {
                "attempts": int(totals["attempts"] or 0),
                "students": int(totals["students"] or 0),
                "passed": int(totals["passed"] or 0),
                "flagged": int(totals["flagged"] or 0),
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin/api/attempts")
def admin_attempts(request: Request, test_id: str | None = None, limit: int = 200, offset: int = 0) -> JSONResponse:
    _require_admin(request)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    where = ""
    params: list[Any] = []
    if test_id:
        where = "WHERE test_id = ?"
        params.append(test_id)

    with closing(db()) as conn:
        rows = conn.execute(
            f"""
            SELECT id, test_id, test_title, student, score, total, percent, passing_percent,
                   passed, answered, duration_sec, flags, created_at, attempt_number, client_hash
            FROM attempts {where}
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) AS c FROM attempts {where}", tuple(params)).fetchone()["c"]

    return JSONResponse(
        {
            "total": int(total),
            "attempts": [
                {
                    "id": int(r["id"]),
                    "testId": r["test_id"],
                    "testTitle": r["test_title"],
                    "student": r["student"],
                    "score": int(r["score"]),
                    "total": int(r["total"]),
                    "percent": float(r["percent"]),
                    "passingPercent": float(r["passing_percent"]),
                    "passed": bool(r["passed"]),
                    "verdict": "ЗАЧЁТ" if r["passed"] else "НЕ ЗАЧЁТ",
                    "attemptNumber": int(r["attempt_number"] or 1),
                    "answered": int(r["answered"]),
                    "durationSec": int(r["duration_sec"]),
                    "flags": [f for f in str(r["flags"]).split("; ") if f],
                    "createdAt": r["created_at"],
                    "clientHash": r["client_hash"],
                }
                for r in rows
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin/api/attempts/{attempt_id}")
def admin_attempt(attempt_id: int, request: Request) -> JSONResponse:
    _require_admin(request)
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Попытка не найдена.")

    test = TESTS.get(row["test_id"]) or {}
    index = _question_index(test) if test else {}

    try:
        stored = json.loads(row["answers_json"] or "[]")
    except Exception:
        stored = []

    answers: list[dict[str, Any]] = []
    for number, record in enumerate(stored, start=1):
        question = index.get(str(record.get("qid")))
        chosen = record.get("chosen")
        options = question["options"] if question else []
        correct = question["correct"] if question else None
        answers.append({
            "number": number,
            "qid": record.get("qid"),
            "question": question["text"] if question else "(вопрос удалён из банка)",
            "answered": chosen is not None,
            "chosenLetter": _letter(chosen) if chosen is not None else "—",
            "chosenText": options[chosen] if isinstance(chosen, int) and 0 <= chosen < len(options) else "нет ответа",
            "correctLetter": _letter(correct) if correct is not None else "—",
            "correctText": options[correct] if isinstance(correct, int) and 0 <= correct < len(options) else "—",
            "ok": bool(record.get("ok")),
        })

    return JSONResponse(
        {
            "attempt": {
                "id": int(row["id"]),
                "student": row["student"],
                "testId": row["test_id"],
                "testTitle": row["test_title"],
                "score": int(row["score"]),
                "total": int(row["total"]),
                "percent": float(row["percent"]),
                "passingPercent": float(row["passing_percent"]),
                "passed": bool(row["passed"]),
                "verdict": "ЗАЧЁТ" if row["passed"] else "НЕ ЗАЧЁТ",
                "attemptNumber": int(row["attempt_number"] or 1),
                "answered": int(row["answered"]),
                "durationSec": int(row["duration_sec"]),
                "flags": [f for f in str(row["flags"]).split("; ") if f],
                "createdAt": row["created_at"],
                "clientHash": row["client_hash"],
            },
            "answers": answers,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin/api/key")
def admin_key(request: Request, test_id: str | None = None) -> JSONResponse:
    _require_admin(request)
    tests = [TESTS[test_id]] if test_id and test_id in TESTS else list(TESTS.values())
    payload: list[dict[str, Any]] = []
    for test in tests:
        items = []
        for number, q in enumerate(test["questions"], start=1):
            items.append({
                "number": number,
                "qid": q["id"],
                "question": q["text"],
                "correctLetter": _letter(q["correct"]),
                "correctText": q["options"][q["correct"]],
                "options": [
                    {"letter": _letter(i), "text": text, "correct": i == q["correct"]}
                    for i, text in enumerate(q["options"])
                ],
            })
        payload.append({
            "id": test["id"],
            "code": test["code"],
            "title": test["title"],
            "passingPercent": test["passing_percent"],
            "items": items,
        })
    return JSONResponse({"tests": payload}, headers={"Cache-Control": "no-store"})


@app.get("/admin/api/log")
def admin_log(request: Request) -> JSONResponse:
    _require_admin(request)
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT created_at, username, success, note, client_hash FROM admin_log ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return JSONResponse(
        {
            "events": [
                {
                    "createdAt": r["created_at"],
                    "username": r["username"],
                    "success": bool(r["success"]),
                    "note": r["note"],
                    "clientHash": r["client_hash"],
                }
                for r in rows
            ]
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin/api/export.csv")
def admin_export(request: Request) -> Response:
    """Выгрузка ведомости в CSV. Отдаётся только владельцу админ-сессии."""
    _require_admin(request)
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT student, test_title, score, total, percent, passing_percent, passed,
                   attempt_number, duration_sec, created_at, flags
            FROM attempts ORDER BY test_title, student
            """
        ).fetchall()

    lines = ["Фамилия;Задание;Баллы;Всего;Процент;Порог;Итог;Попытка;Время, c;Завершено;Флаги"]
    for r in rows:
        values = [
            r["student"],
            r["test_title"],
            r["score"],
            r["total"],
            f"{float(r['percent']):.1f}".replace(".", ","),
            f"{float(r['passing_percent']):.0f}",
            "зачёт" if r["passed"] else "не зачёт",
            r["attempt_number"],
            r["duration_sec"],
            r["created_at"],
            r["flags"] or "—",
        ]
        lines.append(";".join(str(v).replace(";", ",").replace("\n", " ") for v in values))

    body = "\ufeff" + "\r\n".join(lines) + "\r\n"   # BOM, чтобы Excel понял UTF-8
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="vedomost.csv"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- #
#  Статика
# --------------------------------------------------------------------------- #

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "admin.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


ADMIN_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "object-src 'none'"
)


@app.middleware("http")
async def harden(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

    path = request.url.path
    if path == "/admin" or path.startswith("/admin/"):
        # собственных скриптов и стилей внутри HTML нет, поэтому политика строгая:
        # ни вставленного чужого кода, ни инлайна на админской странице не будет
        response.headers["Content-Security-Policy"] = ADMIN_CSP
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/static/vendor/") or path.startswith("/static/fonts/"):
        # библиотеки и шрифты не меняются — можно кэшировать надолго
        response.headers["Cache-Control"] = "public, max-age=86400"
    else:
        # страница и код приложения всегда перепроверяются, чтобы правки
        # раздавались студентам сразу, без «жёсткого» обновления
        response.headers["Cache-Control"] = "no-cache"
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
