# -*- coding: utf-8 -*-
"""
Async Multi-Account Worker Orchestrator & WebSocket Broadcast Manager.
Wraps lv.py's core functions without breaking CLI compatibility.
"""

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Set
import psutil
from fastapi import WebSocket

import lv
from app import database as db
from app.crypto_vault import redact_sensitive


class BotManager:
    def __init__(self) -> None:
        self.start_time: float = time.time()
        self.workers: Dict[str, asyncio.Task] = {}
        self.worker_started_at: Dict[str, float] = {}
        self.uid_to_account_id: Dict[str, str] = {}
        self.game_id_to_account_id: Dict[str, str] = {}
        self.active_account_data: Dict[str, Dict[str, Any]] = {}
        self.ws_clients: Set[WebSocket] = set()
        self._broadcast_lock = asyncio.Lock()

        self.diagnostics: Dict[str, Any] = {
            "last_checked": None,
            "playstore_version": "Checking...",
            "release_version": headers_release_ver(),
            "remote_version": "Unknown",
            "server_url": lv.REGION_SERVERS.get("ME", ""),
            "dns_resolved_ip": None,
            "dns_latency_ms": None,
            "cloudflare_dns": lv.CLOUDFLARE_PRIMARY_DNS,
            "version_api_ok": None,
        }

        lv.register_log_callback(self._on_lv_log)

    def _on_lv_log(self, raw_msg: str) -> None:
        """Intercepts stdout logs from lv.py, redacts secrets, and maps events to accounts."""
        msg = redact_sensitive(raw_msg.strip())
        if not msg:
            return

        # Skip noisy per-packet debug lines in activity feed
        if msg.startswith("[DEBUG]"):
            return

        matched_acc_id: Optional[str] = None
        for uid_val, acc_id in list(self.uid_to_account_id.items()):
            if uid_val and uid_val in msg:
                matched_acc_id = acc_id
                break
        if not matched_acc_id:
            for gid_val, acc_id in list(self.game_id_to_account_id.items()):
                if gid_val and gid_val in msg:
                    matched_acc_id = acc_id
                    break

        if matched_acc_id:
            self._process_account_log_event(matched_acc_id, msg)

    def _process_account_log_event(self, account_id: str, msg: str) -> None:
        """Parse lv.py log patterns to update account status & counters in real time."""
        try:
            if "TCP gateway connected" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="online",
                    status_message="TCP Gateway Connected (Online)",
                )
            elif "-> BR match #" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="searching_match",
                    status_message=msg.replace("[BR] ", ""),
                    inc_matches_searched=1,
                )
            elif "Queue confirmed (BR)" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="searching_match",
                    status_message="In BR Matchmaking Queue",
                )
            elif "Match found [BR]" in msg or "[BR] injected ->" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="in_match",
                    status_message="In Active BR Match Session (UDP)",
                )
                db.add_activity_log(
                    account_id=account_id,
                    account_label=account_id,
                    level="success",
                    event_type="match_joined",
                    message=msg,
                )
            elif "Startup delivered. Playing." in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="in_match",
                    status_message="Match Startup Delivered — Playing BR",
                )
                db.add_activity_log(
                    account_id=account_id,
                    account_label=account_id,
                    level="info",
                    event_type="match_playing",
                    message=msg.split(" | UID:")[0],
                )
            elif "[SEND-OK] JOIN-MATCH-REPLY" in msg or "[SEND-OK] ACK-101" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="in_match",
                    status_message="In BR Match — Session Handshake Active (UDP)",
                )
            elif "[SEND-OK] PING-REPLY" in msg:
                return
            elif "UDP handshake timeout (25s)" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="reconnecting",
                    status_message="UDP handshake timed out (25s) — re-queueing next match...",
                )
                db.add_activity_log(
                    account_id=account_id,
                    account_label=account_id,
                    level="warning",
                    event_type="match_timeout",
                    message=msg.split(" | UID:")[0],
                )
            elif "finished (cmd" in msg or "finished (max duration" in msg or "finished (UDP session complete)" in msg:
                acc_data = self.active_account_data.get(account_id, {})
                lvl = int(acc_data.get("level") or 0) or None
                exp = int(acc_data.get("exp") or 0) if "exp" in acc_data else None
                db.update_account_runtime_state(
                    account_id,
                    status="online",
                    status_message=msg.replace("[★] ", "").split(" | UID:")[0],
                    level=lvl,
                    exp=exp,
                    inc_matches_completed=1,
                )
                db.add_activity_log(
                    account_id=account_id,
                    account_label=account_id,
                    level="success",
                    event_type="match_completed",
                    message=msg.split(" | UID:")[0],
                )
            elif "Profile sync | UID" in msg:
                acc_data = self.active_account_data.get(account_id, {})
                lvl = int(acc_data.get("level") or 0) or None
                exp = int(acc_data.get("exp") or 0) if "exp" in acc_data else None
                db.update_account_runtime_state(
                    account_id,
                    level=lvl,
                    exp=exp,
                )
                db.add_activity_log(
                    account_id=account_id,
                    account_label=account_id,
                    level="success",
                    event_type="profile_sync",
                    message=msg,
                )
            elif "| Lvl " in msg and "Next: BR" in msg:
                acc_data = self.active_account_data.get(account_id, {})
                lvl = int(acc_data.get("level") or 0) or None
                exp = int(acc_data.get("exp") or 0) if "exp" in acc_data else None
                db.update_account_runtime_state(
                    account_id,
                    level=lvl,
                    exp=exp,
                    status_message=msg.replace("[*] ", ""),
                )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.broadcast_state_update())
            except RuntimeError:
                pass
        except Exception:
            pass

    async def run_diagnostics(self, region: str = "ME") -> Dict[str, Any]:
        """Run real DNS resolution and version checks using lv.py functions."""
        t0 = time.perf_counter()
        resolved_ip = await lv.resolve_host_cloudflare("clientbp.ppmainecoonghj.com")
        dns_ms = round((time.perf_counter() - t0) * 1000, 1)

        play_ver = await lv.get_playstore_version()
        ver_cfg = await lv.version_config(region=region)

        if ver_cfg:
            rel_ver, rem_ver, srv_url = ver_cfg
            ver_ok = True
        else:
            rel_ver = lv.headers.get("ReleaseVersion", "OB55")
            rem_ver = play_ver or "1.132.8"
            srv_url = lv.REGION_SERVERS.get(region.upper(), "https://clientbp.ppmainecoonghj.com/")
            ver_ok = False

        self.diagnostics = {
            "last_checked": db.utc_now_iso(),
            "playstore_version": play_ver or "1.132.8",
            "release_version": rel_ver,
            "remote_version": rem_ver,
            "server_url": srv_url,
            "dns_resolved_ip": resolved_ip,
            "dns_latency_ms": dns_ms,
            "cloudflare_dns": lv.CLOUDFLARE_PRIMARY_DNS,
            "version_api_ok": ver_ok,
        }
        return self.diagnostics

    def is_account_running(self, account_id: str) -> bool:
        t = self.workers.get(account_id)
        return bool(t and not t.done())

    async def start_account(self, account_id: str) -> Dict[str, Any]:
        if self.is_account_running(account_id):
            return {"ok": True, "message": "Account worker is already running.", "account": db.get_account_by_id(account_id)}

        cred_row = db.get_account_credentials_internal(account_id)
        if not cred_row:
            raise ValueError(f"Account '{account_id}' not found.")

        uid = cred_row["uid"]
        password = cred_row["password_plain"]
        if not password:
            db.record_account_error(account_id, "Credentials", "Stored credential could not be decrypted or is empty.")
            db.update_account_runtime_state(
                account_id,
                status="error",
                status_message="Missing or invalid encrypted password. Please edit account password.",
            )
            raise ValueError("Missing or undecryptable password for this account.")

        self.uid_to_account_id[uid] = account_id
        self.worker_started_at[account_id] = time.time()

        db.update_account_runtime_state(
            account_id,
            status="authenticating",
            status_message="Starting worker — verifying version & Garena OAuth...",
        )
        db.add_activity_log(
            account_id=account_id,
            account_label=f"{cred_row['display_name']} ({uid})",
            level="info",
            event_type="worker_start",
            message=f"Started worker for '{cred_row['display_name']}' (UID {uid}, Region {cred_row['region']}).",
        )

        task = asyncio.create_task(self._account_worker_lifecycle(account_id))
        self.workers[account_id] = task
        await self.broadcast_state_update()
        return {"ok": True, "message": "Account worker started.", "account": db.get_account_by_id(account_id)}

    async def stop_account(self, account_id: str, reason: str = "Stopped by administrator") -> Dict[str, Any]:
        task = self.workers.pop(account_id, None)
        started = self.worker_started_at.pop(account_id, None)
        elapsed = int(time.time() - started) if started else 0
        self.active_account_data.pop(account_id, None)

        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        acc = db.get_account_by_id(account_id)
        if acc:
            db.update_account_runtime_state(
                account_id,
                status="inactive",
                status_message=reason,
                inc_uptime_seconds=elapsed,
            )
            db.add_activity_log(
                account_id=account_id,
                account_label=f"{acc['display_name']} ({acc['uid']})",
                level="warning",
                event_type="worker_stop",
                message=f"Worker stopped for '{acc['display_name']}': {reason}.",
            )
        await self.broadcast_state_update()
        return {"ok": True, "message": "Account worker stopped.", "account": db.get_account_by_id(account_id)}

    async def restart_account(self, account_id: str) -> Dict[str, Any]:
        await self.stop_account(account_id, reason="Restarting worker...")
        await asyncio.sleep(0.2)
        return await self.start_account(account_id)

    async def _account_worker_lifecycle(self, account_id: str) -> None:
        """
        Executes the full lv.py login and BR worker loop for a single managed account,
        capturing accurate per-stage status and errors without fabricating success.
        """
        cred = db.get_account_credentials_internal(account_id)
        if not cred:
            return

        uid = str(cred["uid"])
        password = str(cred["password_plain"])
        region = str(cred.get("region") or "ME").upper()
        display_name = str(cred.get("display_name") or uid)
        label = f"{display_name} ({uid})"

        max_consecutive_auth_failures = 3
        auth_failures = 0

        try:
            while True:
                db.update_account_runtime_state(
                    account_id,
                    status="authenticating",
                    status_message=f"Step 1/4: Fetching version config for region {region}...",
                )
                await self.broadcast_state_update()

                async with lv._LOGIN_SEMAPHORE:
                    verconfig_res = await lv.version_config(region=region)
                    if verconfig_res is None:
                        # Fallback to PlayStore version + standard region server URL if version API is unreachable
                        play_ver = await lv.get_playstore_version()
                        release_version = lv.headers.get("ReleaseVersion", "OB55")
                        client_version = play_ver or "1.132.8"
                        server_url = lv.REGION_SERVERS.get(region, "https://clientbp.ppmainecoonghj.com/")
                        db.record_account_error(
                            account_id,
                            "VersionConfig",
                            f"Whitehawk version API unreachable; using fallback release={release_version}, client={client_version}.",
                        )
                    else:
                        release_version, client_version, server_url = verconfig_res

                    db.update_account_runtime_state(
                        account_id,
                        status="authenticating",
                        status_message="Step 2/4: Requesting Garena Guest OAuth token...",
                    )
                    await self.broadcast_state_update()

                    tokengrant_response = await lv.get_access_token(uid, password)
                    if tokengrant_response is None:
                        auth_failures += 1
                        err_msg = (
                            f"Garena OAuth token grant failed for UID {uid} "
                            f"(attempt {auth_failures}/{max_consecutive_auth_failures}). "
                            "Check guest UID/password or Garena OAuth availability."
                        )
                        db.record_account_error(account_id, "GarenaOAuth", err_msg)
                        db.add_activity_log(
                            account_id=account_id,
                            account_label=label,
                            level="error",
                            event_type="oauth_failed",
                            message=err_msg,
                        )
                        if auth_failures >= max_consecutive_auth_failures:
                            db.update_account_runtime_state(
                                account_id,
                                status="error",
                                status_message="OAuth authentication failed after 3 attempts (invalid UID/password or rate-limited).",
                            )
                            await self.broadcast_state_update()
                            break

                        db.update_account_runtime_state(
                            account_id,
                            status="reconnecting",
                            status_message=f"OAuth failed (attempt {auth_failures}/{max_consecutive_auth_failures}) — retrying in 15s...",
                            inc_reconnects=1,
                        )
                        await self.broadcast_state_update()
                        await asyncio.sleep(15)
                        continue

                    open_id, access_token, platform = tokengrant_response

                    db.update_account_runtime_state(
                        account_id,
                        status="authenticating",
                        status_message="Step 3/4: Encrypting & sending MajorLogin payload...",
                    )
                    await self.broadcast_state_update()

                    device_info = lv.get_device_for_account(uid)
                    login_payload_data = await lv.build_majorlogin_payload(
                        open_id, access_token, platform, client_version, device_info
                    )
                    if login_payload_data is None:
                        err_msg = "Failed to build/encrypt MajorLogin protobuf payload."
                        db.record_account_error(account_id, "MajorLoginBuild", err_msg)
                        db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                        await self.broadcast_state_update()
                        break

                    majorlogin_response = await lv.send_majorlogin(
                        login_payload_data, release_version, server_url
                    )
                    if majorlogin_response is None:
                        auth_failures += 1
                        err_msg = f"MajorLogin rejected or timed out from {server_url} (release {release_version})."
                        db.record_account_error(account_id, "MajorLogin", err_msg)
                        if auth_failures >= max_consecutive_auth_failures:
                            db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                            await self.broadcast_state_update()
                            break
                        db.update_account_runtime_state(
                            account_id,
                            status="reconnecting",
                            status_message=f"MajorLogin failed — retrying in 15s ({auth_failures}/{max_consecutive_auth_failures})...",
                            inc_reconnects=1,
                        )
                        await self.broadcast_state_update()
                        await asyncio.sleep(15)
                        continue

                    db.update_account_runtime_state(
                        account_id,
                        status="authenticating",
                        status_message="Step 4/4: Fetching GetLoginData & gateway addresses...",
                    )
                    await self.broadcast_state_update()

                    getlogin_result = await lv.send_getlogin(
                        login_payload_data,
                        majorlogin_response.url or server_url,
                        majorlogin_response.token,
                        release_version,
                    )
                    if getlogin_result is None:
                        auth_failures += 1
                        err_msg = "GetLoginData failed to return profile & gateway addresses."
                        db.record_account_error(account_id, "GetLoginData", err_msg)
                        if auth_failures >= max_consecutive_auth_failures:
                            db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                            await self.broadcast_state_update()
                            break
                        await asyncio.sleep(15)
                        continue

                    res_proto, dict_res = getlogin_result

                raw_acc_id = (
                    majorlogin_response.account_id
                    or getattr(res_proto, "account_id", 0)
                    or lv.get_proto_field(dict_res, 1, 0)
                    or 0
                )
                game_acc_id = str(raw_acc_id)
                if not game_acc_id or game_acc_id in ("0", "None"):
                    err_msg = (
                        f"Server returned invalid account_id (0) for UID {uid}. "
                        "Either this Guest UID has not created an in-game character/nickname yet, "
                        "is suspended/banned, or thunderFF_pb2 schema mismatched."
                    )
                    db.record_account_error(account_id, "AccountValidation", err_msg)
                    db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                    await self.broadcast_state_update()
                    break
                majorlogin_response.account_id = int(game_acc_id)

                auth_failures = 0
                self.game_id_to_account_id[game_acc_id] = account_id

                level = max(1, int(lv.get_proto_field(dict_res, 6, 1) or 1))
                exp = max(0, int(lv.get_proto_field(dict_res, 7, 0) or 0))
                nickname = res_proto.nickname or lv.get_proto_field(dict_res, 4, f"Player_{game_acc_id}")
                resolved_region = majorlogin_response.region or lv.get_proto_field(dict_res, 3, region)

                func_addr = res_proto.functional_addrs
                info_addr = res_proto.informational_addrs
                if not isinstance(func_addr, str) or ":" not in func_addr:
                    func_addr = None
                if not isinstance(info_addr, str) or ":" not in info_addr:
                    info_addr = None

                key_val = majorlogin_response.aes_ak
                iv_val = majorlogin_response.iv_i
                if not isinstance(key_val, (bytes, bytearray)) or len(key_val) < 16:
                    key_val = lv.AES_KEY
                if not isinstance(iv_val, (bytes, bytearray)) or len(iv_val) < 16:
                    iv_val = lv.AES_IV

                account_data = {
                    "account_id": majorlogin_response.account_id,
                    "nickname": nickname,
                    "region": resolved_region,
                    "level": level,
                    "exp": exp,
                    "open_id": open_id,
                    "access_token": access_token,
                    "platform": str(platform),
                    "token": majorlogin_response.token,
                    "server_time": majorlogin_response.server_time,
                    "aes_ak": key_val,
                    "iv_i": iv_val,
                    "functional_addrs": func_addr,
                    "informational_addrs": info_addr,
                    "release_version": release_version,
                    "client_version": client_version,
                    "server_url": majorlogin_response.url or server_url,
                    "login_payload_data": login_payload_data,
                    "auth_type": "guest",
                    "auth_uid": uid,
                    "auth_password": password,
                }
                self.active_account_data[account_id] = account_data

                db.update_account_runtime_state(
                    account_id,
                    status="online",
                    status_message=f"Login OK — Lvl {level} ({exp:,} EXP) | Connecting to BR gateway...",
                    game_account_id=game_acc_id,
                    nickname=nickname,
                    region=resolved_region,
                    level=level,
                    exp=exp,
                )
                db.add_activity_log(
                    account_id=account_id,
                    account_label=label,
                    level="success",
                    event_type="login_success",
                    message=f"Authenticated UID {game_acc_id} ({nickname}) | Lvl {level} | Region {resolved_region}.",
                )
                await self.broadcast_state_update()

                if not func_addr:
                    err_msg = f"No functional TCP gateway address returned for account {game_acc_id}."
                    db.record_account_error(account_id, "TCPGateway", err_msg)
                    db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                    await self.broadcast_state_update()
                    break

                await lv.run_account_worker(account_data, uid)

                db.update_account_runtime_state(
                    account_id,
                    status="reconnecting",
                    status_message="Session ended — reconnecting in 3s...",
                    inc_reconnects=1,
                )
                await self.broadcast_state_update()
                await asyncio.sleep(3)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            err_msg = redact_sensitive(f"Unexpected worker exception: {e}")
            db.record_account_error(account_id, "WorkerRuntime", err_msg)
            db.update_account_runtime_state(
                account_id,
                status="error",
                status_message=err_msg,
            )
            db.add_activity_log(
                account_id=account_id,
                account_label=label,
                level="error",
                event_type="worker_exception",
                message=err_msg,
            )
            await self.broadcast_state_update()
        finally:
            started = self.worker_started_at.pop(account_id, None)
            if started:
                db.update_account_runtime_state(
                    account_id,
                    inc_uptime_seconds=int(time.time() - started),
                )
            self.active_account_data.pop(account_id, None)

    def get_system_status(self) -> Dict[str, Any]:
        uptime_sec = int(time.time() - self.start_time)
        cpu_pct = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        proc = psutil.Process()
        proc_mem_mb = round(proc.memory_info().rss / (1024 * 1024), 1)

        active_workers = sum(1 for t in self.workers.values() if not t.done())
        return {
            "uptime_seconds": uptime_sec,
            "cpu_percent": round(cpu_pct, 1),
            "memory_percent": round(vm.percent, 1),
            "process_memory_mb": proc_mem_mb,
            "active_workers": active_workers,
            "websocket_clients": len(self.ws_clients),
            "dns_cache_entries": len(lv._DNS_CACHE),
            "start_match_interval_sec": lv.START_MATCH_INTERVAL,
            "max_match_duration_sec": lv.MAX_MATCH_DURATION,
            "supported_regions": sorted(list(lv.REGION_PREFIX.keys())),
            "diagnostics": self.diagnostics,
        }

    def get_dashboard_overview(self) -> Dict[str, Any]:
        accounts = db.list_accounts()
        acc_by_id = {a["id"]: a for a in accounts}
        updated_any = False
        for acc_id, live_data in list(self.active_account_data.items()):
            try:
                db_acc = acc_by_id.get(acc_id)
                if not db_acc:
                    continue
                live_lvl = int(live_data.get("level") or 0)
                live_exp = int(live_data.get("exp") or 0)
                live_nick = str(live_data.get("nickname") or "")
                if live_lvl > 0 and (
                    live_lvl != int(db_acc.get("level") or 0)
                    or live_exp != int(db_acc.get("exp") or 0)
                ):
                    db.update_account_runtime_state(
                        acc_id,
                        level=live_lvl,
                        exp=live_exp,
                        nickname=live_nick if live_nick else None,
                    )
                    updated_any = True
            except Exception:
                pass
        if updated_any:
            accounts = db.list_accounts()
        total = len(accounts)
        active_statuses = {"authenticating", "online", "searching_match", "in_match", "reconnecting"}
        active = sum(1 for a in accounts if a["status"] in active_statuses)
        error_cnt = sum(1 for a in accounts if a["status"] == "error")
        inactive = total - active

        status_breakdown = {
            "in_match": sum(1 for a in accounts if a["status"] == "in_match"),
            "searching_match": sum(1 for a in accounts if a["status"] == "searching_match"),
            "online": sum(1 for a in accounts if a["status"] == "online"),
            "authenticating": sum(1 for a in accounts if a["status"] in ("authenticating", "reconnecting")),
            "inactive": sum(1 for a in accounts if a["status"] == "inactive"),
            "error": error_cnt,
        }

        region_breakdown: Dict[str, int] = {}
        for a in accounts:
            r = a.get("region", "ME")
            region_breakdown[r] = region_breakdown.get(r, 0) + 1

        total_matches_completed = sum(int(a.get("matches_completed") or 0) for a in accounts)
        total_matches_searched = sum(int(a.get("matches_searched") or 0) for a in accounts)
        total_exp = sum(int(a.get("exp") or 0) for a in accounts)
        avg_level = round(sum(int(a.get("level") or 1) for a in accounts) / total, 1) if total > 0 else 0.0
        total_errors = sum(int(a.get("error_count") or 0) for a in accounts)

        return {
            "summary": {
                "total_accounts": total,
                "active_accounts": active,
                "inactive_accounts": inactive,
                "error_accounts": error_cnt,
                "total_matches_completed": total_matches_completed,
                "total_matches_searched": total_matches_searched,
                "total_exp": total_exp,
                "average_level": avg_level,
                "total_errors": total_errors,
            },
            "status_breakdown": status_breakdown,
            "region_breakdown": region_breakdown,
            "system": self.get_system_status(),
            "recent_activity": db.get_recent_activity(limit=30),
            "accounts": accounts,
        }

    async def broadcast_state_update(self, force: bool = False) -> None:
        if not self.ws_clients:
            return
        now = time.time()
        last_b = getattr(self, "_last_broadcast_ts", 0.0)
        if not force and (now - last_b) < 2.0:
            return
        self._last_broadcast_ts = now
        async with self._broadcast_lock:
            payload = {
                "type": "dashboard_sync",
                "timestamp": db.utc_now_iso(),
                "data": self.get_dashboard_overview(),
            }
            stale: List[WebSocket] = []
            for ws in list(self.ws_clients):
                try:
                    await ws.send_json(payload)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self.ws_clients.discard(ws)

    async def resume_active_accounts_on_startup(self) -> None:
        """Auto-resume accounts that were left in an active state prior to server restart."""
        accounts = db.list_accounts()
        active_statuses = {"authenticating", "online", "searching_match", "in_match", "reconnecting"}
        for acc in accounts:
            if acc.get("status") in active_statuses and not self.is_account_running(acc["id"]):
                try:
                    await self.start_account(acc["id"])
                    await asyncio.sleep(0.3)
                except Exception:
                    db.update_account_runtime_state(
                        acc["id"],
                        status="inactive",
                        status_message="Stopped (click Start to run worker)",
                    )

    async def shutdown_all(self) -> None:
        for acc_id in list(self.workers.keys()):
            try:
                await self.stop_account(acc_id, reason="Server shutting down")
            except Exception:
                pass


def headers_release_ver() -> str:
    return str(lv.headers.get("ReleaseVersion", "OB55"))


bot_manager = BotManager()
