#!/usr/bin/env python3
# Unofficial Figma write MCP (Python port of server.js).
# The MCP client talks to this process over stdio. This process also hosts a
# WebSocket server on localhost, which the Figma plugin (running in the desktop
# app) connects to. Tool calls are forwarded to the plugin, which executes them
# with the Figma Plugin API and sends the result back.

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import anyio

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.connection import State
try:
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
from mcp.types import ImageContent, TextContent
from pydantic import Field

PORT = int(os.environ.get("FIGMA_MCP_PORT", "3055"))
TIMEOUT_S = 30.0
BATCH_TIMEOUT_S = 180.0
EXPORT_MAX_BASE64_BYTES = 5 * 1024 * 1024

# ---------- Security ----------

# Origins the Figma desktop app may send. The first connection logs the actual
# value so users can tune this if needed. Set FIGMA_MCP_ALLOW_ALL_ORIGINS=1 to
# disable the check (not recommended; documents the bypass explicitly).
_FIGMA_ORIGINS = {
    "https://www.figma.com",
    "https://figma.com",
    "null",           # some desktop-app webviews send the literal string "null"
}
_ALLOW_ALL_ORIGINS = os.environ.get("FIGMA_MCP_ALLOW_ALL_ORIGINS", "0").strip() == "1"


def log(*a: Any) -> None:
    # stdout is reserved for MCP stdio transport; log to stderr.
    print("[figma-mcp]", *a, file=sys.stderr, flush=True)


# ---------- WebSocket bridge ----------

_SERVER_START: float = time.time()
_default_file_key: Optional[str] = None  # set by select_file(); cleared when that plugin disconnects


@dataclass
class PluginConn:
    ws: ServerConnection
    fileKey: str
    fileName: str
    pageName: str
    pageId: str
    connectedAt: float
    lastActive: float = field(default=0.0)
    pending_ids: set = field(default_factory=set)  # msg_ids of in-flight futures routed to this conn


plugins: dict[str, PluginConn] = {}      # fileKey -> PluginConn
pending: dict[str, asyncio.Future] = {}  # msg_id -> Future (UUID4, globally unique)


def _resolve_plugin(fileKey: Optional[str] = None) -> PluginConn:
    global _default_file_key
    # 1. Explicit per-call fileKey wins.
    target = fileKey or _default_file_key
    if target is not None:
        conn = plugins.get(target)
        if conn is None:
            connected = list(plugins.keys())
            raise RuntimeError(
                f"No plugin connected for fileKey={target}. "
                f"Connected: {connected}. Use list_files() to see available files."
            )
        return conn
    # 2. Single-plugin back-compat.
    if len(plugins) == 1:
        return next(iter(plugins.values()))
    # 3. No plugins at all.
    if len(plugins) == 0:
        raise RuntimeError(
            "Figma plugin is not connected. Open a file in the Figma desktop app "
            "and run Plugins → Development → Claude Bridge."
        )
    # 4. Multiple plugins, no target — require explicit routing.
    connected = list(plugins.keys())
    raise RuntimeError(
        f"Multiple plugins connected; pass fileKey to target a specific file. "
        f"Connected: {connected}. Use list_files() to see available files."
    )


async def _handle_plugin(ws: ServerConnection) -> None:
    global _default_file_key

    # ------ origin check (DNS-rebinding defence) ------
    origin = (ws.request.headers.get("Origin") or "").strip() or None
    if not _ALLOW_ALL_ORIGINS:
        origin_str = origin or "null"
        if origin_str not in _FIGMA_ORIGINS:
            log(f"WebSocket connection rejected: unknown Origin={origin_str!r}. "
                f"If this is your Figma plugin, add the origin to _FIGMA_ORIGINS or set "
                f"FIGMA_MCP_ALLOW_ALL_ORIGINS=1.")
            try:
                await ws.close(1008, "forbidden origin")
            except Exception:
                pass
            return
    else:
        log(f"Origin check bypassed (FIGMA_MCP_ALLOW_ALL_ORIGINS=1); Origin={origin!r}")

    # ------ expect identify frame first ------
    conn: Optional[PluginConn] = None
    try:
        raw_first = await asyncio.wait_for(ws.__aiter__().__anext__(), timeout=10.0)
    except (asyncio.TimeoutError, StopAsyncIteration):
        log("plugin connected but sent no identify frame within 10 s — closing (old plugin build?)")
        try:
            await ws.close(1002, "expected identify frame")
        except Exception:
            pass
        return
    try:
        first = json.loads(raw_first)
    except Exception:
        log("plugin sent non-JSON as first frame — closing")
        try:
            await ws.close(1002, "expected JSON identify frame")
        except Exception:
            pass
        return
    if first.get("type") != "identify":
        log(f"plugin first frame type={first.get('type')!r} — expected 'identify'; closing (reload the plugin)")
        try:
            await ws.close(1002, "expected identify frame")
        except Exception:
            pass
        return
    payload = first.get("payload") or {}
    file_key = payload.get("fileKey") or ""
    if not file_key:
        log("identify frame missing fileKey — closing")
        try:
            await ws.close(1002, "identify payload missing fileKey")
        except Exception:
            pass
        return

    # ------ eviction protocol (ping-before-replace) ------
    existing = plugins.get(file_key)
    if existing is not None:
        alive = False
        try:
            await asyncio.wait_for(existing.ws.ping(), timeout=2.0)
            alive = True
        except Exception:
            pass
        if alive:
            log(f"rejecting duplicate connection for fileKey={file_key}: existing plugin is still alive")
            try:
                await ws.close(1008, "file already connected to a live plugin instance")
            except Exception:
                pass
            return
        log(f"replacing stale plugin connection for fileKey={file_key} (fileName={existing.fileName})")
        for mid in list(existing.pending_ids):
            fut = pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_exception(RuntimeError(f"plugin replaced by newer connection for fileKey={file_key}"))
        existing.pending_ids.clear()
        try:
            await existing.ws.close()
        except Exception:
            pass

    conn = PluginConn(
        ws=ws,
        fileKey=file_key,
        fileName=payload.get("fileName") or "",
        pageName=payload.get("pageName") or "",
        pageId=payload.get("pageId") or "",
        connectedAt=time.time(),
    )
    plugins[file_key] = conn
    log(f"Figma plugin connected: fileKey={file_key} fileName={conn.fileName!r} page={conn.pageName!r}")

    # ------ normal message loop ------
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            # Re-identify on page change
            if msg.get("type") == "identify":
                p = msg.get("payload") or {}
                if conn.fileKey == (p.get("fileKey") or ""):
                    conn.pageName = p.get("pageName") or conn.pageName
                    conn.pageId = p.get("pageId") or conn.pageId
                    log(f"plugin re-identified page: fileKey={conn.fileKey} page={conn.pageName!r}")
                continue
            fut = pending.pop(msg.get("id"), None)
            if fut is None or fut.done():
                continue
            if "error" in msg and msg["error"]:
                fut.set_exception(RuntimeError(msg["error"]))
            else:
                fut.set_result(msg.get("result"))
    except websockets.ConnectionClosed:
        pass
    finally:
        if plugins.get(file_key) is conn:
            plugins.pop(file_key, None)
            if _default_file_key == file_key:
                _default_file_key = None
        # Fail any futures that were waiting on this connection.
        for mid in list(conn.pending_ids):
            fut = pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_exception(RuntimeError(
                    f"PLUGIN_DISCONNECTED: plugin for fileKey={file_key} disconnected while command was in flight"
                ))
        conn.pending_ids.clear()
        log(f"Figma plugin disconnected: fileKey={file_key} fileName={conn.fileName!r}")


