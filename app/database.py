# -*- coding: utf-8 -*-
"""
SQLite Persistence Layer for Multi-Account Management, Error History, and Activity Logs.
Never exposes encrypted_password or tokens in any public response dictionary.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import lv
from app.config import DATABASE_PATH
from app.crypto_vault import decrypt_secret, encrypt_secret, redact_sensitive

_DB_LOCK = threading.RLock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    uid TEXT UNIQUE NOT NULL,
                    game_account_id TEXT DEFAULT 'Pending',
                    display_name TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    encrypted_password TEXT NOT NULL,
                    region TEXT NOT NULL DEFAULT 'ME',
                    status TEXT NOT NULL DEFAULT 'inactive',
                    status_message TEXT DEFAULT 'Stopped / Ready',
                    level INTEGER DEFAULT 1,
                    initial_level INTEGER DEFAULT 1,
                    exp INTEGER DEFAULT 0,
                    initial_exp INTEGER DEFAULT 0,
                    matches_searched INTEGER DEFAULT 0,
                    matches_completed INTEGER DEFAULT 0,
                    reconnect_count INTEGER DEFAULT 0,
                    total_uptime_seconds INTEGER DEFAULT 0,
                    mode_id INTEGER DEFAULT 1,
                    map_id INTEGER DEFAULT 1,
                    device_profile_json TEXT DEFAULT '{}',
                    notes TEXT DEFAULT '',
                    last_activity TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS error_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_error_history_account ON error_history(account_id, id DESC);

                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT,
                    account_label TEXT,
                    level TEXT NOT NULL DEFAULT 'info',
                    event_type TEXT NOT NULL DEFAULT 'system',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_activity_logs_created ON activity_logs(id DESC);
                """
            )
            # Reset any stale 'online' / 'authenticating' states from previous process run to 'inactive'
            conn.execute(
                """
                UPDATE accounts
                SET status = 'inactive',
                    status_message = 'Idle (Worker stopped)'
                WHERE status IN ('online', 'authenticating', 'searching_match', 'in_match', 'reconnecting');
                """
            )
            conn.commit()
        finally:
            conn.close()


def _sanitize_device_profile(dev: Dict[str, Any]) -> Dict[str, Any]:
    """Return safe device hardware metadata without exposing raw unique identifiers."""
    if not isinstance(dev, dict):
        return {}
    raw_id = str(dev.get("unique_device_id", ""))
    masked_id = f"{raw_id[:13]}...{raw_id[-4:]}" if len(raw_id) > 18 else "Google|****"
    return {
        "brand": dev.get("brand", "Android"),
        "model": dev.get("model", "Generic"),
        "gpu_renderer": dev.get("gpu_renderer", "Adreno"),
        "system_software": dev.get("system_software", "Android OS"),
        "screen_resolution": f"{dev.get('screen_width', 1080)}x{dev.get('screen_height', 2400)}",
        "memory_mb": dev.get("memory", 4096),
        "device_fingerprint_masked": masked_id,
    }


