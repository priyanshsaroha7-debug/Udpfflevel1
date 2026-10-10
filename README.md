# TEAM 84FF — Multi-Account Web Dashboard & Command Center

A modern, responsive dark-themed web dashboard and multi-account orchestration system built around `lv.py` (`lv.txt`) without breaking its standalone CLI capabilities.

---

## 1. Project Folder Structure

```text
/home/user/
├── .env.example                 # Environment variable configuration template
├── .env                         # Active environment variables (auto-generated keys)
├── requirements.txt             # Complete Python package dependencies
├── Procfile                     # Process declaration for Railway / Render / Heroku
├── render.yaml                  # Render.com Free Tier Blueprint configuration
├── Dockerfile                   # Docker container definition for Fly.io / Koyeb / HF Spaces
├── README.md                    # Complete setup, architecture, and deployment guide
├── lv.py                        # Preserved original CLI script with env-var & log-hook support
├── message_ids.py               # UDP/RUDP command ID mapping required by lv.py
├── thunderFF_pb2.py             # Wire-compatible Protobuf message definitions required by lv.py
├── data/                        # Runtime SQLite database & persistent devices.json
│   ├── dashboard.db
│   └── devices.json
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI entrypoint, REST API endpoints & WebSocket /ws/live
│   ├── config.py                # Environment variable loader & secret key initializer
│   ├── auth.py                  # Argon2id admin authentication, HMAC-SHA256 session tokens & rate limiter
│   ├── crypto_vault.py          # Fernet (AES-128-CBC + HMAC-SHA256) password encryption & log redaction
│   ├── database.py              # SQLite WAL persistence layer for accounts, errors & activity logs
│   ├── schemas.py               # Pydantic v2 input validation models
│   └── bot_manager.py           # Asyncio multi-account worker manager wrapping lv.py functions
└── static/
    ├── index.html               # Responsive single-page dashboard UI
    ├── css/
    │   └── styles.css           # Obsidian dark theme, responsive layout, animations
    └── js/
        └── app.js               # Frontend state, live WebSocket/polling sync, SVG charts & CRUD
```

---

## 2. Local Setup Instructions

### Prerequisites
- Python 3.10+ (tested on Python 3.11 – 3.13)

### Step-by-Step Installation
```bash
# 1. Create and activate a virtual environment (optional but recommended)
python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy the environment configuration example
cp .env.example .env

# 4. Start the Web Dashboard server
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser.
- **Default Administrator Username:** `admin`
- **Default Administrator Password:** `admin123` (Change this in `.env` or via the **System & Security** tab inside the dashboard).

### Running the Original CLI Script Directly
Your original CLI workflow remains intact:
```bash
python3 lv.py
```

---

## 3. Free Hosting & Deployment Instructions

> **Important Hosting Note on Outbound TCP/UDP Sockets:**
> The web dashboard, REST API, SQLite database, Cloudflare DNS checks, and HTTP/HTTPS login checks run on any standard free Python host. However, full Battle Royale match sessions (`play_game`) require outbound **raw UDP sockets** (`socket.SOCK_DGRAM`), which some HTTP-only serverless platforms block. Below are the best free options:

### Option A: Render.com (Free Web Service)
1. Push this project folder to a private GitHub repository.
2. Log in to [Render.com](https://render.com) and click **New +** -> **Blueprint** (uses `render.yaml`) or **Web Service**.
3. Set Build Command: `pip install -r requirements.txt`
4. Set Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add environment variables `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

### Option B: Fly.io / Railway / Koyeb (Recommended for Full TCP + UDP Socket Support)
1. Install `flyctl` or connect your GitHub repo to Railway / Koyeb.
2. Deploy using the included `Dockerfile`.
3. Set `PORT=8000`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SECRET_KEY`, and `FERNET_KEY` in the platform's secrets manager.

### Option C: Oracle Cloud Free Tier / Free Linux VPS
1. Provision an Ubuntu VM on Oracle Cloud Always-Free Tier.
2. Clone the repository, install dependencies (`pip install -r requirements.txt`), and run with `systemd` or `tmux`:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   This provides unrestricted outbound TCP and UDP socket access for all accounts.
