"""Fail-closed scope and durable, atomic request limits for voice generation.

Daily limits count attempted requests reserved before contacting the provider;
they are not a prediction of the provider's credit consumption. No message
text, credentials or raw conversation identifiers are stored here.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Sequence


def scope_allowed(
    is_group: bool | None,
    group_id: str | int | None,
    allowed_groups: Sequence[str],
    allow_private: bool,
    *,
    platform: str,
) -> bool:
    """Enforce platform-qualified group scope; bare numeric IDs mean QQ only."""
    if not isinstance(platform, str):
        return False
    platform = platform.strip().lower()
    if (not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", platform)
            or platform in {"unknown", "none", "null", "undefined"}):
        return False
    if not isinstance(allow_private, bool):
        return False
    if is_group is False:
        return allow_private
    if is_group is not True or not isinstance(group_id, (str, int)) or isinstance(group_id, bool):
        return False
    if not isinstance(allowed_groups, (list, tuple)):
        return False
    target = str(group_id).strip()
    if not target or len(target) > 256 or ":" in target or any(ord(c) < 32 for c in target):
        return False
    for item in allowed_groups:
        if not isinstance(item, str):
            continue
        entry = item.strip()
        if ":" in entry:
            entry_platform, entry_id = entry.split(":", 1)
            if entry_platform.strip().lower() == platform and entry_id.strip() == target:
                return True
        elif platform == "qq" and re.fullmatch(r"[0-9]+", entry) and entry == target:
            return True
    return False


def is_admin(platform: str, user_id: str | int | None, admin_users: Sequence[str]) -> bool:
    """Match a trusted sender against a configured platform-qualified allowlist.

    Bare numeric identifiers apply only to QQ, as with group allowlists. An
    empty or malformed platform, user ID or list can never grant an exemption.
    """
    if not isinstance(platform, str):
        return False
    platform = platform.strip().lower()
    if (not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", platform)
            or platform in {"unknown", "none", "null", "undefined"}):
        return False
    if not isinstance(user_id, (str, int)) or isinstance(user_id, bool):
        return False
    target = str(user_id).strip()
    if not target or len(target) > 256 or ":" in target or any(ord(c) < 32 for c in target):
        return False
    if not isinstance(admin_users, (list, tuple)):
        return False
    for item in admin_users:
        if not isinstance(item, str):
            continue
        entry = item.strip()
        if ":" in entry:
            entry_platform, entry_id = entry.split(":", 1)
            if entry_platform.strip().lower() == platform and entry_id.strip() == target:
                return True
        elif platform == "qq" and re.fullmatch(r"[0-9]+", entry) and entry == target:
            return True
    return False


def _valid_nonnegative_int(value: object, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def _prepare_database(db_path: str | Path) -> Path:
    path = Path(db_path)
    if str(db_path).strip() in ("", ".", ":memory:"):
        raise ValueError("A durable file path is required")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Callers must dedicate this parent directory to private guard state.
    os.chmod(path.parent, 0o700)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return path


def _event_hash(session_key: str, request_id: str) -> str:
    return hashlib.sha256((session_key + "\0" + request_id).encode("utf-8")).hexdigest()


def _reserve(
    db_path: str | Path,
    session_key: str,
    cooldown_seconds: float,
    daily_max_requests: int,
    request_id: str | None,
    now: datetime | None,
    bypass_limits: bool,
    random_daily_max_requests: int | None,
) -> tuple[bool, str]:
    if not isinstance(session_key, str) or not session_key.strip():
        return False, "invalid_config"
    if not isinstance(cooldown_seconds, (int, float)) or isinstance(cooldown_seconds, bool):
        return False, "invalid_config"
    if not math.isfinite(cooldown_seconds) or not 0 <= cooldown_seconds <= 31536000:
        return False, "invalid_config"
    if not _valid_nonnegative_int(daily_max_requests, 1_000_000):
        return False, "invalid_config"
    if random_daily_max_requests is not None and not _valid_nonnegative_int(random_daily_max_requests, 1_000_000):
        return False, "invalid_config"
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        return False, "invalid_config"
    if random_daily_max_requests is not None and request_id is None:
        return False, "invalid_config"
    if not isinstance(bypass_limits, bool) or (bypass_limits and request_id is None):
        return False, "invalid_config"
    if now is not None and not isinstance(now, datetime):
        return False, "invalid_config"
    try:
        path = _prepare_database(db_path)
        moment = (now or datetime.now().astimezone()).astimezone()
        timestamp = moment.timestamp()
        day = moment.date().isoformat()
        scope = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        event = _event_hash(session_key, request_id) if request_id is not None else None

        with closing(sqlite3.connect(str(path), timeout=5, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS daily_usage (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS random_daily_usage (day TEXT NOT NULL, scope_hash TEXT NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY(day, scope_hash))")
            db.execute("CREATE TABLE IF NOT EXISTS random_reservations (event_hash TEXT PRIMARY KEY, day TEXT NOT NULL, scope_hash TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS random_settled (event_hash TEXT PRIMARY KEY, day TEXT NOT NULL, scope_hash TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS cooldowns (scope_hash TEXT PRIMARY KEY, last_attempt REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS seen_requests (event_hash TEXT PRIMARY KEY, created REAL NOT NULL)")
            db.execute("DELETE FROM seen_requests WHERE created < ?", (timestamp - 7 * 86400,))
            db.execute("DELETE FROM random_reservations WHERE date(day, '+7 days') < date(?)", (day,))
            db.execute("DELETE FROM random_settled WHERE date(day, '+7 days') < date(?)", (day,))
            if event and db.execute("SELECT 1 FROM seen_requests WHERE event_hash = ?", (event,)).fetchone():
                db.rollback()
                return False, "duplicate"
            if not bypass_limits:
                daily = db.execute("SELECT attempts FROM daily_usage WHERE day = ?", (day,)).fetchone()
                if daily_max_requests > 0 and daily and daily[0] >= daily_max_requests:
                    db.rollback()
                    return False, "daily_limit"
                if random_daily_max_requests is not None and random_daily_max_requests > 0:
                    random_daily = db.execute("SELECT attempts FROM random_daily_usage WHERE day = ? AND scope_hash = ?", (day, scope)).fetchone()
                    if random_daily and random_daily[0] >= random_daily_max_requests:
                        db.rollback()
                        return False, "random_daily_limit"
                last = db.execute("SELECT last_attempt FROM cooldowns WHERE scope_hash = ?", (scope,)).fetchone()
                if last and cooldown_seconds and timestamp - last[0] < cooldown_seconds:
                    db.rollback()
                    return False, "cooldown"
                db.execute("INSERT INTO daily_usage(day, attempts) VALUES (?, 1) "
                           "ON CONFLICT(day) DO UPDATE SET attempts = attempts + 1", (day,))
                if random_daily_max_requests is not None:
                    db.execute("INSERT INTO random_daily_usage(day, scope_hash, attempts) VALUES (?, ?, 1) "
                               "ON CONFLICT(day, scope_hash) DO UPDATE SET attempts = attempts + 1", (day, scope))
                    db.execute("INSERT INTO random_reservations(event_hash, day, scope_hash) VALUES (?, ?, ?)",
                               (event, day, scope))
                db.execute("INSERT INTO cooldowns(scope_hash, last_attempt) VALUES (?, ?) "
                           "ON CONFLICT(scope_hash) DO UPDATE SET last_attempt = excluded.last_attempt", (scope, timestamp))
            if event:
                db.execute("INSERT INTO seen_requests(event_hash, created) VALUES (?, ?)", (event, timestamp))
            db.commit()
        return True, "ok"
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        return False, "storage_error"


async def reserve_usage(
    db_path: str | Path,
    session_key: str,
    cooldown_seconds: float,
    daily_max_requests: int,
    request_id: str | None = None,
    *,
    now: datetime | None = None,
    bypass_limits: bool = False,
    random_daily_max_requests: int | None = None,
) -> tuple[bool, str]:
    """Atomically reserve one attempt, off the event loop.

    Zero daily limit disables the cap, while attempts are still counted;
    zero cooldown disables cooldown.
    Once reserved, an ordinary attempt is never refunded, including on cancellation.
    ``request_id`` prevents reuse for seven days. An administrator exemption
    requires a request ID, does not consume normal quota or advance cooldown,
    and still fails closed if validation or persistent storage fails. Scope
    and administrator identity must be verified by the caller.
    ``random_daily_max_requests`` applies only to random replies: None marks
    an explicit request, 0 records a random attempt without limiting it, and a
    positive value caps random attempts per conversation per local day. The
    caller must finish random reservations after synthesis, including failures.
    A bypassed administrator consumes neither random nor ordinary allowance.
    """
    return await asyncio.to_thread(_reserve, db_path, session_key, cooldown_seconds,
                                   daily_max_requests, request_id, now, bypass_limits,
                                   random_daily_max_requests)


def _finish_random_usage(db_path: str | Path, session_key: str, request_id: str,
                         success: bool) -> tuple[bool, str]:
    if (not isinstance(session_key, str) or not session_key.strip()
            or not isinstance(request_id, str) or not request_id.strip()
            or not isinstance(success, bool)):
        return False, "invalid_config"
    try:
        path = _prepare_database(db_path)
        event = _event_hash(session_key, request_id)
        with closing(sqlite3.connect(str(path), timeout=5, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS random_daily_usage (day TEXT NOT NULL, scope_hash TEXT NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY(day, scope_hash))")
            db.execute("CREATE TABLE IF NOT EXISTS random_reservations (event_hash TEXT PRIMARY KEY, day TEXT NOT NULL, scope_hash TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS random_settled (event_hash TEXT PRIMARY KEY, day TEXT NOT NULL, scope_hash TEXT NOT NULL)")
            reservation = db.execute("SELECT day, scope_hash FROM random_reservations WHERE event_hash = ?", (event,)).fetchone()
            if reservation:
                if not success:
                    db.execute("UPDATE random_daily_usage SET attempts = MAX(attempts - 1, 0) WHERE day = ? AND scope_hash = ?", reservation)
                else:
                    db.execute("INSERT OR IGNORE INTO random_settled(event_hash, day, scope_hash) VALUES (?, ?, ?)", (event, *reservation))
                db.execute("DELETE FROM random_reservations WHERE event_hash = ?", (event,))
            db.commit()
        return True, "ok"
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        return False, "storage_error"


async def finish_random_usage(db_path: str | Path, session_key: str, request_id: str,
                              *, success: bool) -> tuple[bool, str]:
    """Settle a random attempt once; release its per-chat slot on failure.

    Settlement is idempotent, and harmless for admin or unknown requests.
    The ordinary quota and request deduplication remain intact on failure.
    """
    return await asyncio.to_thread(_finish_random_usage, db_path, session_key, request_id, success)


def _refund_random_usage(db_path: str | Path, session_key: str, request_id: str) -> tuple[bool, str]:
    if (not isinstance(session_key, str) or not session_key.strip()
            or not isinstance(request_id, str) or not request_id.strip()):
        return False, "invalid_config"
    try:
        path = _prepare_database(db_path)
        event = _event_hash(session_key, request_id)
        with closing(sqlite3.connect(str(path), timeout=5, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS random_daily_usage (day TEXT NOT NULL, scope_hash TEXT NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY(day, scope_hash))")
            db.execute("CREATE TABLE IF NOT EXISTS random_settled (event_hash TEXT PRIMARY KEY, day TEXT NOT NULL, scope_hash TEXT NOT NULL)")
            settled = db.execute("SELECT day, scope_hash FROM random_settled WHERE event_hash = ?", (event,)).fetchone()
            if settled:
                db.execute("UPDATE random_daily_usage SET attempts = MAX(attempts - 1, 0) WHERE day = ? AND scope_hash = ?", settled)
                db.execute("DELETE FROM random_settled WHERE event_hash = ?", (event,))
            db.commit()
        return True, "ok"
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        return False, "storage_error"


async def refund_random_usage(db_path: str | Path, session_key: str, request_id: str) -> tuple[bool, str]:
    """Release a settled random attempt once, after definite delivery failure.

    Caller must verify the receipt establishes failure before refunding. An
    unknown delivery status does not establish that retry is safe. Unsettled,
    admin, unknown or already-refunded requests are harmless no-ops.
    """
    return await asyncio.to_thread(_refund_random_usage, db_path, session_key, request_id)


def _claim_request(db_path: str | Path, session_key: str, request_id: str,
                   now: datetime | None) -> tuple[bool, str]:
    if (not isinstance(session_key, str) or not session_key.strip()
            or not isinstance(request_id, str) or not request_id.strip()
            or (now is not None and not isinstance(now, datetime))):
        return False, "invalid_config"
    try:
        path = _prepare_database(db_path)
        timestamp = (now or datetime.now().astimezone()).astimezone().timestamp()
        event = _event_hash(session_key, request_id)
        with closing(sqlite3.connect(str(path), timeout=5, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS seen_requests (event_hash TEXT PRIMARY KEY, created REAL NOT NULL)")
            db.execute("DELETE FROM seen_requests WHERE created < ?", (timestamp - 7 * 86400,))
            cursor = db.execute("INSERT OR IGNORE INTO seen_requests(event_hash, created) VALUES (?, ?)",
                                (event, timestamp))
            inserted = cursor.rowcount == 1
            db.commit()
        return (True, "ok") if inserted else (False, "duplicate")
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        return False, "storage_error"


async def claim_request(db_path: str | Path, session_key: str, request_id: str,
                        *, now: datetime | None = None) -> tuple[bool, str]:
    """Claim a natural-language message even if later voice generation is denied.

    This separate claim prevents repeated text fallbacks when an old host
    repeatedly plans the same message. Follow it with ``reserve_usage`` using
    a stable ID in a separate namespace, such as ``voice:<message_id>``. An
    administrator exemption requires that reservation ID. Claims never consume
    the daily voice-attempt quota.
    """
    return await asyncio.to_thread(_claim_request, db_path, session_key, request_id, now)