async def send(command: str, params: Optional[dict] = None, timeout: float = TIMEOUT_S,
               fileKey: Optional[str] = None) -> Any:
    conn = _resolve_plugin(fileKey)
    msg_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    pending[msg_id] = fut
    conn.pending_ids.add(msg_id)
    try:
        await conn.ws.send(json.dumps({"id": msg_id, "command": command, "params": params or {}}))
        conn.lastActive = time.time()
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        pending.pop(msg_id, None)
        raise RuntimeError(f"Timed out waiting for Figma ({command})")
    finally:
        pending.pop(msg_id, None)
        conn.pending_ids.discard(msg_id)


# ---------- MCP server ----------
mcp = FastMCP("figma-write")


def _result_text(result: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _error_text(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


# Simple forwarder: calls `send(command, args)` and returns the JSON result.
# Each tool is declared explicitly below so the MCP schema matches server.js.
async def _forward(command: str, args: dict, fileKey: Optional[str] = None) -> list[TextContent]:
    try:
        return _result_text(await send(command, args, fileKey=fileKey))
    except Exception as e:
        return _error_text(str(e))


# ---------- Multi-file routing tools (PHASE-13) ----------

FileKeyOpt = Annotated[Optional[str], Field(default=None, description="Target file's fileKey. Omit when only one plugin is connected. Use list_files() to discover connected fileKeys.")]


@mcp.tool(description="""List every Figma plugin currently connected to this MCP server.

Returns one entry per open plugin: fileKey (stable per-document GUID used for routing),
fileName (human-readable file name; may be renamed by the user), pageName (currently-active
page in that plugin), and lastActive (seconds since server start when the plugin last sent
or received a message — 0 if the plugin has only identified but no tool has been dispatched yet).

Use before dispatching tools to a specific file when more than one plugin is connected.
In the single-plugin case, returning a one-element list is still useful as a liveness check.""")
async def list_files() -> list[TextContent]:
    now = time.time()
    _ = now  # lastActive is relative to _SERVER_START, not now
    return _result_text([
        {
            "fileKey": p.fileKey,
            "fileName": p.fileName,
            "pageName": p.pageName,
            "lastActive": round(p.lastActive - _SERVER_START, 3) if p.lastActive else 0.0,
        }
        for p in plugins.values()
    ])


@mcp.tool(description="""Set the default target file for all subsequent tool calls in this session.

After calling select_file(fileKey), every tool call that does not pass an explicit fileKey will
route to this file. Persists until the server process exits, the plugin for that file disconnects,
or select_file is called again with a different fileKey.

Pass fileKey=null (or call without arguments) to clear the session default and revert to
single-plugin back-compat (errors if multiple plugins are connected).

Returns {fileKey, fileName, pageName} confirming the selected file.""")
async def select_file(
    fileKey: Annotated[Optional[str], Field(default=None, description="fileKey of the target plugin. Obtain from list_files(). Pass null to clear the session default.")] = None,
) -> list[TextContent]:
    global _default_file_key
    if fileKey is None:
        _default_file_key = None
        return _result_text({"cleared": True, "message": "Session default cleared. Single-plugin back-compat active."})
    conn = plugins.get(fileKey)
    if conn is None:
        connected = list(plugins.keys())
        return _error_text(
            f"No plugin connected for fileKey={fileKey}. "
            f"Connected: {connected}. Use list_files() to see available files."
        )
    _default_file_key = fileKey
    return _result_text({
        "fileKey": conn.fileKey,
        "fileName": conn.fileName,
        "pageName": conn.pageName,
        "message": f"Session default set to fileKey={fileKey}",
    })


@mcp.tool(description="""Return metadata about the currently targeted file without executing a Figma command.

If fileKey is omitted, resolves the target using the same rule as all other tools
(session default > single-plugin back-compat > error).

Returns {fileKey, fileName, pageName, connectedAt, lastActive} from the local registry —
no round-trip to the plugin.""")
async def get_current_file(
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    try:
        conn = _resolve_plugin(fileKey)
    except RuntimeError as e:
        return _error_text(str(e))
    return _result_text({
        "fileKey": conn.fileKey,
        "fileName": conn.fileName,
        "pageName": conn.pageName,
        "connectedAt": round(conn.connectedAt - _SERVER_START, 3),
        "lastActive": round(conn.lastActive - _SERVER_START, 3) if conn.lastActive else 0.0,
    })


# ---------- Field aliases shared across tools ----------
HexColor = Annotated[str, Field(description="Hex color, e.g. #1E1E1E or #FF000080")]
HexColorOpt = Annotated[Optional[str], Field(default=None, description="Hex color, e.g. #1E1E1E or #FF000080")]
ParentId = Annotated[Optional[str], Field(default=None, description="Parent node ID (frame/group). Defaults to current page.")]


@mcp.tool(description="List pages and top-level nodes of the current page.")
async def get_document_info(fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("get_document_info", {}, fileKey=fileKey)


@mcp.tool(description="Get the nodes the user currently has selected.")
async def get_selection(fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("get_selection", {}, fileKey=fileKey)


@mcp.tool(description="""Get details of a node and its children (up to a depth).

Returns every visual property the Figma Plugin API exposes via a structured
`styles` block alongside the legacy flat fields (backwards compatible):

  styles.fills[]         — discriminated union: SOLID | GRADIENT_LINEAR |
                           GRADIENT_RADIAL | GRADIENT_ANGULAR |
                           GRADIENT_DIAMOND | IMAGE. Each entry has type,
                           color/gradientStops/imageHash, gradientTransform,
                           imageTransform, scaleMode, opacity. Invisible
                           paints are filtered out.
  styles.strokes[]       — same paint union as fills.
  styles.strokeWeight    — number or "mixed".
  styles.strokeAlign     — "INSIDE" | "CENTER" | "OUTSIDE".
  styles.strokeCap       — "NONE" | "ROUND" | "SQUARE" | "ARROW_LINES" | etc.
  styles.strokeJoin      — "MITER" | "BEVEL" | "ROUND".
  styles.dashPattern[]   — array of numbers (empty = solid).
  styles.effects[]       — DROP_SHADOW / INNER_SHADOW (color, opacity, offset,
                           radius, spread, blendMode) or LAYER_BLUR /
                           BACKGROUND_BLUR (radius). Invisible effects filtered.
  styles.cornerRadius    — scalar or "mixed".
  styles.cornerRadii     — {topLeft, topRight, bottomRight, bottomLeft} when
                           per-corner values differ.
  styles.cornerSmoothing — squircle factor (0–1), present when > 0.
  styles.autoLayout      — {direction, gap, primaryAxisAlign, counterAxisAlign,
                           primaryAxisSizing, counterAxisSizing, wrap,
                           counterAxisSpacing} when layoutMode != NONE.
  styles.padding         — {top, right, bottom, left} when any side > 0.
  styles.opacity         — node-level opacity (0–1).
  styles.blendMode       — e.g. "NORMAL", "MULTIPLY".
  styles.visible         — boolean.
  styles.rotation        — degrees, present when non-zero.
  styles.clipsContent    — boolean (FRAME/COMPONENT only).
  styles.constraints     — {horizontal, vertical}.
  TEXT extras:
    styles.textDecoration, styles.textAutoResize,
    styles.fontFamily, styles.fontStyle, styles.fontSize,
    styles.fontWeight, styles.lineHeight, styles.letterSpacing,
    styles.textAlignHorizontal, styles.textAlignVertical.
  INSTANCE: mainComponentId (string | null).
  STAR: pointCount, innerRadius (top-level, not in styles).
  VECTOR: vectorPaths[] ({windingRule, data}) when includeGeometry=true.

Parameters:
  lean            — if true, omit the styles block entirely. Use for large
                    tree scans (≥5 nodes) or get_page_frames-style surveys
                    to stay within context budget.
  includeGeometry — if true, include vectorPaths on VECTOR nodes (SVG path
                    strings). Off by default because paths can be large.

Schema reference: scripts/phase08/fixtures/index.cjs""")
async def get_node(
    nodeId: str,
    depth: Annotated[int, Field(ge=0, le=5)] = 1,
    lean: bool = False,
    includeGeometry: bool = False,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("get_node", {"nodeId": nodeId, "depth": depth, "lean": lean, "includeGeometry": includeGeometry}, fileKey=fileKey)


@mcp.tool(description="Create a frame, optionally with auto layout.")
async def create_frame(
    name: Optional[str] = None,
    x: float = 0,
    y: float = 0,
    width: float = 400,
    height: float = 300,
    fill: HexColorOpt = None,
    cornerRadius: Optional[float] = None,
    layoutMode: Optional[Literal["NONE", "HORIZONTAL", "VERTICAL"]] = None,
    itemSpacing: Optional[float] = None,
    padding: Optional[float] = None,
    parentId: ParentId = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args = {"x": x, "y": y, "width": width, "height": height}
    for k, v in [
        ("name", name), ("fill", fill), ("cornerRadius", cornerRadius),
        ("layoutMode", layoutMode), ("itemSpacing", itemSpacing),
        ("padding", padding), ("parentId", parentId),
    ]:
        if v is not None:
            args[k] = v
    return await _forward("create_frame", args, fileKey=fileKey)


@mcp.tool(description="Create a rectangle.")
async def create_rectangle(
    name: Optional[str] = None,
    x: float = 0,
    y: float = 0,
    width: float = 100,
    height: float = 100,
    fill: HexColorOpt = None,
    cornerRadius: Optional[float] = None,
    parentId: ParentId = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args = {"x": x, "y": y, "width": width, "height": height}
    for k, v in [("name", name), ("fill", fill), ("cornerRadius", cornerRadius), ("parentId", parentId)]:
        if v is not None:
            args[k] = v
    return await _forward("create_rectangle", args, fileKey=fileKey)


@mcp.tool(description="Create an ellipse.")
async def create_ellipse(
    name: Optional[str] = None,
    x: float = 0,
    y: float = 0,
    width: float = 100,
    height: float = 100,
    fill: HexColorOpt = None,
    parentId: ParentId = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args = {"x": x, "y": y, "width": width, "height": height}
    for k, v in [("name", name), ("fill", fill), ("parentId", parentId)]:
        if v is not None:
            args[k] = v
    return await _forward("create_ellipse", args, fileKey=fileKey)


@mcp.tool(description="Create a text layer.")
async def create_text(
    text: str,
    x: float = 0,
    y: float = 0,
    fontSize: float = 16,
    fontFamily: str = "Inter",
    fontStyle: Annotated[str, Field(description="e.g. Regular, Bold, Semi Bold")] = "Regular",
    fill: HexColorOpt = None,
    width: Annotated[Optional[float], Field(default=None, description="Fixed width; text wraps. Omit for auto width.")] = None,
    name: Optional[str] = None,
    parentId: ParentId = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args = {"text": text, "x": x, "y": y, "fontSize": fontSize, "fontFamily": fontFamily, "fontStyle": fontStyle}
    for k, v in [("fill", fill), ("width", width), ("name", name), ("parentId", parentId)]:
        if v is not None:
            args[k] = v
    return await _forward("create_text", args, fileKey=fileKey)


@mcp.tool(description="Replace the characters of an existing text layer.")
async def set_text(nodeId: str, text: str, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("set_text", {"nodeId": nodeId, "text": text}, fileKey=fileKey)


@mcp.tool(description="Set a solid fill color on a node.")
async def set_fill(nodeId: str, color: HexColor, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("set_fill", {"nodeId": nodeId, "color": color}, fileKey=fileKey)


# ---------- PHASE-07 fine-grained setters (section iteration) ----------
# Each setter mutates one visual property. All are safe to call via run_batch
# so critique → batch is a single plugin round-trip. The judge subagent uses
# the extended summarize() readback to verify writes converged.

LayoutMode = Literal["NONE", "HORIZONTAL", "VERTICAL"]
PrimaryAxisAlign = Literal["MIN", "CENTER", "MAX", "SPACE_BETWEEN"]
CounterAxisAlign = Literal["MIN", "CENTER", "MAX", "BASELINE"]
TextAlignH = Literal["LEFT", "CENTER", "RIGHT", "JUSTIFIED"]
TextCaseLit = Literal["ORIGINAL", "UPPER", "LOWER", "TITLE"]
TypoUnit = Literal["PIXELS", "PERCENT", "AUTO"]


@mcp.tool(description="Change the auto-layout mode of an existing frame. Pass mode='NONE' to detach auto-layout (frees x/y). Safe to call on frames with children — Figma will re-flow them. primaryAxisAlign / counterAxisAlign are only applied when mode != NONE.")
async def set_auto_layout(
    nodeId: str,
    mode: LayoutMode,
    primaryAxisAlign: Optional[PrimaryAxisAlign] = None,
    counterAxisAlign: Optional[CounterAxisAlign] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"nodeId": nodeId, "mode": mode}
    if primaryAxisAlign is not None:
        args["primaryAxisAlign"] = primaryAxisAlign
    if counterAxisAlign is not None:
        args["counterAxisAlign"] = counterAxisAlign
    return await _forward("set_auto_layout", args, fileKey=fileKey)


@mcp.tool(description="Set per-side padding on an auto-layout frame. Omitted sides keep their current value. `all` is a shorthand for all four sides; explicit sides override `all` in the same call. If the frame is not auto-layout, it is auto-promoted to VERTICAL with sensible defaults and a warning is returned.")
async def set_padding(
    nodeId: str,
    top: Optional[float] = None,
    right: Optional[float] = None,
    bottom: Optional[float] = None,
    left: Optional[float] = None,
    all: Optional[float] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"nodeId": nodeId}
    for k, v in [("top", top), ("right", right), ("bottom", bottom), ("left", left), ("all", all)]:
        if v is not None:
            args[k] = v
    return await _forward("set_padding", args, fileKey=fileKey)


@mcp.tool(description="Set the gap between children on an auto-layout frame.")
async def set_item_spacing(nodeId: str, itemSpacing: float, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("set_item_spacing", {"nodeId": nodeId, "itemSpacing": itemSpacing}, fileKey=fileKey)


@mcp.tool(description="Set per-corner radius on a shape or frame. `all` is a shorthand for all four corners; explicit corners override `all` in the same call. Omitted corners keep their current value.")
async def set_corner_radius(
    nodeId: str,
    topLeft: Optional[float] = None,
    topRight: Optional[float] = None,
    bottomRight: Optional[float] = None,
    bottomLeft: Optional[float] = None,
    all: Optional[float] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"nodeId": nodeId}
    for k, v in [
        ("topLeft", topLeft), ("topRight", topRight),
        ("bottomRight", bottomRight), ("bottomLeft", bottomLeft),
        ("all", all),
    ]:
        if v is not None:
            args[k] = v
    return await _forward("set_corner_radius", args, fileKey=fileKey)


@mcp.tool(description="Mutate typography on a TEXT node. Omitted fields keep current values. lineHeight / letterSpacing accept either a number (interpreted as PIXELS) or an object {value, unit} where unit is PIXELS or PERCENT; lineHeight also accepts {unit:'AUTO'}. Fonts outside the Phase-01 safe-list (Inter, Roboto, IBM Plex Sans, IBM Plex Mono, JetBrains Mono, Atkinson Hyperlegible) emit a warning but are still attempted.")
async def set_text_style(
    nodeId: str,
    fontFamily: Optional[str] = None,
    fontStyle: Optional[str] = None,
    fontSize: Optional[float] = None,
    lineHeight: Annotated[
        Optional[Any],
        Field(default=None, description="Number (PIXELS) OR {value, unit:'PIXELS'|'PERCENT'} OR {unit:'AUTO'}"),
    ] = None,
    letterSpacing: Annotated[
        Optional[Any],
        Field(default=None, description="Number (PIXELS) OR {value, unit:'PIXELS'|'PERCENT'}"),
    ] = None,
    textAlignHorizontal: Optional[TextAlignH] = None,
    textCase: Optional[TextCaseLit] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"nodeId": nodeId}
    for k, v in [
        ("fontFamily", fontFamily), ("fontStyle", fontStyle), ("fontSize", fontSize),
        ("lineHeight", lineHeight), ("letterSpacing", letterSpacing),
        ("textAlignHorizontal", textAlignHorizontal), ("textCase", textCase),
    ]:
        if v is not None:
            args[k] = v
    return await _forward("set_text_style", args, fileKey=fileKey)


@mcp.tool(description="Return the top-level frames of the current page along with the page's bounding box. Used by the PHASE-07 reference-image slicer to map section bboxes proportionally into the reference image. Shape: {pageId, pageName, bbox, frames: [{nodeId, name, type, x, y, width, height}]}.")
async def get_page_frames(fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("get_page_frames", {}, fileKey=fileKey)


# ---------- read extensions (PHASE-11) ----------

@mcp.tool(description="Return all local variable collections with modes and per-mode resolved values. Shape: {collections: [{id, name, modes: [{modeId, name}], variables: [{id, name, resolvedType, valuesByMode: {modeId: value}}]}]}. Returns {collections: [], note: '...'} on FigJam or files with no variable API. approxTokens is NOT included — use get_design_context for budget estimates.")
async def get_variable_defs(fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("get_variable_defs", {}, fileKey=fileKey)


@mcp.tool(description="Return all local paint, text, effect, and grid styles in four separate lists. Shape: {paints: [{id, name, paints}], text: [{id, name, fontSize, fontName, textDecoration, lineHeight, letterSpacing}], effects: [{id, name, effects}], grids: [{id, name, layoutGrids}]}. Each category is fetched independently so one failing group (e.g. grids on FigJam) does not break the others.")
async def get_styles(fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("get_styles", {}, fileKey=fileKey)


@mcp.tool(description="Return a depth- and count-capped flat list of nodes rooted at rootId with geometry only (no style data). Hard caps: depth 100, maxNodes default 2000. Reports truncated:true when either cap is hit. Shape: {schemaVersion:1, rootId, truncated, nodes: [{id, parentId, name, type, visible, localSize, absoluteTransform, absoluteBoundingBox, absoluteRenderBounds, clipsContent}]}. Cheap alternative to get_node for large-page layout scans.")
async def get_layout_tree(
    rootId: Annotated[str, Field(description="nodeId of the root scene node (frame, group, or component). Document and page nodes are rejected.")],
    maxNodes: Annotated[int, Field(ge=1, le=50000, description="Stop collecting after this many nodes and set truncated:true.")] = 2000,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("get_layout_tree", {"rootId": rootId, "maxNodes": maxNodes}, fileKey=fileKey)


@mcp.tool(description="Return a depth-truncated design context tree plus a meta envelope {approxTokens, truncatedAtDepth, droppedChildren}. Reuses the PHASE-08 summarize() serializer in lean mode (name+type+bbox+childCount, no styles). Targets the current selection; falls back to the current page if nothing is selected; or pass nodeId to target a specific node. approxTokens uses a char-count/4 heuristic (consistent with scripts/phase10/budget.py) — useful for budget planning, not billing. depth=1 gives a one-level-deep summary; depth=3 gives a three-level tree. Compare approxTokens across depths before deciding how deep to go.")
async def get_design_context(
    depth: Annotated[int, Field(ge=0, le=10, description="How many levels deep to recurse. 0 = root node only. Default 2.")] = 2,
    nodeId: Annotated[str | None, Field(description="Optional nodeId to target. If omitted, uses current selection or current page.")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    params: dict = {"depth": depth}
    if nodeId is not None:
        params["nodeId"] = nodeId
    return await _forward("get_design_context", params, fileKey=fileKey)


ScaleMode = Literal["FILL", "FIT", "CROP", "TILE"]
IMAGE_FILL_MAX_FILE_BYTES = 10 * 1024 * 1024
IMAGE_FILL_MAX_PIXEL_DIM = 4096
IMAGE_FILL_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_FILL_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _parse_image_dims(raw: bytes, ext: str) -> "tuple[int, int]":
    """Parse image header dimensions without full decode. Returns (0, 0) on soft-fail."""
    import struct
    try:
        if ext == ".png":
            if raw[:8] == b'\x89PNG\r\n\x1a\n' and len(raw) >= 24:
                w, h = struct.unpack('>II', raw[16:24])
                return w, h
        elif ext in (".jpg", ".jpeg"):
            i = 2
            while i < len(raw) - 3:
                if raw[i] != 0xFF:
                    break
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    if i + 9 <= len(raw):
                        h, w = struct.unpack('>HH', raw[i + 5:i + 9])
                        return w, h
                    break
                seg_len = struct.unpack('>H', raw[i + 2:i + 4])[0]
                i += 2 + seg_len
        elif ext == ".webp":
            if raw[8:12] == b'WEBP' and len(raw) >= 16:
                fmt = raw[12:16]
                if fmt == b'VP8X' and len(raw) >= 30:
                    cw = struct.unpack('<I', raw[24:27] + b'\x00')[0] + 1
                    ch = struct.unpack('<I', raw[27:30] + b'\x00')[0] + 1
                    return cw, ch
                # VP8L and VP8 (plain): soft-fail
    except Exception:
        pass
    return 0, 0


def _read_image_bytes(path: str) -> "tuple[str, str, int, int] | list[TextContent]":
    """Read a local image file and return (bytes_b64, mimeType, width, height), or an error list."""
    import base64 as b64mod
    path = os.path.realpath(path)  # resolve symlinks before any checks
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_FILL_ALLOWED_EXT:
        allowed = ", ".join(sorted(IMAGE_FILL_ALLOWED_EXT))
        return _error_text(f"unsupported extension {ext or '(none)'} (allowed: {allowed})")
    if not os.path.exists(path):
        return _error_text(f"no file at {path}")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return _error_text(f"cannot stat {path}: {e}")
    if size > IMAGE_FILL_MAX_FILE_BYTES:
        mb = size / (1024 * 1024)
        return _error_text(f"image too large ({mb:.1f} MB). Max is 10 MB.")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return _error_text(f"cannot read {path}: {e}")
    w, h = _parse_image_dims(raw, ext)
    if w > 0 and h > 0 and (w > IMAGE_FILL_MAX_PIXEL_DIM or h > IMAGE_FILL_MAX_PIXEL_DIM):
        return _error_text(
            f"image too tall/wide for Figma ({w}×{h}, Figma caps createImage around "
            f"{IMAGE_FILL_MAX_PIXEL_DIM}×{IMAGE_FILL_MAX_PIXEL_DIM}). Try downscaling first."
        )
    bytes_b64 = b64mod.b64encode(raw).decode("ascii")
    mime_type = IMAGE_FILL_EXT_TO_MIME[ext]
    return bytes_b64, mime_type, w, h


@mcp.tool(description="Apply a raw-bytes image as a fill on a node. Bytes are base64-encoded; the plugin decodes them, calls figma.createImage, and assigns an ImagePaint to node.fills. Use set_image_fill if you have a local file path instead.")
async def set_image_fill_bytes(
    nodeId: str,
    bytesBase64: Annotated[str, Field(description="Base64-encoded image bytes (PNG, JPEG, or WEBP).")],
    scaleMode: ScaleMode = "FILL",
    opacity: Annotated[float, Field(ge=0, le=1)] = 1,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward(
        "set_image_fill_bytes",
        {"nodeId": nodeId, "bytesBase64": bytesBase64, "scaleMode": scaleMode, "opacity": opacity},
        fileKey=fileKey,
    )


@mcp.tool(description="Apply a local image file as a fill on a node. Reads the file (max 10 MB, extensions .png/.jpg/.jpeg/.webp), base64-encodes, and forwards to the plugin.")
async def set_image_fill(
    nodeId: str,
    path: Annotated[str, Field(description="Local filesystem path to a PNG/JPEG/WEBP. Resolved relative to the MCP server CWD.")],
    scaleMode: ScaleMode = "FILL",
    opacity: Annotated[float, Field(ge=0, le=1)] = 1,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    result = _read_image_bytes(path)
    if not isinstance(result, tuple):
        return result  # error TextContent list
    bytes_b64, mime_type, _w, _h = result
    try:
        r = await send(
            "set_image_fill_bytes",
            {
                "nodeId": nodeId,
                "bytesBase64": bytes_b64,
                "scaleMode": scaleMode,
                "opacity": opacity,
                "mimeType": mime_type,
            },
            fileKey=fileKey,
        )
        return _result_text(r)
    except Exception as e:
        return _error_text(str(e))


@mcp.tool(description="Set a solid stroke on a node.")
async def set_stroke(nodeId: str, color: HexColor, weight: float = 1, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("set_stroke", {"nodeId": nodeId, "color": color, "weight": weight}, fileKey=fileKey)


@mcp.tool(description="Move a node to x/y.")
async def move_node(nodeId: str, x: float, y: float, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("move_node", {"nodeId": nodeId, "x": x, "y": y}, fileKey=fileKey)


@mcp.tool(description="Resize a node.")
async def resize_node(nodeId: str, width: float, height: float, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("resize_node", {"nodeId": nodeId, "width": width, "height": height}, fileKey=fileKey)


@mcp.tool(description="Rename a node.")
async def rename_node(nodeId: str, name: str, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("rename_node", {"nodeId": nodeId, "name": name}, fileKey=fileKey)


@mcp.tool(description="Delete a node.")
async def delete_node(nodeId: str, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("delete_node", {"nodeId": nodeId}, fileKey=fileKey)


@mcp.tool(description="Duplicate a node, optionally moving the copy.")
async def clone_node(nodeId: str, x: Optional[float] = None, y: Optional[float] = None, fileKey: FileKeyOpt = None) -> list[TextContent]:
    args: dict[str, Any] = {"nodeId": nodeId}
    if x is not None:
        args["x"] = x
    if y is not None:
        args["y"] = y
    return await _forward("clone_node", args, fileKey=fileKey)


# ---------- PHASE-12: org tools ----------

@mcp.tool(description="""Move one or more nodes to a new parent. Supports cross-page moves.

For cross-page moves the plugin calls figma.loadAllPagesAsync() so nodes on
non-current pages are reachable by ID. If newParentId is a PAGE node, nodes
become top-level on that page.

Parameters:
  nodeIds      — list of node IDs to move.
  newParentId  — ID of the destination frame, group, or page node. Omit for current page.
  index        — optional insertion index among the parent's children. Omit to append.

Return: {reparented: [{id, oldParentId, newParentId, oldPageId, newPageId}], failed: [{id, error}]}.
Callers must check failed[] — partial success semantics.""")
async def reparent_nodes(
    nodeIds: Annotated[list[str], Field(min_length=1, description="IDs of nodes to reparent.")],
    newParentId: Annotated[Optional[str], Field(default=None, description="Destination parent node ID. Omit for current page.")] = None,
    index: Annotated[Optional[int], Field(default=None, description="Insertion index among parent children. Omit to append.")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"nodeIds": nodeIds}
    if newParentId is not None:
        args["newParentId"] = newParentId
    if index is not None:
        args["index"] = index
    return await _forward("reparent_nodes", args, fileKey=fileKey)


@mcp.tool(description="""Wrap N existing nodes in a new Figma GROUP.

All nodes must share a parent — Figma's figma.group() requires it.
The group is placed as a sibling of the first node, inheriting its parent.

Parameters:
  nodeIds — list of node IDs to group. Must share a parent.
  name    — optional name for the new GROUP node.

Return: summarize(group) — same shape as other creator tools.""")
async def group_nodes(
    nodeIds: Annotated[list[str], Field(min_length=2, description="IDs of nodes to group. Figma requires ≥2 nodes. All must share a parent.")],
    name: Annotated[Optional[str], Field(default=None, description="Name for the new GROUP node.")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"nodeIds": nodeIds}
    if name is not None:
        args["name"] = name
    return await _forward("group_nodes", args, fileKey=fileKey)


@mcp.tool(description="""Ungroup a GROUP node, returning its children to the parent.

Only GROUP nodes are accepted — FRAME, COMPONENT, etc. return an error.
Uses figma.ungroup() which removes the group wrapper and re-parents children.

Parameters:
  nodeId — ID of the GROUP node to dissolve.

Return: {ungrouped: [childId, ...], parentId}.""")
async def ungroup_node(
    nodeId: Annotated[str, Field(description="ID of the GROUP node to dissolve.")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("ungroup_node", {"nodeId": nodeId}, fileKey=fileKey)


@mcp.tool(description="""Resize a FRAME or SECTION to tightly wrap its children's bounding box.

For non-auto-layout FRAMEs: computes min/max x+y across children.absoluteBoundingBox,
calls node.resize(w, h), and shifts each child by the delta so content stays
at the same canvas position. For SECTIONs: uses resizeWithoutConstraints.
Auto-layout FRAMEs: no-op with a warning (Figma already fits them; running
resize on them would be destructive to layout hug/fill).

Parameters:
  nodeId — ID of the FRAME or SECTION to fit.

Return: summarize(node) with optional warning field.""")
async def fit_to_content(
    nodeId: Annotated[str, Field(description="ID of the FRAME or SECTION to fit to its children.")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("fit_to_content", {"nodeId": nodeId}, fileKey=fileKey)


@mcp.tool(description="""Distribute 3+ nodes with equal horizontal spacing.

Sorts nodes by x, locks the leftmost and rightmost, and redistributes the
middle nodes so gaps between consecutive x-sorted nodes are equal. The total
span (from left edge of first to right edge of last) is preserved.

Parameters:
  nodeIds — list of ≥3 node IDs to distribute. Must have ≥3 items.

Return: {distributed: [{id, x, y}]} — positions after redistribution.""")
async def distribute_horizontally(
    nodeIds: Annotated[list[str], Field(min_length=3, description="List of ≥3 node IDs to distribute horizontally.")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("distribute_horizontally", {"nodeIds": nodeIds}, fileKey=fileKey)


@mcp.tool(description="""Distribute 3+ nodes with equal vertical spacing.

Sorts nodes by y, locks the topmost and bottommost, and redistributes the
middle nodes so gaps between consecutive y-sorted nodes are equal. The total
span (from top edge of first to bottom edge of last) is preserved.

Parameters:
  nodeIds — list of ≥3 node IDs to distribute. Must have ≥3 items.

Return: {distributed: [{id, x, y}]} — positions after redistribution.""")
async def distribute_vertically(
    nodeIds: Annotated[list[str], Field(min_length=3, description="List of ≥3 node IDs to distribute vertically.")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("distribute_vertically", {"nodeIds": nodeIds}, fileKey=fileKey)


@mcp.tool(description="""Snap nodes to the nearest grid multiple.

Sets each node's x and y to the nearest integer multiple of `grid` (default 8,
matching Phase-10 baseline spacing unit). Example: x=13, grid=8 → x=16.

Parameters:
  nodeIds — list of node IDs to snap.
  grid    — grid size in pixels (default 8).

Return: {aligned: [{id, x, y}]} — snapped positions.""")
async def align_to_grid(
    nodeIds: Annotated[list[str], Field(min_length=1, description="List of node IDs to snap to the grid.")],
    grid: Annotated[int, Field(ge=1, description="Grid size in pixels. Default 8.")] = 8,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("align_to_grid", {"nodeIds": nodeIds, "grid": grid}, fileKey=fileKey)


@mcp.tool(description="""Delete multiple nodes in one call. Batch version of delete_node.

Partial-success semantics: each failed ID is captured in failed[] rather than
aborting the whole batch. Callers must inspect failed[] for per-node errors.
No confirm:true gate — the driving Claude agent is the gate.

Parameters:
  nodeIds — list of node IDs to delete.

Return: {deleted: [{id, name, type}], failed: [{id, error}]}.""")
async def delete_nodes(
    nodeIds: Annotated[list[str], Field(min_length=1, description="IDs of nodes to delete.")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("delete_nodes", {"nodeIds": nodeIds}, fileKey=fileKey)


@mcp.tool(description="""Clone multiple nodes in one call. Batch version of clone_node.

Each clone is placed next to its source with the shared offset applied. Partial-
success semantics: each failed ID is captured in failed[]. Callers must inspect
failed[] for per-node errors.

Parameters:
  nodeIds — list of node IDs to clone.
  offsetX — x offset applied to each clone (default 0).
  offsetY — y offset applied to each clone (default 0).

Return: {cloned: [summarize(c), ...], failed: [{id, error}]}.""")
async def clone_nodes(
    nodeIds: Annotated[list[str], Field(min_length=1, description="IDs of nodes to clone.")],
    offsetX: float = 0,
    offsetY: float = 0,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("clone_nodes", {"nodeIds": nodeIds, "offsetX": offsetX, "offsetY": offsetY}, fileKey=fileKey)


@mcp.tool(description="""Set the current page's selection without zooming the viewport.

Non-destructive mirror of select_and_zoom's first line only — the viewport
position and zoom level are NOT changed. Useful in batch workflows that prep a
selection for a downstream fit_to_content or export_frame without disorienting
the designer watching the plugin work.

Parameters:
  nodeIds — list of node IDs to select. All must be on the current page.

Constraint: all nodeIds must live on the current page. Figma's
figma.currentPage.selection = [...] assignment raises if any node belongs to
another page. Use set_page first (or set_selection on the destination page
after a reparent_nodes move) to switch context before selecting.

Return: {selected: [id, ...]}.""")
async def set_selection(
    nodeIds: Annotated[list[str], Field(description="IDs of nodes to select on the current page.")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("set_selection", {"nodeIds": nodeIds}, fileKey=fileKey)


@mcp.tool(description="""Return the current page's viewport state without side effects.

Useful for verifying that tools which touch selection or exports do NOT move
the viewport. Pair with set_selection + fit_to_content + export_frame to catch
future regressions in viewport-polluting handlers.

Return: {center: {x, y}, zoom, bounds: {x, y, width, height}}.""")
async def get_viewport(fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("get_viewport", {}, fileKey=fileKey)


@mcp.tool(description="""Create a new Figma page without switching to it.

Composable with set_page: create the page first, then navigate to it when
ready. Does not alter the current page.

Parameters:
  name — name for the new page.

Return: {id, name}.""")
async def create_page(
    name: Annotated[str, Field(description="Name for the new page.")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("create_page", {"name": name}, fileKey=fileKey)


@mcp.tool(description="""Create an image-filled rectangle in one call. Fuses create_rectangle + set_image_fill.

Reads the local file (same path/size/ext checks as set_image_fill: max 10 MB,
.png/.jpg/.jpeg/.webp), base64-encodes it, and sends a single plugin command
that creates a RECTANGLE and applies the image as a fill.

Width/height default to the image's native dimensions when readable from the
header (PNG, JPEG, WEBP VP8X), else 100×100. Pass explicit values to override.

Parameters:
  path      — local filesystem path to a PNG/JPEG/WEBP.
  x         — canvas x position (default 0).
  y         — canvas y position (default 0).
  parentId  — parent frame/group ID. Defaults to current page.
  width     — rectangle width in pixels. Defaults to native image width.
  height    — rectangle height in pixels. Defaults to native image height.
  name      — node name. Defaults to the file basename without extension.
  scaleMode — image fill scale mode: FILL | FIT | CROP | TILE (default FILL).
  opacity   — fill opacity 0–1 (default 1).

Return: summarize(rectangle) — same shape as create_rectangle.""")
async def create_image(
    path: Annotated[str, Field(description="Local filesystem path to a PNG/JPEG/WEBP image.")],
    x: float = 0,
    y: float = 0,
    parentId: ParentId = None,
    width: Annotated[Optional[float], Field(default=None, description="Rectangle width in pixels. Defaults to native image width.")] = None,
    height: Annotated[Optional[float], Field(default=None, description="Rectangle height in pixels. Defaults to native image height.")] = None,
    name: Annotated[Optional[str], Field(default=None, description="Node name. Defaults to the file basename without extension.")] = None,
    scaleMode: ScaleMode = "FILL",
    opacity: Annotated[float, Field(ge=0, le=1)] = 1,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    result = _read_image_bytes(path)
    if not isinstance(result, tuple):
        return result  # error TextContent list
    bytes_b64, _mime, real_w, real_h = result
    args: dict[str, Any] = {
        "bytesBase64": bytes_b64,
        "x": x,
        "y": y,
        "scaleMode": scaleMode,
        "opacity": opacity,
    }
    if name is not None:
        args["name"] = name
    else:
        args["name"] = os.path.splitext(os.path.basename(path))[0]
    if parentId is not None:
        args["parentId"] = parentId
    if width is not None:
        args["width"] = width
    elif real_w > 0:
        args["width"] = real_w
    if height is not None:
        args["height"] = height
    elif real_h > 0:
        args["height"] = real_h
    return await _forward("create_image", args, fileKey=fileKey)


@mcp.tool(description="Select nodes in the editor and zoom the viewport to them.")
async def select_and_zoom(nodeIds: list[str], fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("select_and_zoom", {"nodeIds": nodeIds}, fileKey=fileKey)


@mcp.tool(description="Switch the active page by name or id.")
async def set_page(
    pageId: Annotated[Optional[str], Field(default=None, description="Page node ID, e.g. '0:1'")] = None,
    pageName: Annotated[Optional[str], Field(default=None, description="Exact page name, e.g. 'Components'")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {}
    if pageId is not None:
        args["pageId"] = pageId
    if pageName is not None:
        args["pageName"] = pageName
    return await _forward("set_page", args, fileKey=fileKey)


@mcp.tool(description="Convert an existing FRAME node into a reusable Figma component (in-place).")
async def create_component(
    nodeId: Annotated[str, Field(description="ID of the FRAME to convert")],
    name: Annotated[Optional[str], Field(default=None, description="Component name, e.g. 'Button/Default'")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"nodeId": nodeId}
    if name is not None:
        args["name"] = name
    return await _forward("create_component", args, fileKey=fileKey)


@mcp.tool(description="Create an instance of a component and place it on the canvas. Optional textOverrides patches nested TEXT descendants by zero-based child-index path (e.g. path:[0,1] = second child of first child), applied after instantiation so per-instance text differs from the component template without extra round-trips.")
async def create_instance(
    componentId: Annotated[str, Field(description="ID of the COMPONENT node to instantiate")],
    x: float = 0,
    y: float = 0,
    parentId: Annotated[Optional[str], Field(default=None, description="Parent frame/group ID. Defaults to current page.")] = None,
    name: Annotated[Optional[str], Field(default=None, description="Override name for this instance")] = None,
    textOverrides: Annotated[
        Optional[list[dict]],
        Field(default=None, description="Per-instance text overrides on nested TEXT descendants. Each item: {path: number[], text: string}."),
    ] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"componentId": componentId, "x": x, "y": y}
    for k, v in [("parentId", parentId), ("name", name), ("textOverrides", textOverrides)]:
        if v is not None:
            args[k] = v
    return await _forward("create_instance", args, fileKey=fileKey)


@mcp.tool(description="List all components on the current page (or all pages). Set includeSets=true to also return component sets with their variants (used by sync-manifest.js to detect the file's variantNaming convention).")
async def get_components(allPages: bool = False, includeSets: bool = False, fileKey: FileKeyOpt = None) -> list[TextContent]:
    return await _forward("get_components", {"allPages": allPages, "includeSets": includeSets}, fileKey=fileKey)


@mcp.tool(description="Combine existing COMPONENT nodes into a Figma variant set (ComponentSetNode). All components must be on the same page. After combination, child variants are repositioned horizontally with 24px spacing so the set is readable. Returns the set id and its child ids.")
async def create_component_set(
    componentIds: Annotated[list[str], Field(min_length=1, description="IDs of existing COMPONENT nodes to combine. Pre-existing instances of these components may detach — only combine components that have no instances yet.")],
    name: Annotated[str, Field(description="Name for the resulting component set, e.g. 'Button'.")],
    parentId: Annotated[Optional[str], Field(default=None, description="Parent frame/page ID. Defaults to current page.")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"componentIds": componentIds, "name": name}
    if parentId is not None:
        args["parentId"] = parentId
    return await _forward("create_component_set", args, fileKey=fileKey)


@mcp.tool(description="Set VARIANT-type properties on an instance. Reads componentPropertyDefinitions from the parent ComponentSet and validates each key + value before calling setProperties. Does NOT support TEXT / BOOLEAN / INSTANCE_SWAP properties.")
async def set_variant_property(
    instanceId: Annotated[str, Field(description="ID of an INSTANCE node whose main component is inside a ComponentSet.")],
    properties: Annotated[dict[str, str], Field(description="Property-name → property-value map. All values are variant-option strings (e.g. { Size: 'Small', State: 'Default' }).")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    return await _forward("set_variant_property", {"instanceId": instanceId, "properties": properties}, fileKey=fileKey)


# ---------- PHASE-09 write-side setters ----------

StrokeAlignLit = Literal["INSIDE", "CENTER", "OUTSIDE"]
GradientTypeLit = Literal["LINEAR", "RADIAL", "ANGULAR", "DIAMOND"]
EffectTargetLit = Literal["fill", "stroke"]


@mcp.tool(description="""Show or hide a node.

Sets node.visible to true or false. Readable back via get_node → styles.visible.

Parameters:
  nodeId   — single node ID, or use nodeIds for multiple.
  nodeIds  — list of node IDs; returns results[] array.
  visible  — true to show, false to hide.

Supports _ref:<alias> substitution on nodeId/nodeIds.""")
async def set_node_visibility(
    visible: bool,
    nodeId: Annotated[Optional[str], Field(default=None, description="Node ID. Use nodeIds for multiple nodes.")] = None,
    nodeIds: Annotated[Optional[list[str]], Field(default=None, description="List of node IDs. Returns results[] array.")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"visible": visible}
    if nodeId is not None:
        args["nodeId"] = nodeId
    if nodeIds is not None:
        args["nodeIds"] = nodeIds
    return await _forward("set_node_visibility", args, fileKey=fileKey)


@mcp.tool(description="""Set advanced stroke properties on a node.

Extends the 2-param set_stroke (color + weight only) with strokeAlign,
dashPattern, strokeCap, and strokeJoin. All fields are optional — omitted
fields keep the node's current value.

Parameters:
  nodeId       — single node ID, or use nodeIds for multiple.
  nodeIds      — list of node IDs; returns results[] array.
  color        — hex fill for the stroke paint, e.g. "#FF0000".
  weight       — stroke width in pixels.
  strokeAlign  — "INSIDE" | "CENTER" | "OUTSIDE".
  dashPattern  — array of numbers [dash, gap, dash, gap, ...]. Empty array = solid.
  strokeCap    — "NONE" | "ROUND" | "SQUARE" | "ARROW_LINES" | "ARROW_EQUILATERAL".
  strokeJoin   — "MITER" | "BEVEL" | "ROUND".

set_stroke(nodeId, color, weight) remains unchanged as a 2-param alias.
Supports _ref:<alias> substitution on nodeId/nodeIds.""")
async def set_stroke_properties(
    nodeId: Annotated[Optional[str], Field(default=None, description="Node ID. Use nodeIds for multiple nodes.")] = None,
    nodeIds: Annotated[Optional[list[str]], Field(default=None, description="List of node IDs. Returns results[] array.")] = None,
    color: HexColorOpt = None,
    weight: Annotated[Optional[float], Field(default=None, description="Stroke width in pixels.")] = None,
    strokeAlign: Annotated[Optional[StrokeAlignLit], Field(default=None)] = None,
    dashPattern: Annotated[Optional[list[float]], Field(default=None, description="[dash, gap, ...]. Empty list = solid.")] = None,
    strokeCap: Annotated[Optional[str], Field(default=None, description="NONE | ROUND | SQUARE | ARROW_LINES | ARROW_EQUILATERAL")] = None,
    strokeJoin: Annotated[Optional[str], Field(default=None, description="MITER | BEVEL | ROUND")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {}
    for k, v in [
        ("nodeId", nodeId), ("nodeIds", nodeIds), ("color", color), ("weight", weight),
        ("strokeAlign", strokeAlign), ("dashPattern", dashPattern),
        ("strokeCap", strokeCap), ("strokeJoin", strokeJoin),
    ]:
        if v is not None:
            args[k] = v
    return await _forward("set_stroke_properties", args, fileKey=fileKey)


@mcp.tool(description="""Apply a gradient fill (or stroke) to a node.

Replaces fills[] (or strokes[]) with a single gradient paint. Readable back
via get_node → styles.fills[0].type === "GRADIENT_LINEAR" (etc.).

Parameters:
  nodeId          — single node ID, or use nodeIds for multiple.
  nodeIds         — list of node IDs; returns results[] array.
  gradientType    — "LINEAR" | "RADIAL" | "ANGULAR" | "DIAMOND" (default: "LINEAR").
  gradientStops   — list of ≥2 stops: [{color: "#hex", opacity?: float, position: float}].
                    position is 0.0–1.0 along the gradient axis.
  gradientTransform — 2×3 affine matrix [[a,b,c],[d,e,f]]. Controls the gradient's
                    angle and scale. Default: [[1,0,0],[0,1,0]] (left→right).
                    For a top→bottom gradient use [[0,1,0],[-1,0,1]].
  opacity         — overall paint opacity (0–1). Optional.
  target          — "fill" (default) or "stroke".

Supports _ref:<alias> substitution on nodeId/nodeIds.""")
async def set_gradient_fill(
    gradientStops: Annotated[list[dict], Field(min_length=2, description="List of ≥2 stops: [{color, opacity?, position}].")],
    nodeId: Annotated[Optional[str], Field(default=None, description="Node ID. Use nodeIds for multiple nodes.")] = None,
    nodeIds: Annotated[Optional[list[str]], Field(default=None, description="List of node IDs. Returns results[] array.")] = None,
    gradientType: GradientTypeLit = "LINEAR",
    gradientTransform: Annotated[Optional[list[list[float]]], Field(default=None, description="2×3 affine matrix [[a,b,c],[d,e,f]].")] = None,
    opacity: Annotated[Optional[float], Field(default=None, ge=0, le=1)] = None,
    target: EffectTargetLit = "fill",
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"gradientStops": gradientStops, "gradientType": gradientType, "target": target}
    for k, v in [("nodeId", nodeId), ("nodeIds", nodeIds), ("gradientTransform", gradientTransform), ("opacity", opacity)]:
        if v is not None:
            args[k] = v
    return await _forward("set_gradient_fill", args, fileKey=fileKey)


@mcp.tool(description="""Apply effects (shadows, blurs) to a node.

Replaces node.effects wholesale with the supplied array. Pass [] to clear all
effects. Readable back via get_node → styles.effects[].

Per-effect shapes:
  DROP_SHADOW / INNER_SHADOW:
    {type, color: "#hex", opacity?: float, offset: {x, y}, radius, spread?: float,
     blendMode?: str, visible?: bool}
  LAYER_BLUR / BACKGROUND_BLUR:
    {type, radius, visible?: bool}

Parameters:
  nodeId   — single node ID, or use nodeIds for multiple.
  nodeIds  — list of node IDs; same effects array applied to all.
  effects  — list of effect dicts (see above). Pass [] to clear.

Supports _ref:<alias> substitution on nodeId/nodeIds.""")
async def set_effects(
    effects: Annotated[list[dict], Field(description="List of effect dicts. Pass [] to clear all effects.")],
    nodeId: Annotated[Optional[str], Field(default=None, description="Node ID. Use nodeIds for multiple nodes.")] = None,
    nodeIds: Annotated[Optional[list[str]], Field(default=None, description="List of node IDs; same effects applied to all.")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"effects": effects}
    if nodeId is not None:
        args["nodeId"] = nodeId
    if nodeIds is not None:
        args["nodeIds"] = nodeIds
    return await _forward("set_effects", args, fileKey=fileKey)


@mcp.tool(description="""Import an SVG string as a Figma vector node.

Calls figma.createNodeFromSvg(svg). The returned node id is usable in
follow-up run_batch commands via _ref:<alias>.

Parameters:
  svg      — inline SVG markup, e.g. '<svg><circle cx="20" cy="20" r="15" fill="red"/></svg>'.
  x        — x position on canvas (default 0). Ignored inside auto-layout parents.
  y        — y position on canvas (default 0). Ignored inside auto-layout parents.
  parentId — parent frame/group ID. Defaults to current page.
  name     — node name in the layers panel.

SVG parse errors are returned as structured errors with the first 200 chars
of the SVG for diagnosis.""")
async def create_svg(
    svg: Annotated[str, Field(description="Inline SVG markup string.")],
    x: float = 0,
    y: float = 0,
    parentId: ParentId = None,
    name: Annotated[Optional[str], Field(default=None, description="Node name in the Figma layers panel.")] = None,
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    args: dict[str, Any] = {"svg": svg, "x": x, "y": y}
    if parentId is not None:
        args["parentId"] = parentId
    if name is not None:
        args["name"] = name
    return await _forward("create_svg", args, fileKey=fileKey)


@mcp.tool(description="Execute an ordered array of commands in a single plugin round-trip. Later commands can reference prior results via `_ref:<alias>` strings in params.")
async def run_batch(
    commands: Annotated[list[dict], Field(description="Ordered list of commands; parents must precede children. Each item: {command: string, params?: object, _ref?: string}.")],
    fileKey: FileKeyOpt = None,
) -> list[TextContent]:
    try:
        result = await send("run_batch", {"commands": commands}, timeout=BATCH_TIMEOUT_S, fileKey=fileKey)
        return _result_text(result)
    except Exception as e:
        return _error_text(str(e))


ExportFormat = Literal["PNG", "JPG", "SVG", "PDF"]
EXPORT_FORMAT_MIME = {
    "PNG": "image/png",
    "JPG": "image/jpeg",
    "SVG": "image/svg+xml",
    "PDF": "application/pdf",
}


@mcp.tool(description="Render a frame/component/instance as PNG/JPG/SVG/PDF. Default format=PNG, scale=2. Pass savePath (absolute path) to write to disk — required for SVG/PDF. PNG/JPG: auto-scales so the longer edge ≤ maxDimension (512=layout check, 1024=default, 2048=export-quality); explicit scale overrides maxDimension auto-scale. SVG/PDF: vector, scale ignored. Back-compat: existing callers that omit format/scale still work identically (PNG, maxDimension-capped).")
async def export_frame(
    nodeId: str,
    maxDimension: Annotated[int, Field(ge=64, le=4096)] = 1024,
    savePath: str | None = None,
    format: ExportFormat = "PNG",
    scale: Annotated[float, Field(ge=0.1, le=4.0)] = 2,
    fileKey: FileKeyOpt = None,
) -> list[TextContent | ImageContent]:
    fmt = (format or "PNG").upper()
    # SVG/PDF require savePath — enforce before hitting the plugin
    if fmt in ("SVG", "PDF") and not savePath:
        return _error_text(
            f"savePath is required for {fmt} export. Provide an absolute path (e.g. /tmp/export.{fmt.lower()})."
        )
    try:
        result = await send("export_frame", {"nodeId": nodeId, "maxDimension": maxDimension, "format": fmt, "scale": scale}, fileKey=fileKey)
    except Exception as e:
        return _error_text(str(e))
    base64_data = result.get("base64") if isinstance(result, dict) else None
    if not isinstance(base64_data, str) or not base64_data:
        return _error_text("plugin returned no image data")

    mime_type = EXPORT_FORMAT_MIME.get(fmt, "image/png")

    saved_info = None
    if savePath:
        import base64 as b64mod
        if not os.path.isabs(savePath):
            return _error_text(f"savePath must be absolute, got: {savePath}")
        try:
            parent = os.path.dirname(savePath)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(savePath, "wb") as f:
                f.write(b64mod.b64decode(base64_data))
            saved_info = {"savedTo": savePath, "bytes": os.path.getsize(savePath)}
        except Exception as e:
            return _error_text(f"failed to write {savePath}: {e}")

    meta = {k: v for k, v in result.items() if k != "base64"}
    if saved_info:
        meta.update(saved_info)

    if savePath:
        return [TextContent(type="text", text=json.dumps(meta, indent=2))]

    # Inline return — only for PNG/JPG (SVG/PDF are always saved, enforced above)
    if len(base64_data) > EXPORT_MAX_BASE64_BYTES:
        mb = len(base64_data) / (1024 * 1024)
        suggested = max(64, maxDimension // 2)
        return _error_text(
            f"image too large for inline return ({mb:.2f} MB base64). "
            f"Retry with a smaller maxDimension (e.g. {suggested})."
        )
    return [
        TextContent(type="text", text=json.dumps(meta, indent=2)),
        ImageContent(type="image", data=base64_data, mimeType=mime_type),
    ]


# ---------- Entry point ----------
async def _amain() -> None:
    log("security: WebSocket bound to 127.0.0.1 (loopback only); Origin check active; no shared-secret token (local-dev mode)")
    log("security NOTE: layer names/text from Figma pass to AI unfiltered — avoid opening untrusted files while server is running")
    async with websockets.serve(_handle_plugin, "127.0.0.1", PORT, max_size=20 * 1024 * 1024):
        log(f"waiting for Figma plugin on ws://127.0.0.1:{PORT}")
        await mcp.run_stdio_async()


if __name__ == "__main__":
    anyio.run(_amain)
