# -*- coding: utf-8 -*-
"""
MAIN ENTRYPOINT FILE (run.py)
শুধু `python3 run.py` চালালেই অটোমেটিক সব প্যাকেজ সেটআপ হয়ে ওয়েব ড্যাশবোর্ড ও বট চালু হয়ে যাবে!
Google Cloud Shell (Port 8080 & 8000), GitHub Codespaces, Wispbyte এবং লোকাল পিসিতে ১০০% কাজ করে।
"""

import asyncio
import importlib
import os
import subprocess
import sys


def ensure_dependencies() -> None:
    """Auto-install requirements.txt if any required package is missing (e.g. fresh Cloud Shell)."""
    required_modules = [
        "fastapi",
        "uvicorn",
        "httpx",
        "Crypto",
        "google.protobuf",
        "protobuf_decoder",
        "dns",
        "psutil",
        "argon2",
        "cryptography",
        "google_play_scraper",
        "requests",
    ]
    missing = []
    for mod in required_modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)

    if not missing:
        return

    req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    print("=" * 65)
    print(f"[*] Missing packages detected ({', '.join(missing)}).")
    print("[*] Auto-installing dependencies from requirements.txt...")
    print("=" * 65)
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", req_file])
    except subprocess.CalledProcessError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "--user", "--break-system-packages", "-r", req_file]
        )
    print("[+] All dependencies installed successfully!")


ensure_dependencies()

import uvicorn
from app.config import HOST, PORT


async def _serve_multi_port(host: str, primary_port: int, extra_port: int) -> None:
    config_main = uvicorn.Config(
        "app.main:app",
        host=host,
        port=primary_port,
        reload=False,
        loop="asyncio",
        access_log=False,
    )
    server_main = uvicorn.Server(config_main)

    config_extra = uvicorn.Config(
        "app.main:app",
        host=host,
        port=extra_port,
        reload=False,
        loop="asyncio",
        access_log=False,
    )
    server_extra = uvicorn.Server(config_extra)

    await asyncio.gather(server_main.serve(), server_extra.serve())


if __name__ == "__main__":
    is_cloud_shell = os.getenv("CLOUD_SHELL") == "true" or bool(os.getenv("DEVSHELL_PROJECT_ID"))
    print("=" * 65)
    print("  TEAM 84FF - Multi-Account Web Dashboard & Bot Engine")
    print(f"  Primary URL   : http://localhost:{PORT}")
    if is_cloud_shell and PORT != 8080:
        print("  Cloud Shell   : http://localhost:8080 (Web Preview Default)")
    print("  Default Login : admin / admin123")
    print("=" * 65)

    if is_cloud_shell and PORT != 8080:
        try:
            asyncio.run(_serve_multi_port(HOST, PORT, 8080))
        except Exception:
            uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False, loop="asyncio", access_log=False)
    else:
        uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False, loop="asyncio", access_log=False)
