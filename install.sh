#!/bin/bash
# Manhwa PDF Merger — one-liner installer
# Usage: curl -fsSL https://raw.githubusercontent.com/IT08-byte/manhwa-pdf-merger/main/install.sh | bash

set -e

REPO="https://github.com/IT08-byte/manhwa-pdf-merger.git"
INSTALL_DIR="$HOME/manhwa-pdf-merger"
ALIAS_CMD='alias manhwa-merger="cd $HOME/manhwa-pdf-merger && python3 app.py"'

echo ""
echo "  Manhwa PDF Merger — Installer"
echo "  ================================"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "  ✗ Python 3 not found."
    echo "  Install it from https://python.org and re-run this script."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PY_VERSION" -lt 9 ]; then
    echo "  ✗ Python 3.9+ is required. You have Python 3.$PY_VERSION."
    echo "  Please upgrade Python from https://python.org"
    exit 1
fi
echo "  ✓ Python $(python3 --version | cut -d' ' -f2) found"

# Download repo
if [ -d "$INSTALL_DIR" ]; then
    echo "  ↻ Updating existing install at $INSTALL_DIR"
    cd "$INSTALL_DIR" && git pull --quiet
else
    if command -v git &>/dev/null; then
        echo "  ↓ Cloning repo to $INSTALL_DIR"
        git clone --quiet "$REPO" "$INSTALL_DIR"
    else
        echo "  ↓ git not found — downloading ZIP instead"
        ZIP_URL="https://github.com/IT08-byte/manhwa-pdf-merger/archive/refs/heads/main.zip"
        TMP_ZIP="/tmp/manhwa-merger.zip"
        curl -fsSL "$ZIP_URL" -o "$TMP_ZIP"
        unzip -q "$TMP_ZIP" -d "$HOME"
        mv "$HOME/manhwa-pdf-merger-main" "$INSTALL_DIR"
        rm "$TMP_ZIP"
    fi
fi
echo "  ✓ Downloaded to $INSTALL_DIR"

# Install dependencies
cd "$INSTALL_DIR"
echo "  ↓ Installing Python dependencies"
pip3 install -r requirements.txt -q
echo "  ✓ Dependencies installed"

# Add alias to shell config
SHELL_RC=""
if [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
elif [ -f "$HOME/.bash_profile" ]; then
    SHELL_RC="$HOME/.bash_profile"
fi

if [ -n "$SHELL_RC" ]; then
    if ! grep -q "manhwa-merger" "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo "# Manhwa PDF Merger" >> "$SHELL_RC"
        echo "$ALIAS_CMD" >> "$SHELL_RC"
        echo "  ✓ Added 'manhwa-merger' command to $SHELL_RC"
        echo "    (restart your terminal or run: source $SHELL_RC)"
    else
        echo "  ✓ 'manhwa-merger' alias already in $SHELL_RC"
    fi
fi

echo ""
echo "  Installation complete!"
echo ""
echo "  To start:"
echo "    cd $INSTALL_DIR && python3 app.py"
echo "    — or after restarting your terminal: manhwa-merger"
echo ""
echo "  Then open http://localhost:5055 in your browser."
echo ""
