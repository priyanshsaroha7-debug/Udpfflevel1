#!/usr/bin/env bash
set -e

echo "================================================================="
echo "  TEAM 84FF - Auto Setup & Start Script (Cloud Shell / Linux)"
echo "================================================================="

if [ -f "84FF_Multi_Account_Dashboard.zip" ] && [ ! -f "run.py" ]; then
    echo "[*] Extracting 84FF_Multi_Account_Dashboard.zip..."
    unzip -o 84FF_Multi_Account_Dashboard.zip
fi

echo "[*] Installing Python packages..."
python3 -m pip install -q -r requirements.txt || python3 -m pip install -q --user --break-system-packages -r requirements.txt

echo "[+] Starting TEAM 84FF Dashboard..."
exec python3 run.py
