# -*- coding: utf-8 -*-
"""
FastAPI Application Entrypoint for TEAM 84FF Multi-Account Dashboard.
Provides secure admin authentication, account CRUD, bulk operations,
metadata import/export, real-time WebSocket updates, and wraps lv.py.
"""

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import lv
from app import database as db
from app.auth import (
    check_rate_limit,
    clear_failed_logins,
    create_session_token,
    record_failed_login,
    require_admin,
    update_admin_password,
    verify_admin_credentials,
    verify_websocket_admin,
)
from app.bot_manager import bot_manager
from app.config import DEFAULT_ACCOUNT_REGION, SESSION_EXPIRE_HOURS
from app.schemas import (
    AccountCreateRequest,
    AccountUpdateRequest,
    BulkAccountCreateRequest,
    BulkActionRequest,
    LoginRequest,
    MetadataImportRequest,
    PasswordChangeRequest,
    VALID_REGIONS,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.add_activity_log(
        account_id=None,
        account_label="System",
        level="info",
        event_type="server_startup",
        message="Multi-Account Web Dashboard initialized. Ready to manage accounts.",
    )

    async def periodic_sync():
        # Run initial diagnostics and auto-resume active accounts in background
        try:
            await bot_manager.run_diagnostics(DEFAULT_ACCOUNT_REGION)
        except Exception:
            pass
        try:
            await bot_manager.resume_active_accounts_on_startup()
        except Exception:
            pass
        while True:
            await asyncio.sleep(4.0)
            try:
                await bot_manager.broadcast_state_update()
            except Exception:
                pass

    sync_task = asyncio.create_task(periodic_sync())
    try:
        yield
    finally:
        sync_task.cancel()
        await bot_manager.shutdown_all()


app = FastAPI(
    title="84FF Multi-Account Command Center",
    description="Secure Web Dashboard & Multi-Account Management System for lv.py",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_spa():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "ok", "timestamp": db.utc_now_iso()}


# ---------------------------------------------------------------------------
# Authentication Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/auth/login", tags=["Authentication"])
async def login(payload: LoginRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    if not verify_admin_credentials(payload.username, payload.password):
        record_failed_login(client_ip)
        db.add_activity_log(
            account_id=None,
            account_label="Security",
            level="warning",
            event_type="auth_failed",
            message=f"Failed administrator login attempt from IP {client_ip}.",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrator username or password.",
        )

    clear_failed_logins(client_ip)
    token = create_session_token(payload.username.strip())
    response.set_cookie(
        key="ff_admin_session",
        value=token,
        httponly=True,
        max_age=SESSION_EXPIRE_HOURS * 3600,
        samesite="lax",
    )
    db.add_activity_log(
        account_id=None,
        account_label="Administrator",
        level="success",
        event_type="auth_login",
        message=f"Administrator '{payload.username.strip()}' signed in.",
    )
    return {
        "ok": True,
        "token": token,
        "user": {"username": payload.username.strip(), "role": "admin"},
    }


@app.post("/api/auth/logout", tags=["Authentication"])
async def logout(response: Response):
    response.delete_cookie("ff_admin_session")
    return {"ok": True, "message": "Logged out successfully."}


@app.get("/api/auth/me", tags=["Authentication"])
async def get_current_admin(admin: Dict = Depends(require_admin)):
    return {"ok": True, "user": {"username": admin.get("sub"), "role": admin.get("role", "admin")}}


@app.post("/api/auth/change-password", tags=["Authentication"])
async def change_admin_password(
    payload: PasswordChangeRequest,
    admin: Dict = Depends(require_admin),
):
    if not update_admin_password(payload.current_password, payload.new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current administrator password is incorrect.",
        )
    db.add_activity_log(
        account_id=None,
        account_label="Administrator",
        level="info",
        event_type="password_changed",
        message="Administrator session password updated.",
    )
    return {"ok": True, "message": "Administrator password updated for active server session."}


# ---------------------------------------------------------------------------
# Dashboard Overview & System Diagnostics
# ---------------------------------------------------------------------------

@app.get("/api/dashboard/overview", tags=["Dashboard"])
async def get_overview(admin: Dict = Depends(require_admin)):
    return bot_manager.get_dashboard_overview()


@app.post("/api/system/diagnostics", tags=["System"])
async def run_system_diagnostics(
    region: str = Query(default="ME"),
    admin: Dict = Depends(require_admin),
):
    reg = region.strip().upper()
    if reg not in VALID_REGIONS:
        reg = "ME"
    diag = await bot_manager.run_diagnostics(region=reg)
    db.add_activity_log(
        account_id=None,
        account_label="Diagnostics",
        level="info",
        event_type="diagnostics_check",
        message=(
            f"Diagnostics completed ({reg}): DNS {diag['dns_resolved_ip']} ({diag['dns_latency_ms']}ms), "
            f"PlayStore v{diag['playstore_version']}, Release {diag['release_version']}."
        ),
    )
    await bot_manager.broadcast_state_update()
    return {"ok": True, "diagnostics": diag}


@app.get("/api/regions", tags=["System"])
async def get_supported_regions(admin: Dict = Depends(require_admin)):
    regions = []
    for code in sorted(VALID_REGIONS):
        regions.append({
            "code": code,
            "prefix": lv.REGION_PREFIX.get(code, "031500"),
            "lang": lv.REGION_LANG.get(code, "en"),
            "server_url": lv.REGION_SERVERS.get(code, "https://clientbp.ppmainecoonghj.com/"),
        })
    return {"regions": regions}


# ---------------------------------------------------------------------------
# Account Management Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/accounts", tags=["Accounts"])
async def list_accounts_endpoint(
    search: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    sort_by: str = Query(default="created_at"),
    sort_dir: str = Query(default="desc"),
    admin: Dict = Depends(require_admin),
):
    accounts = db.list_accounts(
        search=search,
        region=region,
        status_filter=status_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    return {"accounts": accounts, "total": len(accounts)}


@app.post("/api/accounts", status_code=status.HTTP_201_CREATED, tags=["Accounts"])
async def create_account_endpoint(
    payload: AccountCreateRequest,
    admin: Dict = Depends(require_admin),
):
    try:
        acc = db.create_account(
            uid=payload.uid,
            password=payload.password,
            display_name=payload.display_name,
            region=payload.region,
            mode_id=payload.mode_id,
            map_id=payload.map_id,
            notes=payload.notes or "",
        )
        await bot_manager.broadcast_state_update()
        return {"ok": True, "account": acc}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.post("/api/accounts/bulk-add", tags=["Accounts"])
async def bulk_add_accounts_endpoint(
    payload: BulkAccountCreateRequest,
    admin: Dict = Depends(require_admin),
):
    lines = [ln.strip() for ln in payload.raw_lines.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise HTTPException(status_code=400, detail="No valid account lines found.")

    created = []
    errors = []
    default_reg = payload.default_region.strip().upper()

    for idx, line in enumerate(lines, start=1):
        try:
            # Support UID:PASSWORD or UID,PASSWORD,REGION,DISPLAY_NAME
            if ":" in line and "," not in line:
                parts = [p.strip() for p in line.split(":", 3)]
            else:
                parts = [p.strip() for p in line.replace(":", ",").split(",", 3)]

            if len(parts) < 2 or not parts[0] or not parts[1]:
                errors.append({"line": idx, "error": "Expected format UID:PASSWORD or UID,PASSWORD[,REGION,NAME]"})
                continue

            uid = parts[0]
            pw = parts[1]
            reg = parts[2].upper() if len(parts) >= 3 and parts[2] else default_reg
            if reg not in VALID_REGIONS:
                reg = default_reg
            name = parts[3] if len(parts) >= 4 and parts[3] else f"Player_{uid[-6:]}"

            req = AccountCreateRequest(uid=uid, password=pw, display_name=name, region=reg)
            acc = db.create_account(
                uid=req.uid,
                password=req.password,
                display_name=req.display_name,
                region=req.region,
            )
            created.append(acc)
        except Exception as e:
            errors.append({"line": idx, "error": str(e)})

    await bot_manager.broadcast_state_update()
    return {
        "ok": True,
        "created_count": len(created),
        "error_count": len(errors),
        "created": created,
        "errors": errors,
    }


@app.get("/api/accounts/export/metadata", tags=["Accounts"])
async def export_accounts_metadata(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    ids: Optional[str] = Query(default=None, description="Comma-separated account IDs"),
    admin: Dict = Depends(require_admin),
):
    """
    Exports account metadata ONLY. Passwords, tokens, and secrets are NEVER exported.
    """
    accounts = db.list_accounts(sort_by="created_at", sort_dir="asc")
    if ids:
        id_set = {i.strip() for i in ids.split(",") if i.strip()}
        accounts = [a for a in accounts if a["id"] in id_set]

    export_fields = [
        "id", "uid", "game_account_id", "display_name", "nickname",
        "region", "status", "status_message", "level", "exp", "exp_gained",
        "matches_searched", "matches_completed", "reconnect_count",
        "total_uptime_seconds", "mode_id", "map_id", "notes",
        "last_activity", "created_at",
    ]
    sanitized_rows = [{k: a.get(k) for k in export_fields} for a in accounts]

    db.add_activity_log(
        account_id=None,
        account_label="Export",
        level="info",
        event_type="metadata_exported",
        message=f"Exported metadata for {len(sanitized_rows)} account(s) as {format.upper()}.",
    )

    if format == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=export_fields)
        writer.writeheader()
        writer.writerows(sanitized_rows)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=accounts_metadata.csv"},
        )

    return JSONResponse(
        content={
            "exported_at": db.utc_now_iso(),
            "security_notice": "Metadata export only. Passwords and access tokens are never included.",
            "count": len(sanitized_rows),
            "accounts": sanitized_rows,
        },
        headers={"Content-Disposition": "attachment; filename=accounts_metadata.json"},
    )


@app.post("/api/accounts/import/metadata", tags=["Accounts"])
async def import_accounts_metadata(
    payload: MetadataImportRequest,
    admin: Dict = Depends(require_admin),
):
    """
    Imports accounts or updates existing account metadata from a JSON list.
    If an imported entry is new and does not contain a password, a placeholder
    encrypted state is set and flagged so the admin can set its password before starting.
    """
    imported_count = 0
    updated_count = 0
    errors = []

    default_reg = payload.default_region.strip().upper()
    if default_reg not in VALID_REGIONS:
        default_reg = "ME"

    existing_by_uid = {a["uid"]: a["id"] for a in db.list_accounts()}

    for idx, item in enumerate(payload.accounts, start=1):
        try:
            uid = str(item.get("uid") or "").strip()
            if not uid:
                errors.append({"index": idx, "error": "Missing 'uid' field."})
                continue

            reg = str(item.get("region") or default_reg).strip().upper()
            if reg not in VALID_REGIONS:
                reg = default_reg

            display_name = str(item.get("display_name") or item.get("nickname") or f"Player_{uid[-6:]}").strip()
            notes = str(item.get("notes") or "").strip()
            mode_id = int(item.get("mode_id") or 1)
            map_id = int(item.get("map_id") or 1)
            pw = str(item.get("password") or "").strip()

            if uid in existing_by_uid:
                acc_id = existing_by_uid[uid]
                db.update_account(
                    account_id=acc_id,
                    display_name=display_name,
                    password=pw if pw else None,
                    region=reg,
                    mode_id=mode_id,
                    map_id=map_id,
                    notes=notes,
                )
                updated_count += 1
            else:
                acc = db.create_account(
                    uid=uid,
                    password=pw,
                    display_name=display_name,
                    region=reg,
                    mode_id=mode_id,
                    map_id=map_id,
                    notes=notes,
                )
                if not pw:
                    db.update_account_runtime_state(
                        acc["id"],
                        status="inactive",
                        status_message="Imported metadata — set password before starting worker",
                    )
                existing_by_uid[uid] = acc["id"]
                imported_count += 1
        except Exception as e:
            errors.append({"index": idx, "error": str(e)})

    await bot_manager.broadcast_state_update()
    return {
        "ok": True,
        "imported_count": imported_count,
        "updated_count": updated_count,
        "error_count": len(errors),
        "errors": errors,
    }


@app.post("/api/accounts/bulk-action", tags=["Accounts"])
async def bulk_account_action(
    payload: BulkActionRequest,
    admin: Dict = Depends(require_admin),
):
    results = []
    errors = []

    for acc_id in payload.account_ids:
        try:
            if payload.action == "start":
                res = await bot_manager.start_account(acc_id)
                results.append({"id": acc_id, "status": "started", "detail": res.get("message")})
            elif payload.action == "stop":
                res = await bot_manager.stop_account(acc_id, reason="Stopped via bulk action")
                results.append({"id": acc_id, "status": "stopped", "detail": res.get("message")})
            elif payload.action == "restart":
                res = await bot_manager.restart_account(acc_id)
                results.append({"id": acc_id, "status": "restarted", "detail": res.get("message")})
            elif payload.action == "delete":
                await bot_manager.stop_account(acc_id, reason="Deleting account")
                deleted = db.delete_account(acc_id)
                if deleted:
                    results.append({"id": acc_id, "status": "deleted"})
                else:
                    errors.append({"id": acc_id, "error": "Account not found"})
            elif payload.action == "change_region":
                if not payload.region:
                    raise ValueError("Target region is required for change_region action.")
                updated = db.update_account(acc_id, region=payload.region)
                if updated:
                    results.append({"id": acc_id, "status": "region_updated", "region": payload.region})
                else:
                    errors.append({"id": acc_id, "error": "Account not found"})
            elif payload.action == "clear_errors":
                cleared = db.clear_account_errors(acc_id)
                results.append({"id": acc_id, "status": "errors_cleared", "cleared": cleared})
        except Exception as e:
            errors.append({"id": acc_id, "error": str(e)})

    await bot_manager.broadcast_state_update()
    return {
        "ok": len(errors) == 0,
        "action": payload.action,
        "processed_count": len(results),
        "error_count": len(errors),
        "results": results,
        "errors": errors,
    }


@app.get("/api/accounts/{account_id}", tags=["Accounts"])
async def get_account_details(account_id: str, admin: Dict = Depends(require_admin)):
    acc = db.get_account_by_id(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
    acc["worker_running"] = bot_manager.is_account_running(account_id)
    return {"account": acc}


@app.put("/api/accounts/{account_id}", tags=["Accounts"])
async def update_account_endpoint(
    account_id: str,
    payload: AccountUpdateRequest,
    admin: Dict = Depends(require_admin),
):
    acc = db.update_account(
        account_id=account_id,
        display_name=payload.display_name,
        password=payload.password,
        region=payload.region,
        mode_id=payload.mode_id,
        map_id=payload.map_id,
        notes=payload.notes,
    )
    if not acc:
        raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
    await bot_manager.broadcast_state_update()
    return {"ok": True, "account": acc}


@app.delete("/api/accounts/{account_id}", tags=["Accounts"])
async def delete_account_endpoint(account_id: str, admin: Dict = Depends(require_admin)):
    await bot_manager.stop_account(account_id, reason="Account deleted")
    ok = db.delete_account(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
    await bot_manager.broadcast_state_update()
    return {"ok": True, "message": "Account removed."}


@app.post("/api/accounts/{account_id}/start", tags=["Accounts"])
async def start_single_account(account_id: str, admin: Dict = Depends(require_admin)):
    try:
        return await bot_manager.start_account(account_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/accounts/{account_id}/stop", tags=["Accounts"])
async def stop_single_account(account_id: str, admin: Dict = Depends(require_admin)):
    return await bot_manager.stop_account(account_id)


@app.post("/api/accounts/{account_id}/restart", tags=["Accounts"])
async def restart_single_account(account_id: str, admin: Dict = Depends(require_admin)):
    try:
        return await bot_manager.restart_account(account_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/accounts/{account_id}/errors", tags=["Accounts"])
async def clear_single_account_errors(account_id: str, admin: Dict = Depends(require_admin)):
    cleared = db.clear_account_errors(account_id)
    await bot_manager.broadcast_state_update()
    return {"ok": True, "cleared": cleared}


# ---------------------------------------------------------------------------
# WebSocket Live Updates Endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws/live")
async def websocket_live_endpoint(websocket: WebSocket):
    admin = await verify_websocket_admin(websocket)
    if not admin:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    bot_manager.ws_clients.add(websocket)
    try:
        await websocket.send_json({
            "type": "dashboard_sync",
            "timestamp": db.utc_now_iso(),
            "data": bot_manager.get_dashboard_overview(),
        })
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_json({"type": "pong", "timestamp": db.utc_now_iso()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bot_manager.ws_clients.discard(websocket)
