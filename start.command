#!/bin/sh
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "First start: installing what the tool needs. This takes a minute."
  python3 -m venv .venv || { echo "Python 3 was not found. Install Python 3.11 or newer."; read -r; exit 1; }
  ".venv/bin/python" -m pip install --quiet --upgrade pip
  ".venv/bin/python" -m pip install --quiet -r requirements-proofread.txt || { echo "Installing failed. Check the internet connection and start again."; rm -rf .venv; read -r; exit 1; }
fi
echo "Starting the proofreading tool. Keep this window open while you work."
".venv/bin/python" review/server.py
