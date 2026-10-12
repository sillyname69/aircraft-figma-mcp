#!/usr/bin/env bash
# setup.sh — figma-write-mcp one-time installer (macOS / Linux)
# Run with:  bash setup.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== figma-write-mcp setup ==="
echo ""

# ── Step 1: Find Python 3.10+ ─────────────────────────────────────────────
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10 or later is required but was not found."
    echo ""
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "  Install options (macOS):"
        echo "    python.org:  https://www.python.org/downloads/"
        echo "    Homebrew:    brew install python@3.12"
    else
        echo "  Install options (Linux):"
        echo "    Ubuntu/Debian: sudo apt install python3.12 python3.12-venv"
        echo "    Fedora:        sudo dnf install python3.12"
    fi
    echo ""
    echo "After installing Python, run this script again."
    exit 1
fi

echo "Python: $($PYTHON --version)"

# ── Step 2: Create virtual environment ───────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at .venv/ ..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists — skipping creation."
fi

PIP="$VENV_DIR/bin/pip"
PYTHON_VENV="$VENV_DIR/bin/python"

# ── Step 3: Install Python dependencies ──────────────────────────────────
echo "Installing Python dependencies (mcp, websockets, anyio, pydantic) ..."
"$PIP" install --upgrade pip --quiet
"$PIP" install -r "$SCRIPT_DIR/requirements.txt" --quiet
echo "Done."
echo ""

# ── Step 4: Tesseract check (optional — non-blocking) ────────────────────
if command -v tesseract &>/dev/null; then
    echo "Tesseract OCR: $(tesseract --version 2>&1 | head -1) (found)"
    echo "  Optional OCR scripts in scripts/ are ready to use."
else
    echo "Tesseract OCR: not found (this is OPTIONAL)"
    echo "  The main MCP server works without it."
    echo "  OCR scripts (scripts/extract-geometry.py) need it:"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "    brew install tesseract"
    else
        echo "    sudo apt install tesseract-ocr    # Ubuntu/Debian"
        echo "    sudo dnf install tesseract        # Fedora"
    fi
fi
echo ""

# ── Step 5: MCP config registration ──────────────────────────────────────
SERVER_PATH="$SCRIPT_DIR/server.py"
PYTHON_ABS="$VENV_DIR/bin/python"

echo "=== Registering MCP server with Claude ==="
echo ""

REGISTERED=false

# 5a. Claude Code (claude CLI)
if command -v claude &>/dev/null; then
    echo "Claude Code detected — adding figma-write MCP server ..."
    claude mcp add figma-write -- "$PYTHON_ABS" "$SERVER_PATH"
    echo "  Registered. Restart Claude Code to apply."
    REGISTERED=true
fi

# 5b. Claude Desktop (macOS config file)
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
if [ -f "$DESKTOP_CONFIG" ]; then
    echo "Claude Desktop config found — writing figma-write entry ..."
    "$PYTHON_VENV" - <<PYEOF
import json, sys

config_path = "$DESKTOP_CONFIG"
python_cmd  = "$PYTHON_ABS"
server_arg  = "$SERVER_PATH"

try:
    with open(config_path) as f:
        config = json.load(f)
except Exception:
    config = {}

config.setdefault("mcpServers", {})
config["mcpServers"]["figma-write"] = {
    "command": python_cmd,
    "args": [server_arg]
}

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

print("  Written to:", config_path)
print("  Restart Claude Desktop to apply.")
PYEOF
    REGISTERED=true
fi

# 5c. Linux Claude Desktop config (~/.config/Claude)
LINUX_CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
if [ -f "$LINUX_CONFIG" ]; then
    echo "Claude Desktop config found (Linux) — writing figma-write entry ..."
    "$PYTHON_VENV" - <<PYEOF
import json

config_path = "$LINUX_CONFIG"
python_cmd  = "$PYTHON_ABS"
server_arg  = "$SERVER_PATH"

try:
    with open(config_path) as f:
        config = json.load(f)
except Exception:
    config = {}

config.setdefault("mcpServers", {})
config["mcpServers"]["figma-write"] = {
    "command": python_cmd,
    "args": [server_arg]
}

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

print("  Written to:", config_path)
print("  Restart Claude Desktop to apply.")
PYEOF
    REGISTERED=true
fi

if [ "$REGISTERED" = false ]; then
    echo "Neither Claude Code nor Claude Desktop was detected automatically."
    echo ""
    echo "Add this to your MCP config manually:"
    echo ""
    echo "  Claude Desktop — edit claude_desktop_config.json:"
    echo '  {'
    echo '    "mcpServers": {'
    echo '      "figma-write": {'
    echo "        \"command\": \"$PYTHON_ABS\","
    echo "        \"args\": [\"$SERVER_PATH\"]"
    echo '      }'
    echo '    }'
    echo '  }'
    echo ""
    echo "  Claude Code — run:"
    echo "    claude mcp add figma-write -- \"$PYTHON_ABS\" \"$SERVER_PATH\""
fi

# ── Step 6: Plugin setup instructions ────────────────────────────────────
MANIFEST_PATH="$SCRIPT_DIR/plugin/manifest.json"
echo ""
echo "=== Import the Figma plugin (one time) ==="
echo ""
echo "  1. Open the Figma desktop app and open any design file."
echo "  2. Go to: Plugins → Development → Import plugin from manifest…"
echo "  3. Select:  $MANIFEST_PATH"
echo "  4. The plugin will appear as 'Claude Bridge' under Plugins → Development."
echo ""
echo "=== Starting each session ==="
echo ""
echo "  Run: Plugins → Development → Claude Bridge"
echo "  Wait for the green dot and 'Connected to Claude'."
echo "  Keep the small window open while you work."
echo ""
echo "Setup complete."
