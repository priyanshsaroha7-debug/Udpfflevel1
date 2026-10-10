# -*- coding: utf-8 -*-
import os
import secrets
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ENV_PATH)


def _ensure_env_secrets() -> tuple[str, str]:
    """Ensure persistent SECRET_KEY and FERNET_KEY exist so encrypted accounts survive restarts."""
    secret_key = os.getenv("SECRET_KEY", "").strip()
    fernet_key = os.getenv("FERNET_KEY", "").strip()

    updated_lines = []
    need_write = False

    if not secret_key:
        secret_key = secrets.token_hex(32)
        os.environ["SECRET_KEY"] = secret_key
        updated_lines.append(f"SECRET_KEY={secret_key}")
        need_write = True

    if not fernet_key:
        fernet_key = Fernet.generate_key().decode("utf-8")
        os.environ["FERNET_KEY"] = fernet_key
        updated_lines.append(f"FERNET_KEY={fernet_key}")
        need_write = True

    if need_write:
        try:
            existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
            with open(ENV_PATH, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                for line in updated_lines:
                    f.write(line + "\n")
        except Exception:
            pass

    return secret_key, fernet_key


SECRET_KEY, FERNET_KEY = _ensure_env_secrets()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123").strip()
SESSION_EXPIRE_HOURS = int(os.getenv("SESSION_EXPIRE_HOURS", "24"))

DATABASE_PATH = os.getenv("DATABASE_PATH", str(DATA_DIR / "dashboard.db"))
DEVICES_FILE_PATH = os.getenv("DEVICES_FILE_PATH", str(DATA_DIR / "devices.json"))
os.environ["DEVICES_FILE_PATH"] = DEVICES_FILE_PATH

DEFAULT_ACCOUNT_REGION = os.getenv("DEFAULT_ACCOUNT_REGION", "ME").upper()
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")