def row_to_public_account(row: sqlite3.Row, error_count: Optional[int] = None) -> Dict[str, Any]:
    """Convert a DB row into a safe dictionary for the API/UI. Never includes passwords or tokens."""
    d = dict(row)
    has_creds = bool(d.pop("encrypted_password", None))
    raw_dev = {}
    try:
        raw_dev = json.loads(d.pop("device_profile_json", "{}") or "{}")
    except Exception:
        raw_dev = {}

    reg = d.get("region", "ME")
    exp_now = int(d.get("exp") or 0)
    exp_init = int(d.get("initial_exp") or 0)
    exp_gained = max(0, exp_now - exp_init) if exp_init > 0 else 0

    result = {
        "id": d["id"],
        "uid": d["uid"],
        "game_account_id": d.get("game_account_id") or "Pending",
        "display_name": d["display_name"],
        "nickname": d.get("nickname") or d["display_name"],
        "region": reg,
        "region_prefix": lv.REGION_PREFIX.get(reg, "031500"),
        "region_lang": lv.REGION_LANG.get(reg, "en"),
        "status": d.get("status", "inactive"),
        "status_message": redact_sensitive(d.get("status_message") or ""),
        "level": int(d.get("level") or 1),
        "initial_level": int(d.get("initial_level") or 1),
        "exp": exp_now,
        "initial_exp": exp_init,
        "exp_gained": exp_gained,
        "matches_searched": int(d.get("matches_searched") or 0),
        "matches_completed": int(d.get("matches_completed") or 0),
        "reconnect_count": int(d.get("reconnect_count") or 0),
        "total_uptime_seconds": int(d.get("total_uptime_seconds") or 0),
        "mode_id": int(d.get("mode_id") or 1),
        "map_id": int(d.get("map_id") or 1),
        "notes": d.get("notes") or "",
        "has_credentials": has_creds,
        "device_profile": _sanitize_device_profile(raw_dev),
        "last_activity": d.get("last_activity"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }
    if error_count is not None:
        result["error_count"] = error_count
    return result


def list_accounts(
    search: Optional[str] = None,
    region: Optional[str] = None,
    status_filter: Optional[str] = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
) -> List[Dict[str, Any]]:
    allowed_sort = {
        "created_at": "a.created_at",
        "updated_at": "a.updated_at",
        "last_activity": "a.last_activity",
        "display_name": "LOWER(a.display_name)",
        "uid": "a.uid",
        "region": "a.region",
        "status": "a.status",
        "level": "a.level",
        "exp": "a.exp",
        "matches_completed": "a.matches_completed",
    }
    order_col = allowed_sort.get(sort_by, "a.created_at")
    order_dir = "ASC" if sort_dir.lower() == "asc" else "DESC"

    where_clauses = []
    params: List[Any] = []

    if search and search.strip():
        q = f"%{search.strip()}%"
        where_clauses.append(
            "(a.display_name LIKE ? OR a.uid LIKE ? OR a.nickname LIKE ? OR a.game_account_id LIKE ? OR a.id LIKE ? OR a.status_message LIKE ?)"
        )
        params.extend([q, q, q, q, q, q])

    if region and region.strip().upper() != "ALL":
        where_clauses.append("a.region = ?")
        params.append(region.strip().upper())

    if status_filter and status_filter.strip().lower() != "all":
        sf = status_filter.strip().lower()
        if sf == "active":
            where_clauses.append("a.status IN ('authenticating', 'online', 'searching_match', 'in_match', 'reconnecting')")
        elif sf == "inactive":
            where_clauses.append("a.status = 'inactive'")
        else:
            where_clauses.append("a.status = ?")
            params.append(sf)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    sql = f"""
        SELECT a.*,
               (SELECT COUNT(*) FROM error_history e WHERE e.account_id = a.id) AS err_cnt
        FROM accounts a
        {where_sql}
        ORDER BY {order_col} {order_dir};
    """
    with _DB_LOCK:
        conn = get_connection()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [row_to_public_account(r, error_count=int(r["err_cnt"])) for r in rows]
        finally:
            conn.close()


def get_account_by_id(account_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM error_history e WHERE e.account_id = a.id) AS err_cnt
                FROM accounts a
                WHERE a.id = ?;
                """,
                (account_id,),
            ).fetchone()
            if not row:
                return None
            acc = row_to_public_account(row, error_count=int(row["err_cnt"]))
            errs = conn.execute(
                "SELECT id, stage, error_message, created_at FROM error_history WHERE account_id = ? ORDER BY id DESC LIMIT 100;",
                (account_id,),
            ).fetchall()
            acc["error_history"] = [dict(e) for e in errs]
            logs = conn.execute(
                "SELECT id, level, event_type, message, created_at FROM activity_logs WHERE account_id = ? ORDER BY id DESC LIMIT 100;",
                (account_id,),
            ).fetchall()
            acc["recent_logs"] = [dict(l) for l in logs]
            return acc
        finally:
            conn.close()


def get_account_credentials_internal(account_id: str) -> Optional[Dict[str, Any]]:
    """Server-internal helper to retrieve decrypted UID/password for worker startup."""
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?;", (account_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["password_plain"] = decrypt_secret(d["encrypted_password"])
            return d
        finally:
            conn.close()


def create_account(
    uid: str,
    password: str,
    display_name: Optional[str] = None,
    region: str = "ME",
    mode_id: int = 1,
    map_id: int = 1,
    notes: str = "",
) -> Dict[str, Any]:
    uid_clean = uid.strip()
    reg_clean = region.strip().upper()
    name_clean = (display_name or "").strip() or f"Account_{uid_clean[-6:]}"
    acc_id = f"acc_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()
    enc_pw = encrypt_secret(password)
    dev_profile = lv.get_device_for_account(uid_clean)

    with _DB_LOCK:
        conn = get_connection()
        try:
            existing = conn.execute("SELECT id FROM accounts WHERE uid = ?;", (uid_clean,)).fetchone()
            if existing:
                raise ValueError(f"An account with UID '{uid_clean}' already exists.")

            conn.execute(
                """
                INSERT INTO accounts (
                    id, uid, game_account_id, display_name, nickname,
                    encrypted_password, region, status, status_message,
                    level, initial_level, exp, initial_exp,
                    matches_searched, matches_completed, reconnect_count,
                    total_uptime_seconds, mode_id, map_id,
                    device_profile_json, notes, last_activity, created_at, updated_at
                ) VALUES (?, ?, 'Pending', ?, '', ?, ?, 'inactive', 'Added — Ready to start',
                          1, 1, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    acc_id, uid_clean, name_clean, enc_pw, reg_clean,
                    mode_id, map_id, json.dumps(dev_profile), notes or "",
                    now, now, now
                ),
            )
            conn.commit()
        finally:
            conn.close()

    add_activity_log(
        account_id=acc_id,
        account_label=f"{name_clean} ({uid_clean})",
        level="info",
        event_type="account_created",
        message=f"Account '{name_clean}' (UID {uid_clean}) registered in region {reg_clean}.",
    )
    return get_account_by_id(acc_id)


