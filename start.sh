#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "Installing dependencies..."
pip3 install -r requirements.txt -q

echo ""
echo "  Open http://localhost:5055 in your browser"
echo ""
python3 app.py
