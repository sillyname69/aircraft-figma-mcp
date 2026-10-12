# setup.ps1 — figma-write-mcp one-time installer (Windows)
#
# Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Or right-click setup.ps1 → "Run with PowerShell"
# (If blocked by execution policy, open PowerShell as Administrator and run:
#   Set-ExecutionPolicy RemoteSigned -Scope CurrentUser)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== figma-write-mcp setup ===" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Find Python 3.10+ ─────────────────────────────────────────────
$Python = $null

# Try Windows Python Launcher first (most reliable on Windows), then bare names
$candidates = @("py", "python3", "python")

foreach ($candidate in $candidates) {
    try {
        # For "py" launcher, try explicit version flags
        if ($candidate -eq "py") {
            foreach ($ver in @("-3.13", "-3.12", "-3.11", "-3.10")) {
                try {
                    $result = & py $ver -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" 2>$null
                    if ($LASTEXITCODE -eq 0) {
                        $Python = "py $ver"
                        break
                    }
                } catch {}
            }
            if ($Python) { break }
        } else {
            $result = & $candidate -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $Python = $candidate
                break
            }
        }
    } catch {}
}

if (-not $Python) {
    Write-Host "ERROR: Python 3.10 or later is required but was not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install options (Windows):"
    Write-Host "  python.org:  https://www.python.org/downloads/"
    Write-Host "               IMPORTANT: check 'Add Python to PATH' during install"
    Write-Host "  winget:      winget install Python.Python.3.12"
    Write-Host ""
    Write-Host "After installing Python, close this window and run setup.ps1 again."
    Read-Host "Press Enter to exit"
    exit 1
}

# Get version string for display
$pythonArgs = $Python -split " "
$PythonExe = $pythonArgs[0]
$PythonFlags = $pythonArgs[1..($pythonArgs.Length-1)]
$verStr = & $PythonExe @PythonFlags --version 2>&1
Write-Host "Python: $verStr"

# ── Step 2: Create virtual environment ───────────────────────────────────
$VenvDir = Join-Path $ScriptDir ".venv"
if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating virtual environment at .venv\ ..."
    & $PythonExe @PythonFlags -m venv $VenvDir
} else {
    Write-Host "Virtual environment already exists — skipping creation."
}

$PipPath    = Join-Path $VenvDir "Scripts\pip.exe"
$PythonVenv = Join-Path $VenvDir "Scripts\python.exe"
$ReqFile    = Join-Path $ScriptDir "requirements.txt"

# ── Step 3: Install Python dependencies ──────────────────────────────────
Write-Host "Installing Python dependencies (mcp, websockets, anyio, pydantic) ..."
& $PipPath install --upgrade pip --quiet
& $PipPath install -r $ReqFile --quiet
Write-Host "Done."
Write-Host ""

# ── Step 4: Tesseract check (optional — non-blocking) ────────────────────
$TessCmd = Get-Command tesseract -ErrorAction SilentlyContinue
if ($TessCmd) {
    $tessVer = & tesseract --version 2>&1 | Select-Object -First 1
    Write-Host "Tesseract OCR: $tessVer (found)"
    Write-Host "  Optional OCR scripts in scripts\ are ready to use."
} else {
    Write-Host "Tesseract OCR: not found (this is OPTIONAL)"
    Write-Host "  The main MCP server works without it."
    Write-Host "  OCR scripts (scripts\extract-geometry.py) need it:"
    Write-Host "    winget install UB-Mannheim.TesseractOCR"
    Write-Host "    or: https://github.com/UB-Mannheim/tesseract/wiki"
}
Write-Host ""

# ── Step 5: MCP config registration ──────────────────────────────────────
$ServerPath = Join-Path $ScriptDir "server.py"
$PythonAbs  = $PythonVenv

Write-Host "=== Registering MCP server with Claude ===" -ForegroundColor Cyan
Write-Host ""

$Registered = $false

# 5a. Claude Code (claude CLI)
$ClaudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if ($ClaudeCmd) {
    Write-Host "Claude Code detected — adding figma-write MCP server ..."
    & claude mcp add figma-write -- $PythonAbs $ServerPath
    Write-Host "  Registered. Restart Claude Code to apply."
    $Registered = $true
}

# 5b. Claude Desktop (Windows config file)
$DesktopConfig = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
if (Test-Path $DesktopConfig) {
    Write-Host "Claude Desktop config found — writing figma-write entry ..."

    # Escape backslashes for Python string literals
    $PythonAbsEsc  = $PythonAbs  -replace '\\', '\\'
    $ServerPathEsc = $ServerPath -replace '\\', '\\'
    $ConfigPathEsc = $DesktopConfig -replace '\\', '\\'

    $PyScript = @"
import json
config_path = r'$DesktopConfig'
python_cmd  = r'$PythonAbs'
server_arg  = r'$ServerPath'
try:
    with open(config_path) as f:
        config = json.load(f)
except Exception:
    config = {}
config.setdefault('mcpServers', {})
config['mcpServers']['figma-write'] = {
    'command': python_cmd,
    'args': [server_arg]
}
with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
print('  Written to: ' + config_path)
print('  Restart Claude Desktop to apply.')
"@
    & $PythonVenv -c $PyScript
    $Registered = $true
}

if (-not $Registered) {
    Write-Host "Neither Claude Code nor Claude Desktop was detected automatically."
    Write-Host ""
    Write-Host "Add this to your MCP config manually:"
    Write-Host ""
    Write-Host "  Claude Desktop — edit %APPDATA%\Claude\claude_desktop_config.json:"
    Write-Host "  {"
    Write-Host "    `"mcpServers`": {"
    Write-Host "      `"figma-write`": {"
    Write-Host "        `"command`": `"$PythonAbs`","
    Write-Host "        `"args`": [`"$ServerPath`"]"
    Write-Host "      }"
    Write-Host "    }"
    Write-Host "  }"
    Write-Host ""
    Write-Host "  Claude Code — run in PowerShell:"
    Write-Host "    claude mcp add figma-write -- `"$PythonAbs`" `"$ServerPath`""
}

# ── Step 6: Plugin setup instructions ────────────────────────────────────
$ManifestPath = Join-Path $ScriptDir "plugin\manifest.json"
Write-Host ""
Write-Host "=== Import the Figma plugin (one time) ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Open the Figma desktop app and open any design file."
Write-Host "  2. Go to: Plugins -> Development -> Import plugin from manifest..."
Write-Host "  3. Select:  $ManifestPath"
Write-Host "  4. The plugin will appear as 'Claude Bridge' under Plugins -> Development."
Write-Host ""
Write-Host "=== Starting each session ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Run: Plugins -> Development -> Claude Bridge"
Write-Host "  Wait for the green dot and 'Connected to Claude'."
Write-Host "  Keep the small window open while you work."
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to close"