def update_account(
    account_id: str,
    display_name: Optional[str] = None,
    password: Optional[str] = None,
    region: Optional[str] = None,
    mode_id: Optional[int] = None,
    map_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    now = utc_now_iso()
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?;", (account_id,)).fetchone()
            if not row:
                return None
            updates = ["updated_at = ?"]
            params: List[Any] = [now]

            if display_name is not None and display_name.strip():
                updates.append("display_name = ?")
                params.append(display_name.strip())
            if password is not None and password.strip():
                updates.append("encrypted_password = ?")
                params.append(encrypt_secret(password.strip()))
            if region is not None and region.strip():
                updates.append("region = ?")
                params.append(region.strip().upper())
            if mode_id is not None:
                updates.append("mode_id = ?")
                params.append(int(mode_id))
            if map_id is not None:
                updates.append("map_id = ?")
                params.append(int(map_id))
            if notes is not None:
                updates.append("notes = ?")
                params.append(notes)

            params.append(account_id)
            conn.execute(f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?;", params)
            conn.commit()
        finally:
            conn.close()

    acc = get_account_by_id(account_id)
    if acc:
        add_activity_log(
            account_id=account_id,
            account_label=f"{acc['display_name']} ({acc['uid']})",
            level="info",
            event_type="account_updated",
            message=f"Updated configuration for account '{acc['display_name']}' (Region: {acc['region']}).",
        )
    return acc


def update_account_runtime_state(
    account_id: str,
    *,
    status: Optional[str] = None,
    status_message: Optional[str] = None,
    game_account_id: Optional[str] = None,
    nickname: Optional[str] = None,
    region: Optional[str] = None,
    level: Optional[int] = None,
    exp: Optional[int] = None,
    inc_matches_searched: int = 0,
    inc_matches_completed: int = 0,
    inc_reconnects: int = 0,
    inc_uptime_seconds: int = 0,
) -> None:
    now = utc_now_iso()
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT initial_level, initial_exp, level, exp FROM accounts WHERE id = ?;",
                (account_id,),
            ).fetchone()
            if not row:
                return

            updates = ["updated_at = ?", "last_activity = ?"]
            params: List[Any] = [now, now]

            if status is not None:
                updates.append("status = ?")
                params.append(status)
            if status_message is not None:
                updates.append("status_message = ?")
                params.append(redact_sensitive(status_message))
            if game_account_id is not None:
                updates.append("game_account_id = ?")
                params.append(str(game_account_id))
            if nickname is not None and nickname:
                updates.append("nickname = ?")
                params.append(str(nickname))
            if region is not None and region in lv.REGION_PREFIX:
                updates.append("region = ?")
                params.append(region)
            if level is not None and level > 0:
                updates.append("level = ?")
                params.append(int(level))
                if int(row["initial_level"] or 1) <= 1 and int(row["level"] or 1) <= 1:
                    updates.append("initial_level = ?")
                    params.append(int(level))
            if exp is not None and exp >= 0:
                updates.append("exp = ?")
                params.append(int(exp))
                if int(row["initial_exp"] or 0) == 0 and int(exp) > 0:
                    updates.append("initial_exp = ?")
                    params.append(int(exp))
            if inc_matches_searched > 0:
                updates.append("matches_searched = matches_searched + ?")
                params.append(int(inc_matches_searched))
            if inc_matches_completed > 0:
                updates.append("matches_completed = matches_completed + ?")
                params.append(int(inc_matches_completed))
            if inc_reconnects > 0:
                updates.append("reconnect_count = reconnect_count + ?")
                params.append(int(inc_reconnects))
            if inc_uptime_seconds > 0:
                updates.append("total_uptime_seconds = total_uptime_seconds + ?")
                params.append(int(inc_uptime_seconds))

            params.append(account_id)
            conn.execute(f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?;", params)
            conn.commit()
        finally:
            conn.close()


def delete_account(account_id: str) -> bool:
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute("SELECT display_name, uid FROM accounts WHERE id = ?;", (account_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM accounts WHERE id = ?;", (account_id,))
            conn.commit()
            add_activity_log(
                account_id=None,
                account_label=f"{row['display_name']} ({row['uid']})",
                level="warning",
                event_type="account_deleted",
                message=f"Removed account '{row['display_name']}' (UID {row['uid']}) from dashboard.",
            )
            return True
        finally:
            conn.close()


def record_account_error(account_id: str, stage: str, error_message: str) -> None:
    now = utc_now_iso()
    clean_msg = redact_sensitive(error_message)
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO error_history (account_id, stage, error_message, created_at) VALUES (?, ?, ?, ?);",
                (account_id, stage, clean_msg, now),
            )
            conn.commit()
        finally:
            conn.close()


def clear_account_errors(account_id: str) -> int:
    with _DB_LOCK:
        conn = get_connection()
        try:
            cur = conn.execute("DELETE FROM error_history WHERE account_id = ?;", (account_id,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def add_activity_log(
    account_id: Optional[str],
    account_label: str,
    level: str,
    event_type: str,
    message: str,
) -> Dict[str, Any]:
    now = utc_now_iso()
    clean_msg = redact_sensitive(message)
    with _DB_LOCK:
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO activity_logs (account_id, account_label, level, event_type, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (account_id, account_label, level, event_type, clean_msg, now),
            )
            # Keep activity_logs table bounded to latest 1000 entries
            conn.execute(
                """
                DELETE FROM activity_logs
                WHERE id NOT IN (SELECT id FROM activity_logs ORDER BY id DESC LIMIT 1000);
                """
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "account_id": account_id,
                "account_label": account_label,
                "level": level,
                "event_type": event_type,
                "message": clean_msg,
                "created_at": now,
            }
        finally:
            conn.close()


def get_recent_activity(limit: int = 40) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, account_id, account_label, level, event_type, message, created_at FROM activity_logs ORDER BY id DESC LIMIT ?;",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
