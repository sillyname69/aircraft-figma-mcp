// Claude Bridge — runs inside the Figma desktop app.
// The UI iframe holds the WebSocket to the local MCP server and relays
// commands here; this file executes them with the Plugin API.

figma.showUI(__html__, { width: 260, height: 140, themeColors: true });

// ---------- identify handshake ----------
async function computeIdentity() {
  // figma.fileKey is the real cloud key — unique per document for saved files.
  let fileKey = (typeof figma.fileKey === "string" && figma.fileKey) ? figma.fileKey : null;
  if (!fileKey) {
    // clientStorage may be shared across files in dev-plugin mode, so key by
    // figma.root.name (human name) to get a per-document storage slot.
    // Not perfectly stable (user can rename) but unique enough for local sessions.
    const storageKey = "claude-bridge-uuid-" + (figma.root.name || "default");
    fileKey = await figma.clientStorage.getAsync(storageKey);
    if (!fileKey) {
      fileKey = "fallback-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      await figma.clientStorage.setAsync(storageKey, fileKey);
    }
  }
  return {
    fileKey,
    fileName: figma.root.name,
    pageId: figma.currentPage.id,
    pageName: figma.currentPage.name,
  };
}

computeIdentity().then((id) => {
  figma.ui.postMessage({ type: "identity", payload: id });
});

figma.on("currentpagechange", () => {
  computeIdentity().then((id) => {
    figma.ui.postMessage({ type: "identity", payload: id });
  });
});

// ---------- helpers ----------
function hexToPaint(hex) {
  let h = String(hex).replace("#", "").trim();
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  if (!/^[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(h)) throw new Error(`Invalid hex color: ${hex}`);
  const n = (i) => parseInt(h.slice(i, i + 2), 16) / 255;
  const paint = { type: "SOLID", color: { r: n(0), g: n(2), b: n(4) } };
  if (h.length === 8) paint.opacity = n(6);
  return paint;
}

async function getNode(id) {
  const node = await figma.getNodeByIdAsync(id);
  if (!node) throw new Error(`Node not found: ${id}`);
  return node;
}

async function getParent(parentId) {
  if (!parentId) return figma.currentPage;
  const p = await getNode(parentId);
  if (!("appendChild" in p)) throw new Error(`Node ${parentId} (${p.type}) cannot have children`);
  return p;
}

function _rgbToHex(c) {
  const hx = (v) => Math.round(v * 255).toString(16).padStart(2, "0");
  return `#${hx(c.r)}${hx(c.g)}${hx(c.b)}`.toUpperCase();
}

// Convert Figma's lineHeight / letterSpacing shape ({value,unit} | {unit:"AUTO"})
// to a JSON-friendly readback. Returns null when the API gave us figma.mixed.
function _typographyScalar(v) {
  if (v == null) return null;
  if (typeof v === "symbol") return null; // figma.mixed
  if (typeof v === "object") {
    if (v.unit === "AUTO") return { unit: "AUTO" };
    return { value: v.value, unit: v.unit };
  }
  return v;
}

// Approximate numeric weight from the font style string. Figma's TEXT API does
// not expose a numeric weight directly; the judge needs *something* comparable
// to CSS weights so convergence can be checked. This is a lookup, not inference.
function _fontWeightFromStyle(style) {
  if (!style) return null;
  const s = String(style).toLowerCase();
  // Order matters: match longest/most-specific first.
  if (s.includes("extralight") || s.includes("ultra light") || s.includes("ultralight")) return 200;
  if (s.includes("semibold") || s.includes("semi bold") || s.includes("demibold") || s.includes("demi bold")) return 600;
  if (s.includes("extrabold") || s.includes("ultra bold") || s.includes("ultrabold")) return 800;
  if (s.includes("thin") || s.includes("hairline")) return 100;
  if (s.includes("light")) return 300;
  if (s.includes("medium")) return 500;
  if (s.includes("bold")) return 700;
  if (s.includes("black") || s.includes("heavy")) return 900;
  if (s.includes("regular") || s.includes("book") || s.includes("normal")) return 400;
  return null;
}

// =============================================================================
// summarize() — full-style serializer (PHASE-08)
// Ported from gethopp/figma-mcp-bridge serializer.ts (MIT, Copyright 2026 GETHOPP LTD).
// See plugin/NOTICE.md for full attribution.
//
// Signature: summarize(node, depth = 0, opts = {})
//   opts.lean          — omit styles block; useful for large tree scans
//   opts.includeGeometry — include vectorPaths on VECTOR nodes
//
// All flat fields from the pre-PHASE-08 summarize() are preserved for
// backwards compatibility alongside the new structured styles{} block.
// =============================================================================

function _isMixed(v) { return typeof v === "symbol"; }

function _serializePaints(paints) {
  if (_isMixed(paints)) return "mixed";
  if (!paints || !Array.isArray(paints)) return [];
  const out = [];
  for (const p of paints) {
    if (p.visible === false) continue;
    switch (p.type) {
      case "SOLID":
        out.push({ type: "SOLID", color: _rgbToHex(p.color), opacity: p.opacity });
        break;
      case "GRADIENT_LINEAR":
      case "GRADIENT_RADIAL":
      case "GRADIENT_ANGULAR":
      case "GRADIENT_DIAMOND":
        out.push({
          type: p.type,
          gradientStops: (p.gradientStops || []).map((s) => ({
            color: _rgbToHex(s.color),
            opacity: s.color.a,
            position: s.position,
          })),
          gradientTransform: p.gradientTransform,
          opacity: p.opacity,
        });
        break;
      case "IMAGE":
        out.push({
          type: "IMAGE",
          scaleMode: p.scaleMode,
          imageHash: p.imageHash,
          imageTransform: p.imageTransform,
          opacity: p.opacity,
        });
        break;
      default: break;
    }
  }
  return out;
}

function _serializeEffects(effects) {
  if (!effects || !Array.isArray(effects)) return [];
  const out = [];
  for (const e of effects) {
    if (e.visible === false) continue;
    switch (e.type) {
      case "DROP_SHADOW":
      case "INNER_SHADOW":
        out.push({
          type: e.type,
          color: _rgbToHex(e.color),
          opacity: e.color.a,
          offset: e.offset,
          radius: e.radius,
          spread: e.spread,
          blendMode: e.blendMode,
        });
        break;
      case "LAYER_BLUR":
      case "BACKGROUND_BLUR":
        out.push({ type: e.type, radius: e.radius });
        break;
      default: break;
    }
  }
  return out;
}

function _serializeStyles(node) {
  const styles = {};

  if ("opacity" in node) styles.opacity = node.opacity;
  if ("blendMode" in node) styles.blendMode = node.blendMode;
  if ("visible" in node) styles.visible = node.visible;

  if ("fills" in node) styles.fills = _serializePaints(node.fills);
  if ("strokes" in node) styles.strokes = _serializePaints(node.strokes);
  if ("strokeWeight" in node) styles.strokeWeight = _isMixed(node.strokeWeight) ? "mixed" : node.strokeWeight;
  if ("strokeAlign" in node) styles.strokeAlign = node.strokeAlign;
  if ("strokeCap" in node) styles.strokeCap = node.strokeCap;
  if ("strokeJoin" in node) styles.strokeJoin = node.strokeJoin;
  if ("dashPattern" in node && Array.isArray(node.dashPattern) && node.dashPattern.length > 0) {
    styles.dashPattern = [...node.dashPattern];
  }

  if ("effects" in node && Array.isArray(node.effects) && node.effects.length > 0) {
    styles.effects = _serializeEffects(node.effects);
  }

  if ("cornerRadius" in node) {
    styles.cornerRadius = _isMixed(node.cornerRadius) ? "mixed" : node.cornerRadius;
  }
  if ("topLeftRadius" in node) {
    const tl = node.topLeftRadius, tr = node.topRightRadius;
    const br = node.bottomRightRadius, bl = node.bottomLeftRadius;
    if (tl !== tr || tr !== br || br !== bl) {
      styles.cornerRadii = { topLeft: tl, topRight: tr, bottomRight: br, bottomLeft: bl };
    }
  }
  if ("cornerSmoothing" in node && node.cornerSmoothing > 0) {
    styles.cornerSmoothing = node.cornerSmoothing;
  }

  if ("layoutMode" in node && node.layoutMode !== "NONE") {
    styles.autoLayout = {
      direction: node.layoutMode,
      gap: node.itemSpacing,
      primaryAxisAlign: node.primaryAxisAlignItems,
      counterAxisAlign: node.counterAxisAlignItems,
      primaryAxisSizing: node.primaryAxisSizingMode,
      counterAxisSizing: node.counterAxisSizingMode,
      wrap: "layoutWrap" in node ? node.layoutWrap : undefined,
      counterAxisSpacing: "counterAxisSpacing" in node ? node.counterAxisSpacing : undefined,
    };
  }

  if ("paddingLeft" in node) {
    const t = node.paddingTop, r = node.paddingRight, b = node.paddingBottom, l = node.paddingLeft;
    if (t > 0 || r > 0 || b > 0 || l > 0) styles.padding = { top: t, right: r, bottom: b, left: l };
  }

  if ("clipsContent" in node) styles.clipsContent = node.clipsContent;
  if ("rotation" in node && node.rotation !== 0) styles.rotation = node.rotation;
  if ("constraints" in node) {
    styles.constraints = { horizontal: node.constraints.horizontal, vertical: node.constraints.vertical };
  }

  return styles;
}

async function summarize(node, depth = 0, opts = {}) {
  const { lean = false, includeGeometry = false } = opts;
  const out = { id: node.id, name: node.name, type: node.type };

  // Geometry
  if ("x" in node) Object.assign(out, { x: node.x, y: node.y, width: node.width, height: node.height });

  // TEXT content
  if (node.type === "TEXT") out.characters = node.characters;

  // --- legacy flat fields (backwards compat) ---

  if ("fills" in node && Array.isArray(node.fills)) {
    const first = node.fills[0];
    if (first && first.type === "IMAGE") {
      out.fillType = "IMAGE";
      out.imageHash = first.imageHash;
      out.scaleMode = first.scaleMode;
      out.opacity = first.opacity != null ? first.opacity : 1;
    } else {
      const solid = node.fills.find((f) => f.type === "SOLID");
      if (solid) out.fill = _rgbToHex(solid.color);
    }
  }

  if ("strokes" in node && Array.isArray(node.strokes) && node.strokes.length > 0) {
    const s = node.strokes.find((p) => p.type === "SOLID");
    if (s) {
      out.stroke = {
        color: _rgbToHex(s.color),
        weight: typeof node.strokeWeight === "number" ? node.strokeWeight : null,
      };
    }
  }

  if ("layoutMode" in node && node.layoutMode !== "NONE") {
    out.layoutMode = node.layoutMode;
    if ("paddingTop" in node) {
      out.paddingTop = node.paddingTop;
      out.paddingRight = node.paddingRight;
      out.paddingBottom = node.paddingBottom;
      out.paddingLeft = node.paddingLeft;
    }
    if ("itemSpacing" in node) out.itemSpacing = node.itemSpacing;
    if ("primaryAxisAlignItems" in node) out.primaryAxisAlignItems = node.primaryAxisAlignItems;
    if ("counterAxisAlignItems" in node) out.counterAxisAlignItems = node.counterAxisAlignItems;
  }

  if ("cornerRadius" in node) {
    if (_isMixed(node.cornerRadius)) {
      out.cornerRadius = {
        topLeft: node.topLeftRadius,
        topRight: node.topRightRadius,
        bottomRight: node.bottomRightRadius,
        bottomLeft: node.bottomLeftRadius,
      };
    } else if (typeof node.cornerRadius === "number") {
      out.cornerRadius = node.cornerRadius;
    }
  }

  if (node.type === "TEXT") {
    if (typeof node.fontName === "object" && node.fontName && !_isMixed(node.fontName)) {
      out.fontName = { family: node.fontName.family, style: node.fontName.style };
      out.fontWeight = _fontWeightFromStyle(node.fontName.style);
    }
    if (typeof node.fontSize === "number") out.fontSize = node.fontSize;
    const lh = _typographyScalar(node.lineHeight);
    if (lh !== null) out.lineHeight = lh;
    const ls = _typographyScalar(node.letterSpacing);
    if (ls !== null) out.letterSpacing = ls;
    if (typeof node.textAlignHorizontal === "string") out.textAlignHorizontal = node.textAlignHorizontal;
    if (typeof node.textAlignVertical === "string") out.textAlignVertical = node.textAlignVertical;
    if (typeof node.textCase === "string") out.textCase = node.textCase;
  }

  // --- structured styles block (omitted in lean mode) ---
  if (!lean) {
    out.styles = _serializeStyles(node);

    if (node.type === "TEXT") {
      if ("textDecoration" in node) out.styles.textDecoration = _isMixed(node.textDecoration) ? "mixed" : node.textDecoration;
      if ("textAutoResize" in node) out.styles.textAutoResize = node.textAutoResize;
      if (node.fontName && !_isMixed(node.fontName)) {
        out.styles.fontFamily = node.fontName.family;
        out.styles.fontStyle = node.fontName.style;
      } else if (_isMixed(node.fontName)) {
        out.styles.fontFamily = "mixed";
        out.styles.fontStyle = "mixed";
      }
      if ("fontSize" in node) out.styles.fontSize = _isMixed(node.fontSize) ? "mixed" : node.fontSize;
      if ("fontWeight" in node) out.styles.fontWeight = _isMixed(node.fontWeight) ? "mixed" : node.fontWeight;
      if ("lineHeight" in node) out.styles.lineHeight = _typographyScalar(node.lineHeight);
      if ("letterSpacing" in node) out.styles.letterSpacing = _typographyScalar(node.letterSpacing);
      if ("textAlignHorizontal" in node) out.styles.textAlignHorizontal = _isMixed(node.textAlignHorizontal) ? "mixed" : node.textAlignHorizontal;
      if ("textAlignVertical" in node) out.styles.textAlignVertical = _isMixed(node.textAlignVertical) ? "mixed" : node.textAlignVertical;
    }

    if (node.type === "INSTANCE") {
      try {
        const mc = await node.getMainComponentAsync();
        out.mainComponentId = mc ? mc.id : null;
      } catch {
        out.mainComponentId = null;
      }
    }

    if (node.type === "STAR") {
      if ("pointCount" in node) out.pointCount = node.pointCount;
      if ("innerRadius" in node) out.innerRadius = node.innerRadius;
    }

    if (node.type === "VECTOR" && includeGeometry) {
      if ("vectorPaths" in node && Array.isArray(node.vectorPaths)) {
        out.vectorPaths = node.vectorPaths.map((vp) => ({ windingRule: vp.windingRule, data: vp.data }));
      }
    }
  }

  if ("children" in node) {
    out.childCount = node.children.length;
    if (depth > 0) out.children = await Promise.all(node.children.map((c) => summarize(c, depth - 1, opts)));
  }
  return out;
}

async function place(node, parent, p) {
  parent.appendChild(node);
  if (p.name) node.name = p.name;
  // Auto-layout children get positioned by their parent.
  if (!("layoutMode" in parent) || parent.layoutMode === "NONE") {
    node.x = p.x ?? 0;
    node.y = p.y ?? 0;
  }
  return await summarize(node);
}

async function loadFontsFor(textNode) {
  const fonts = textNode.getRangeAllFontNames(0, Math.max(textNode.characters.length, 1));
  await Promise.all(fonts.map((f) => figma.loadFontAsync(f)));
}

// ---------- commands ----------
const handlers = {
  async get_document_info() {
    await figma.loadAllPagesAsync();
    return {
      file: figma.root.name,
      currentPage: { id: figma.currentPage.id, name: figma.currentPage.name },
      pages: figma.root.children.map((p) => ({ id: p.id, name: p.name })),
      topLevelNodes: await Promise.all(figma.currentPage.children.map((n) => summarize(n))),
    };
  },

  async get_selection() {
    return Promise.all(figma.currentPage.selection.map((n) => summarize(n, 1)));
  },

  async get_node({ nodeId, depth = 1, lean = false, includeGeometry = false }) {
    return await summarize(await getNode(nodeId), depth, { lean, includeGeometry });
  },

  async create_frame(p) {
    const f = figma.createFrame();
    f.resize(p.width ?? 400, p.height ?? 300);
    f.fills = p.fill ? [hexToPaint(p.fill)] : [];
    if (p.cornerRadius != null) f.cornerRadius = p.cornerRadius;
    if (p.layoutMode && p.layoutMode !== "NONE") {
      f.layoutMode = p.layoutMode;
      // Primary axis = layout direction (vertical → height, horizontal → width).
      // Counter axis = the other one. Hug contents on any axis the caller didn't size.
      const vertical = p.layoutMode === "VERTICAL";
      const primaryGiven = vertical ? p.height != null : p.width != null;
      const counterGiven = vertical ? p.width != null : p.height != null;
      f.primaryAxisSizingMode = primaryGiven ? "FIXED" : "AUTO";
      f.counterAxisSizingMode = counterGiven ? "FIXED" : "AUTO";
      if (p.itemSpacing != null) f.itemSpacing = p.itemSpacing;
      if (p.padding != null) {
        f.paddingTop = f.paddingBottom = f.paddingLeft = f.paddingRight = p.padding;
      }
    }
    return place(f, await getParent(p.parentId), p);
  },

  async create_rectangle(p) {
    const r = figma.createRectangle();
    r.resize(p.width ?? 100, p.height ?? 100);
    if (p.fill) r.fills = [hexToPaint(p.fill)];
    if (p.cornerRadius != null) r.cornerRadius = p.cornerRadius;
    return place(r, await getParent(p.parentId), p);
  },

  async create_ellipse(p) {
    const e = figma.createEllipse();
    e.resize(p.width ?? 100, p.height ?? 100);
    if (p.fill) e.fills = [hexToPaint(p.fill)];
    return place(e, await getParent(p.parentId), p);
  },

  async create_text(p) {
    const font = { family: p.fontFamily || "Inter", style: p.fontStyle || "Regular" };
    try {
      await figma.loadFontAsync(font);
    } catch {
      throw new Error(`Font not available: ${font.family} ${font.style}. Check the exact style name in Figma's font menu.`);
    }
    const t = figma.createText();
    t.fontName = font;
    t.fontSize = p.fontSize ?? 16;
    t.characters = p.text;
    if (p.fill) t.fills = [hexToPaint(p.fill)];
    if (p.width) {
      t.textAutoResize = "HEIGHT";
      t.resize(p.width, t.height);
    }
    return place(t, await getParent(p.parentId), p);
  },

  async set_text({ nodeId, text }) {
    const t = await getNode(nodeId);
    if (t.type !== "TEXT") throw new Error(`Node ${nodeId} is ${t.type}, not TEXT`);
    await loadFontsFor(t);
    t.characters = text;
    return await summarize(t);
  },

  async set_fill({ nodeId, color }) {
    const n = await getNode(nodeId);
    if (!("fills" in n)) throw new Error(`Node ${nodeId} has no fills`);
    n.fills = [hexToPaint(color)];
    return await summarize(n);
  },

  // Change the auto-layout mode of a frame. Safe to call on frames that
  // already have children — Figma will re-flow them. Pass mode="NONE" to
  // detach auto-layout entirely (returns children to free-form x/y).
  async set_auto_layout({ nodeId, mode, primaryAxisAlign, counterAxisAlign }) {
    const n = await getNode(nodeId);
    if (!("layoutMode" in n)) {
      throw new Error(`Node ${nodeId} (${n.type}) does not support auto layout`);
    }
    const allowedModes = ["NONE", "HORIZONTAL", "VERTICAL"];
    if (!allowedModes.includes(mode)) {
      throw new Error(`set_auto_layout: invalid mode "${mode}" (allowed: ${allowedModes.join(", ")})`);
    }
    n.layoutMode = mode;
    if (mode !== "NONE") {
      const allowedPrimary = ["MIN", "CENTER", "MAX", "SPACE_BETWEEN"];
      const allowedCounter = ["MIN", "CENTER", "MAX", "BASELINE"];
      if (primaryAxisAlign != null) {
        if (!allowedPrimary.includes(primaryAxisAlign)) {
          throw new Error(`set_auto_layout: invalid primaryAxisAlign "${primaryAxisAlign}" (allowed: ${allowedPrimary.join(", ")})`);
        }
        n.primaryAxisAlignItems = primaryAxisAlign;
      }
      if (counterAxisAlign != null) {
        if (!allowedCounter.includes(counterAxisAlign)) {
          throw new Error(`set_auto_layout: invalid counterAxisAlign "${counterAxisAlign}" (allowed: ${allowedCounter.join(", ")})`);
        }
        n.counterAxisAlignItems = counterAxisAlign;
      }
    }
    return await summarize(n);
  },

  // Per-side padding on an auto-layout frame. Fields are independent; omitted
  // sides keep their current value. `all` is a shorthand that sets all four
  // (and is overridden by any explicit side also provided in the same call).
  // If the frame is not auto-layout, auto-promote to VERTICAL with sensible
  // defaults and emit a warning (padding is meaningless on free-form frames).
  async set_padding({ nodeId, top, right, bottom, left, all }) {
    const n = await getNode(nodeId);
    if (!("paddingTop" in n)) {
      throw new Error(`Node ${nodeId} (${n.type}) does not support padding`);
    }
    const warnings = [];
    if (!("layoutMode" in n) || n.layoutMode === "NONE") {
      n.layoutMode = "VERTICAL";
      n.primaryAxisSizingMode = "AUTO";
      n.counterAxisSizingMode = "AUTO";
      warnings.push("auto-promoted non-auto-layout frame to layoutMode=VERTICAL to accept padding");
    }
    const resolve = (side) => {
      if (side != null) return side;
      if (all != null) return all;
      return null;
    };
    const t = resolve(top);
    const r = resolve(right);
    const b = resolve(bottom);
    const l = resolve(left);
    if (t != null) n.paddingTop = t;
    if (r != null) n.paddingRight = r;
    if (b != null) n.paddingBottom = b;
    if (l != null) n.paddingLeft = l;
    const out = await summarize(n);
    if (warnings.length) out.warnings = warnings;
    return out;
  },

  // Gap between children on an auto-layout frame.
  async set_item_spacing({ nodeId, itemSpacing }) {
    const n = await getNode(nodeId);
    if (!("itemSpacing" in n)) {
      throw new Error(`Node ${nodeId} (${n.type}) does not support itemSpacing`);
    }
    if (!("layoutMode" in n) || n.layoutMode === "NONE") {
      throw new Error(`Node ${nodeId} is not auto-layout — call set_auto_layout first`);
    }
    if (typeof itemSpacing !== "number") {
      throw new Error("set_item_spacing: itemSpacing must be a number");
    }
    n.itemSpacing = itemSpacing;
    return await summarize(n);
  },

  // Per-corner radius. `all` is a shorthand that sets all four; explicit corners
  // override `all` in the same call. Omitted corners keep their current value.
  async set_corner_radius({ nodeId, topLeft, topRight, bottomRight, bottomLeft, all }) {
    const n = await getNode(nodeId);
    if (!("cornerRadius" in n)) {
      throw new Error(`Node ${nodeId} (${n.type}) does not support cornerRadius`);
    }
    const anyPerCorner = topLeft != null || topRight != null || bottomRight != null || bottomLeft != null;
    if (!anyPerCorner && all != null) {
      n.cornerRadius = all;
      return await summarize(n);
    }
    // Mixed per-corner path. Fill from existing-or-`all` so unspecified corners
    // don't collapse to zero.
    const current = typeof n.cornerRadius === "number" ? n.cornerRadius : null;
    const base = all != null ? all : current;
    const resolve = (corner, existingKey) => {
      if (corner != null) return corner;
      if (base != null) return base;
      // Existing per-corner value (when node already had mixed corners).
      return typeof n[existingKey] === "number" ? n[existingKey] : 0;
    };
    n.topLeftRadius = resolve(topLeft, "topLeftRadius");
    n.topRightRadius = resolve(topRight, "topRightRadius");
    n.bottomRightRadius = resolve(bottomRight, "bottomRightRadius");
    n.bottomLeftRadius = resolve(bottomLeft, "bottomLeftRadius");
    return await summarize(n);
  },

  // Mutate typography on an existing TEXT node. Omitted fields keep current
  // values. Font family/style changes require a new fontName; we honor the
  // Phase-01 font safe-list by warning (not crashing) on anything outside it.
  // lineHeight / letterSpacing accept either a number (PIXELS) or a
  // {value, unit} / {unit:"AUTO"} / {value, unit:"PERCENT"} object.
  async set_text_style({ nodeId, fontFamily, fontStyle, fontSize, lineHeight, letterSpacing, textAlignHorizontal, textCase }) {
    const n = await getNode(nodeId);
    if (n.type !== "TEXT") throw new Error(`Node ${nodeId} is ${n.type}, not TEXT`);
    const warnings = [];
    const SAFE_LIST = ["Inter", "Roboto", "IBM Plex Sans", "IBM Plex Mono", "JetBrains Mono", "Atkinson Hyperlegible"];

    // Load all current fonts first so any mutation on existing characters works.
    await loadFontsFor(n);

    // Font change: load the new font, warn if outside safe-list but still try.
    if (fontFamily != null || fontStyle != null) {
      const nextFamily = fontFamily != null ? fontFamily : (n.fontName && n.fontName.family) || "Inter";
      const nextStyle = fontStyle != null ? fontStyle : (n.fontName && n.fontName.style) || "Regular";
      if (!SAFE_LIST.includes(nextFamily)) {
        warnings.push(`font "${nextFamily}" is outside the Phase-01 safe-list (${SAFE_LIST.join(", ")}); attempting load but may fall back`);
      }
      const nextFont = { family: nextFamily, style: nextStyle };
      try {
        await figma.loadFontAsync(nextFont);
        n.fontName = nextFont;
      } catch (e) {
        warnings.push(`font "${nextFamily} ${nextStyle}" not available (${e.message || e}); kept existing font`);
      }
    }

    if (fontSize != null) {
      if (typeof fontSize !== "number" || fontSize <= 0) {
        throw new Error("set_text_style: fontSize must be a positive number");
      }
      n.fontSize = fontSize;
    }
    if (lineHeight != null) {
      if (typeof lineHeight === "number") {
        n.lineHeight = { value: lineHeight, unit: "PIXELS" };
      } else if (lineHeight && typeof lineHeight === "object") {
        if (lineHeight.unit === "AUTO") {
          n.lineHeight = { unit: "AUTO" };
        } else if (lineHeight.unit === "PIXELS" || lineHeight.unit === "PERCENT") {
          n.lineHeight = { value: lineHeight.value, unit: lineHeight.unit };
        } else {
          throw new Error("set_text_style: lineHeight.unit must be AUTO, PIXELS, or PERCENT");
        }
      }
    }
    if (letterSpacing != null) {
      if (typeof letterSpacing === "number") {
        n.letterSpacing = { value: letterSpacing, unit: "PIXELS" };
      } else if (letterSpacing && typeof letterSpacing === "object") {
        if (letterSpacing.unit === "PIXELS" || letterSpacing.unit === "PERCENT") {
          n.letterSpacing = { value: letterSpacing.value, unit: letterSpacing.unit };
        } else {
          throw new Error("set_text_style: letterSpacing.unit must be PIXELS or PERCENT");
        }
      }
    }
    if (textAlignHorizontal != null) {
      const allowed = ["LEFT", "CENTER", "RIGHT", "JUSTIFIED"];
      if (!allowed.includes(textAlignHorizontal)) {
        throw new Error(`set_text_style: invalid textAlignHorizontal "${textAlignHorizontal}" (allowed: ${allowed.join(", ")})`);
      }
      n.textAlignHorizontal = textAlignHorizontal;
    }
    if (textCase != null) {
      const allowed = ["ORIGINAL", "UPPER", "LOWER", "TITLE"];
      if (!allowed.includes(textCase)) {
        throw new Error(`set_text_style: invalid textCase "${textCase}" (allowed: ${allowed.join(", ")})`);
      }
      n.textCase = textCase;
    }
    const out = await summarize(n);
    if (warnings.length) out.warnings = warnings;
    return out;
  },

  // Return the top-level frames of the current page (and the page's own bbox).
  // Shape matches what crop-reference.py expects — section-level nodes with
  // name + bbox + nodeId, nothing more. Non-frame children are included only
  // when they expose x/y/width/height (groups, components, instances), so
  // page-dumps of mixed documents still make sense.
  async get_page_frames() {
    const page = figma.currentPage;
    const items = [];
    for (const child of page.children) {
      if (!("x" in child)) continue;
      items.push({
        nodeId: child.id,
        name: child.name,
        type: child.type,
        x: child.x,
        y: child.y,
        width: child.width,
        height: child.height,
      });
    }
    // Compute a bounding box for the whole page so the slicer can map
    // section bboxes proportionally into the reference image.
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const it of items) {
      if (it.x < minX) minX = it.x;
      if (it.y < minY) minY = it.y;
      if (it.x + it.width > maxX) maxX = it.x + it.width;
      if (it.y + it.height > maxY) maxY = it.y + it.height;
    }
    const bbox = items.length ? { x: minX, y: minY, width: maxX - minX, height: maxY - minY } : null;
    return { pageId: page.id, pageName: page.name, bbox, frames: items };
  },

  async set_image_fill_bytes({ nodeId, bytesBase64, scaleMode, opacity }) {
    const n = await getNode(nodeId);
    if (!("fills" in n)) throw new Error(`Node ${nodeId} (${n.type}) has no fills`);
    if (typeof n.fills === "symbol") {
      throw new Error(`Cannot set image fill on node ${nodeId} with mixed/inherited fills`);
    }
    if (typeof bytesBase64 !== "string" || !bytesBase64) {
      throw new Error("set_image_fill_bytes: bytesBase64 must be a non-empty string");
    }
    const mode = scaleMode || "FILL";
    if (!["FILL", "FIT", "CROP", "TILE"].includes(mode)) {
      throw new Error(`set_image_fill_bytes: invalid scaleMode "${mode}" (allowed: FILL, FIT, CROP, TILE)`);
    }
    const op = opacity != null ? opacity : 1;
    const bytes = figma.base64Decode(bytesBase64);
    let image;
    try {
      image = figma.createImage(bytes);
    } catch (e) {
      throw new Error(
        `Figma rejected the image (${e.message}). Likely exceeds its internal pixel-dimension cap. Downscale the source and retry.`
      );
    }
    n.fills = [{ type: "IMAGE", scaleMode: mode, imageHash: image.hash, opacity: op }];
    return await summarize(n);
  },

  async set_stroke({ nodeId, color, weight = 1 }) {
    const n = await getNode(nodeId);
    if (!("strokes" in n)) throw new Error(`Node ${nodeId} has no strokes`);
    n.strokes = [hexToPaint(color)];
    n.strokeWeight = weight;
    return await summarize(n);
  },

  async move_node({ nodeId, x, y }) {
    const n = await getNode(nodeId);
    n.x = x; n.y = y;
    return await summarize(n);
  },

  async resize_node({ nodeId, width, height }) {
    const n = await getNode(nodeId);
    if (!("resize" in n)) throw new Error(`Node ${nodeId} cannot be resized`);
    n.resize(width, height);
    return await summarize(n);
  },

  async rename_node({ nodeId, name }) {
    const n = await getNode(nodeId);
    n.name = name;
    return await summarize(n);
  },

  async delete_node({ nodeId }) {
    const n = await getNode(nodeId);
    const info = { id: n.id, name: n.name, type: n.type };
    n.remove();
    return { deleted: info };
  },

  async clone_node({ nodeId, x, y }) {
    const n = await getNode(nodeId);
    const c = n.clone();
    if (x != null) c.x = x;
    if (y != null) c.y = y;
    return await summarize(c);
  },

  // ---------- org tools (PHASE-12) ----------

  async reparent_nodes({ nodeIds, newParentId, index }) {
    await figma.loadAllPagesAsync();
    if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
      throw new Error("reparent_nodes: nodeIds must be a non-empty array");
    }
    const newParent = await getParent(newParentId);
    const reparented = [];
    const failed = [];
    for (const id of nodeIds) {
      try {
        const n = await figma.getNodeByIdAsync(id);
        if (!n) throw new Error(`Node not found: ${id}`);
        const oldParentId = n.parent ? n.parent.id : null;
        // Find old page by walking up
        let oldPage = n.parent;
        while (oldPage && oldPage.type !== "PAGE") oldPage = oldPage.parent;
        const oldPageId = oldPage ? oldPage.id : null;
        // Find new page by walking up from newParent
        let newPage = newParent;
        while (newPage && newPage.type !== "PAGE" && newPage.type !== "DOCUMENT") newPage = newPage.parent;
        const newPageId = newPage ? newPage.id : null;
        if (typeof index === "number") {
          newParent.insertChild(index, n);
        } else {
          newParent.appendChild(n);
        }
        reparented.push({ id: n.id, oldParentId, newParentId: newParent.id, oldPageId, newPageId });
      } catch (e) {
        failed.push({ id, error: e && e.message ? e.message : String(e) });
      }
    }
    return { reparented, failed };
  },

  async group_nodes({ nodeIds, name }) {
    if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
      throw new Error("group_nodes: nodeIds must be a non-empty array");
    }
    const nodes = [];
    for (const id of nodeIds) {
      const n = await getNode(id);
      nodes.push(n);
    }
    // All nodes must share a parent
    const firstParent = nodes[0].parent;
    for (const n of nodes) {
      if (n.parent !== firstParent) {
        throw new Error("group_nodes: all nodes must share a parent");
      }
    }
    if (!firstParent || !("appendChild" in firstParent)) {
      throw new Error("group_nodes: nodes must have a valid parent");
    }
    const group = figma.group(nodes, firstParent);
    if (name) group.name = name;
    return await summarize(group);
  },

  async ungroup_node({ nodeId }) {
    const n = await getNode(nodeId);
    if (n.type !== "GROUP") {
      throw new Error(`ungroup_node: node ${nodeId} is ${n.type}, not GROUP`);
    }
    const parentId = n.parent ? n.parent.id : null;
    const children = figma.ungroup(n);
    return { ungrouped: children.map((c) => c.id), parentId };
  },

  async fit_to_content({ nodeId }) {
    const node = await getNode(nodeId);
    if (node.type !== "FRAME" && node.type !== "SECTION") {
      throw new Error(`fit_to_content: node ${nodeId} is ${node.type}, expected FRAME or SECTION`);
    }
    if (node.type === "FRAME" && node.layoutMode !== "NONE") {
      return { ...await summarize(node), warning: "fit_to_content: auto-layout FRAME already fits its content — no-op" };
    }
    if (!node.children || node.children.length === 0) {
      return { ...await summarize(node), warning: "fit_to_content: node has no children — no-op" };
    }
    // Compute bounding box of all children using absoluteBoundingBox
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const child of node.children) {
      const bb = child.absoluteBoundingBox;
      if (!bb) continue;
      if (bb.x < minX) minX = bb.x;
      if (bb.y < minY) minY = bb.y;
      if (bb.x + bb.width > maxX) maxX = bb.x + bb.width;
      if (bb.y + bb.height > maxY) maxY = bb.y + bb.height;
    }
    if (!isFinite(minX)) return { ...await summarize(node), warning: "fit_to_content: could not compute child bounds — no-op" };
    const newW = maxX - minX;
    const newH = maxY - minY;
    // Delta between node's absolute origin and the children's bounding box origin
    const nodeAbs = node.absoluteBoundingBox;
    const deltaX = nodeAbs ? minX - nodeAbs.x : 0;
    const deltaY = nodeAbs ? minY - nodeAbs.y : 0;
    // Shift children so their positions are relative to the new top-left
    for (const child of node.children) {
      child.x = child.x - deltaX;
      child.y = child.y - deltaY;
    }
    // Reposition node to match the bounding box origin
    if (nodeAbs) {
      node.x = node.x + deltaX;
      node.y = node.y + deltaY;
    }
    if (node.type === "SECTION") {
      node.resizeWithoutConstraints(newW, newH);
    } else {
      node.resize(newW, newH);
    }
    return await summarize(node);
  },

  async distribute_horizontally({ nodeIds }) {
    if (!Array.isArray(nodeIds) || nodeIds.length < 3) {
      throw new Error("distribute_horizontally: requires at least 3 nodeIds");
    }
    const nodes = [];
    for (const id of nodeIds) nodes.push(await getNode(id));
    // Sort by x position
    nodes.sort((a, b) => a.x - b.x);
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const totalSpan = (last.x + last.width) - first.x;
    const totalNodeWidth = nodes.reduce((sum, n) => sum + n.width, 0);
    const gap = (totalSpan - totalNodeWidth) / (nodes.length - 1);
    let cursor = first.x + first.width + gap;
    const distributed = [{ id: first.id, x: first.x, y: first.y }];
    for (let i = 1; i < nodes.length - 1; i++) {
      nodes[i].x = Math.round(cursor);
      distributed.push({ id: nodes[i].id, x: nodes[i].x, y: nodes[i].y });
      cursor += nodes[i].width + gap;
    }
    distributed.push({ id: last.id, x: last.x, y: last.y });
    return { distributed };
  },

  async distribute_vertically({ nodeIds }) {
    if (!Array.isArray(nodeIds) || nodeIds.length < 3) {
      throw new Error("distribute_vertically: requires at least 3 nodeIds");
    }
    const nodes = [];
    for (const id of nodeIds) nodes.push(await getNode(id));
    // Sort by y position
    nodes.sort((a, b) => a.y - b.y);
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const totalSpan = (last.y + last.height) - first.y;
    const totalNodeHeight = nodes.reduce((sum, n) => sum + n.height, 0);
    const gap = (totalSpan - totalNodeHeight) / (nodes.length - 1);
    let cursor = first.y + first.height + gap;
    const distributed = [{ id: first.id, x: first.x, y: first.y }];
    for (let i = 1; i < nodes.length - 1; i++) {
      nodes[i].y = Math.round(cursor);
      distributed.push({ id: nodes[i].id, x: nodes[i].x, y: nodes[i].y });
      cursor += nodes[i].height + gap;
    }
    distributed.push({ id: last.id, x: last.x, y: last.y });
    return { distributed };
  },

  async align_to_grid({ nodeIds, grid = 8 }) {
    if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
      throw new Error("align_to_grid: nodeIds must be a non-empty array");
    }
    const g = grid || 8;
    const aligned = [];
    for (const id of nodeIds) {
      const n = await getNode(id);
      n.x = Math.round(n.x / g) * g;
      n.y = Math.round(n.y / g) * g;
      aligned.push({ id: n.id, x: n.x, y: n.y });
    }
    return { aligned };
  },

  async delete_nodes({ nodeIds }) {
    if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
      throw new Error("delete_nodes: nodeIds must be a non-empty array");
    }
    const deleted = [];
    const failed = [];
    for (const id of nodeIds) {
      try {
        const n = await getNode(id);
        const info = { id: n.id, name: n.name, type: n.type };
        n.remove();
        deleted.push(info);
      } catch (e) {
        failed.push({ id, error: e && e.message ? e.message : String(e) });
      }
    }
    return { deleted, failed };
  },

  async clone_nodes({ nodeIds, offsetX = 0, offsetY = 0 }) {
    if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
      throw new Error("clone_nodes: nodeIds must be a non-empty array");
    }
    const cloned = [];
    const failed = [];
    for (const id of nodeIds) {
      try {
        const n = await getNode(id);
        const c = n.clone();
        c.x = n.x + (offsetX || 0);
        c.y = n.y + (offsetY || 0);
        cloned.push(await summarize(c));
      } catch (e) {
        failed.push({ id, error: e && e.message ? e.message : String(e) });
      }
    }
    return { cloned, failed };
  },

  async set_selection({ nodeIds }) {
    if (!Array.isArray(nodeIds)) throw new Error("set_selection: nodeIds must be an array");
    const nodes = [];
    for (const id of nodeIds) nodes.push(await getNode(id));
    figma.currentPage.selection = nodes;
    return { selected: nodes.map((n) => n.id) };
  },

  async get_viewport() {
    const v = figma.viewport;
    return {
      center: { x: v.center.x, y: v.center.y },
      zoom: v.zoom,
      bounds: {
        x: v.bounds.x,
        y: v.bounds.y,
        width: v.bounds.width,
        height: v.bounds.height,
      },
    };
  },

  async create_page({ name }) {
    if (!name || typeof name !== "string") throw new Error("create_page: name must be a non-empty string");
    const page = figma.createPage();
    page.name = name;
    return { id: page.id, name: page.name };
  },

  async create_image({ bytesBase64, x, y, parentId, width, height, name, scaleMode, opacity }) {
    if (typeof bytesBase64 !== "string" || !bytesBase64) {
      throw new Error("create_image: bytesBase64 must be a non-empty string");
    }
    const mode = scaleMode || "FILL";
    if (!["FILL", "FIT", "CROP", "TILE"].includes(mode)) {
      throw new Error(`create_image: invalid scaleMode "${mode}" (allowed: FILL, FIT, CROP, TILE)`);
    }
    const op = opacity != null ? opacity : 1;
    const r = figma.createRectangle();
    if (name) r.name = name;
    r.resize(width ?? 100, height ?? 100);
    const bytes = figma.base64Decode(bytesBase64);
    let image;
    try {
      image = figma.createImage(bytes);
    } catch (e) {
      throw new Error(
        `Figma rejected the image (${e.message}). Likely exceeds its internal pixel-dimension cap. Downscale the source and retry.`
      );
    }
    r.fills = [{ type: "IMAGE", scaleMode: mode, imageHash: image.hash, opacity: op }];
    return await place(r, await getParent(parentId), { x: x ?? 0, y: y ?? 0 });
  },

  async select_and_zoom({ nodeIds }) {
    const nodes = [];
    for (const id of nodeIds) nodes.push(await getNode(id));
    figma.currentPage.selection = nodes;
    figma.viewport.scrollAndZoomIntoView(nodes);
    return { selected: nodes.map((n) => n.id) };
  },

  // Switch the current page by name or id.
  async set_page({ pageId, pageName }) {
    await figma.loadAllPagesAsync();
    let page;
    if (pageId) {
      page = figma.root.children.find((p) => p.id === pageId);
    } else if (pageName) {
      page = figma.root.children.find((p) => p.name === pageName);
    }
    if (!page) throw new Error(`Page not found: ${pageId || pageName}`);
    await figma.setCurrentPageAsync(page);
    return { currentPage: { id: page.id, name: page.name } };
  },

  // Convert an existing frame into a Figma component (in-place).
  // The frame's children become the component's content.
  async create_component({ nodeId, name }) {
    const node = await getNode(nodeId);
    if (node.type !== "FRAME") throw new Error(`Node ${nodeId} is ${node.type}, not FRAME. Only frames can be converted to components.`);
    const component = figma.createComponentFromNode(node);
    if (name) component.name = name;
    return await summarize(component);
  },

  // Create an instance of a component node.
  async create_instance({ componentId, x, y, parentId: pid, name, textOverrides }) {
    const comp = await getNode(componentId);
    if (comp.type !== "COMPONENT") throw new Error(`Node ${componentId} is ${comp.type}, not COMPONENT`);
    const instance = comp.createInstance();
    if (name) instance.name = name;
    const parent = await getParent(pid);
    const placed = place(instance, parent, { x: x ?? 0, y: y ?? 0, name });
    if (Array.isArray(textOverrides) && textOverrides.length) {
      for (const ov of textOverrides) {
        if (!Array.isArray(ov.path) || !ov.path.length) throw new Error(`create_instance: textOverride path must be a non-empty array`);
        let n = instance;
        for (let i = 0; i < ov.path.length; i++) {
          const idx = ov.path[i];
          if (!("children" in n) || idx >= n.children.length) {
            throw new Error(`create_instance: textOverride path [${ov.path.join(",")}] out of range at depth ${i}`);
          }
          n = n.children[idx];
        }
        if (n.type !== "TEXT") throw new Error(`create_instance: textOverride path [${ov.path.join(",")}] resolved to ${n.type}, not TEXT`);
        await loadFontsFor(n);
        n.characters = ov.text;
      }
    }
    return placed;
  },

  // Combine existing COMPONENT nodes into a ComponentSetNode (variant set).
  // Requirements: all nodes are COMPONENTs, all on the same page, and ideally
  // none has existing instances (the staging-frame-first ordering ensures this
  // when called from the parser). After combination, child positions are reset
  // to (0,0) — we re-lay them out horizontally with spacing so the set is
  // readable in the file.
  async create_component_set({ componentIds, name, parentId }) {
    if (!Array.isArray(componentIds) || componentIds.length < 1) {
      throw new Error("create_component_set: componentIds must be a non-empty array");
    }
    const nodes = [];
    for (const id of componentIds) {
      const n = await getNode(id);
      if (n.type !== "COMPONENT") {
        throw new Error(`create_component_set: node ${id} is ${n.type}, not COMPONENT`);
      }
      nodes.push(n);
    }
    const firstPage = nodes[0].parent && nodes[0].parent.type === "PAGE" ? nodes[0].parent : null;
    for (const n of nodes) {
      let p = n.parent;
      while (p && p.type !== "PAGE") p = p.parent;
      if (!p) throw new Error(`create_component_set: node ${n.id} is not on any page`);
      if (firstPage && p !== firstPage) {
        throw new Error(`create_component_set: all components must be on the same page (${n.id} is on ${p.name}, expected ${firstPage.name})`);
      }
    }
    const parent = await getParent(parentId);
    const set = figma.combineAsVariants(nodes, parent);
    if (name) set.name = name;
    // combineAsVariants collapses children to (0,0). Spread them horizontally so
    // the set is actually inspectable in the file.
    const SPACING = 24;
    let xCursor = 0;
    for (const child of set.children) {
      child.x = xCursor;
      child.y = 0;
      xCursor += child.width + SPACING;
    }
    return {
      ...summarize(set),
      childCount: set.children.length,
      childIds: set.children.map((c) => c.id),
    };
  },

  // Set VARIANT-type properties on an instance. Rejects TEXT, BOOLEAN, and
  // INSTANCE_SWAP properties (those need the "<name>#<uniqueId>" key form that
  // Phase 03 doesn't support).
  async set_variant_property({ instanceId, properties }) {
    const node = await getNode(instanceId);
    if (node.type !== "INSTANCE") {
      throw new Error(`set_variant_property: node ${instanceId} is ${node.type}, not INSTANCE`);
    }
    if (!properties || typeof properties !== "object") {
      throw new Error("set_variant_property: properties must be an object");
    }
    const main = node.mainComponent;
    if (!main) throw new Error(`set_variant_property: instance ${instanceId} has no main component`);
    const set = main.parent;
    if (!set || set.type !== "COMPONENT_SET") {
      throw new Error(`set_variant_property: instance ${instanceId}'s main component is not inside a ComponentSet (standalone component — no variant properties to set)`);
    }
    const defs = set.componentPropertyDefinitions;
    for (const [key, value] of Object.entries(properties)) {
      const def = defs[key];
      if (!def) {
        throw new Error(`set_variant_property: unknown property "${key}". Available: ${Object.keys(defs).join(", ") || "(none)"}`);
      }
      if (def.type !== "VARIANT") {
        throw new Error(`set_variant_property: property "${key}" is ${def.type}, not VARIANT. This handler only supports VARIANT properties.`);
      }
      const options = def.variantOptions || [];
      if (!options.includes(String(value))) {
        throw new Error(`set_variant_property: invalid value "${value}" for "${key}". Valid options: ${options.join(", ")}`);
      }
    }
    node.setProperties(properties);
    return { ...summarize(node), appliedProperties: properties };
  },

  // List components on the current page (or all pages). When includeSets=true,
  // additionally emits a `componentSets` array where each entry carries its
  // variant children's ids + names — sync-manifest.js reads these names to
  // detect the file's variantNaming convention.
  async get_components({ allPages = false, includeSets = false }) {
    await figma.loadAllPagesAsync();
    const pages = allPages ? figma.root.children : [figma.currentPage];
    const components = [];
    const componentSets = [];
    const seenSets = new Set();
    for (const page of pages) {
      page.findAll((n) => {
        if (n.type === "COMPONENT") {
          components.push({
            id: n.id,
            name: n.name,
            pageId: page.id,
            pageName: page.name,
            componentSetId: n.parent && n.parent.type === "COMPONENT_SET" ? n.parent.id : null,
          });
          if (includeSets && n.parent && n.parent.type === "COMPONENT_SET" && !seenSets.has(n.parent.id)) {
            seenSets.add(n.parent.id);
            const set = n.parent;
            componentSets.push({
              id: set.id,
              name: set.name,
              pageId: page.id,
              pageName: page.name,
              variants: set.children
                .filter((c) => c.type === "COMPONENT")
                .map((c) => ({ id: c.id, name: c.name })),
            });
          }
        }
        return false;
      });
    }
    return includeSets ? { components, componentSets } : { components };
  },

  // ---------- read extensions (PHASE-11) ----------
  // Ported from gethopp/figma-mcp-bridge code.ts (MIT, Copyright 2026 GETHOPP LTD).
  // See plugin/NOTICE.md §PHASE-11 for full attribution.

  // Return all local variable collections with modes and resolved values.
  // On FigJam (or any environment where figma.variables is unavailable) returns
  // {collections: [], note: "..."} instead of throwing.
  async get_variable_defs() {
    function serializeVariableValue(value) {
      if (typeof value === "object" && value !== null) {
        if ("type" in value && value.type === "VARIABLE_ALIAS") {
          return { type: "VARIABLE_ALIAS", id: value.id };
        }
        if ("r" in value && "g" in value && "b" in value) {
          return { type: "COLOR", r: value.r, g: value.g, b: value.b, a: "a" in value ? value.a : 1 };
        }
      }
      return value;
    }
    try {
      const collections = await figma.variables.getLocalVariableCollectionsAsync();
      const variableData = await Promise.all(
        collections.map(async (collection) => {
          const variables = await Promise.all(
            collection.variableIds.map((id) => figma.variables.getVariableByIdAsync(id))
          );
          return {
            id: collection.id,
            name: collection.name,
            modes: collection.modes.map((mode) => ({ modeId: mode.modeId, name: mode.name })),
            variables: variables
              .filter((v) => v !== null)
              .map((variable) => ({
                id: variable.id,
                name: variable.name,
                resolvedType: variable.resolvedType,
                valuesByMode: Object.fromEntries(
                  Object.entries(variable.valuesByMode).map(([modeId, value]) => [
                    modeId,
                    serializeVariableValue(value),
                  ])
                ),
              })),
          };
        })
      );
      return { collections: variableData };
    } catch (e) {
      return { collections: [], note: "Variables are not available in this file type: " + (e && e.message ? e.message : String(e)) };
    }
  },

  // Return all local paint, text, effect, and grid styles. Each category is
  // wrapped in its own try/catch so one failing group doesn't break the others.
  async get_styles() {
    let paints = [], text = [], effects = [], grids = [];
    try { paints = (await figma.getLocalPaintStylesAsync()).map((s) => ({ id: s.id, name: s.name, paints: _serializePaints(s.paints) })); } catch { /* unavailable */ }
    try { text = (await figma.getLocalTextStylesAsync()).map((s) => ({ id: s.id, name: s.name, fontSize: s.fontSize, fontName: s.fontName, textDecoration: s.textDecoration, lineHeight: s.lineHeight, letterSpacing: s.letterSpacing })); } catch { /* unavailable */ }
    try { effects = (await figma.getLocalEffectStylesAsync()).map((s) => ({ id: s.id, name: s.name, effects: _serializeEffects(s.effects) })); } catch { /* unavailable */ }
    try { grids = (await figma.getLocalGridStylesAsync()).map((s) => ({ id: s.id, name: s.name, layoutGrids: s.layoutGrids })); } catch { /* unavailable */ }
    return { paints, text, effects, grids };
  },

  // Return a depth- and count-capped flat list of nodes with geometry only.
  // Ported from gethopp getLayoutTree() (code.ts:447-497). Hard depth cap 100.
  async get_layout_tree({ rootId, maxNodes = 2000 }) {
    const root = await figma.getNodeByIdAsync(rootId);
    if (!root || root.type === "DOCUMENT" || root.type === "PAGE") {
      throw new Error("Scene root required — provide a frame, group, or component nodeId");
    }
    const nodes = [];
    let truncated = false;
    function visit(node, depth) {
      if (nodes.length >= maxNodes || depth > 100) { truncated = true; return; }
      nodes.push({
        id: node.id,
        parentId: node.parent ? node.parent.id : null,
        name: node.name,
        type: node.type,
        visible: node.visible,
        localSize: { width: node.width, height: node.height },
        absoluteTransform: node.absoluteTransform,
        absoluteBoundingBox: node.absoluteBoundingBox,
        absoluteRenderBounds: "absoluteRenderBounds" in node ? node.absoluteRenderBounds : null,
        clipsContent: "clipsContent" in node ? node.clipsContent : false,
      });
      if ("children" in node) for (const child of node.children) visit(child, depth + 1);
    }
    visit(root, 0);
    return { schemaVersion: 1, rootId, truncated, nodes };
  },

  // Return a depth-truncated design context tree with a meta envelope.
  // Reuses summarize(node, 1, {lean:true}) per visited node for name+type+bbox+childCount.
  // depth=2 default. Targets current selection; falls back to current page.
  // meta.approxTokens uses char-count/4 heuristic (consistent with scripts/phase10/budget.py).
  async get_design_context({ depth = 2, nodeId = null } = {}) {
    let droppedChildren = 0;
    async function visitNode(node, currentDepth) {
      const base = await summarize(node, 0, { lean: true }); // name+type+bbox+childCount, no styles
      if (currentDepth >= depth) {
        // At or past depth limit: keep node summary but drop children, count them
        if ("children" in node) droppedChildren += node.children.length;
        return base;
      }
      if ("children" in node && node.children.length > 0) {
        base.children = await Promise.all(node.children.map((c) => visitNode(c, currentDepth + 1)));
      }
      return base;
    }

    let targets;
    if (nodeId) {
      const n = await figma.getNodeByIdAsync(nodeId);
      if (!n) throw new Error("Node not found: " + nodeId);
      targets = [n];
    } else if (figma.currentPage.selection.length > 0) {
      targets = Array.from(figma.currentPage.selection);
    } else {
      targets = [figma.currentPage];
    }

    const context = await Promise.all(targets.map((n) => visitNode(n, 0)));
    const json = JSON.stringify(context);
    const approxTokens = Math.ceil(json.length / 4);
    return {
      fileName: figma.root.name,
      currentPage: { id: figma.currentPage.id, name: figma.currentPage.name },
      selectionCount: figma.currentPage.selection.length,
      context,
      meta: { approxTokens, truncatedAtDepth: depth, droppedChildren },
    };
  },

  // Export a node in the requested format. PNG/JPG are auto-scaled; SVG/PDF
  // are vector and ignore scale. The server enforces savePath for SVG/PDF
  // before dispatching — but the plugin handles them cleanly if reached.
  // Backwards-compat: all existing callers that omit format/scale still work.
  async export_frame({ nodeId, maxDimension = 1024, format = "PNG", scale = 2 }) {
    const node = await getNode(nodeId);
    if (!("exportAsync" in node)) {
      throw new Error(`Node ${nodeId} (${node.type}) cannot be exported`);
    }
    if (node.width === 0 || node.height === 0) {
      throw new Error("Cannot export zero-size node");
    }
    if (node.visible === false) {
      throw new Error("Cannot export hidden node");
    }
    const fmt = (format || "PNG").toUpperCase();
    let bytes;
    let effectiveScale = 1;
    if (fmt === "SVG" || fmt === "PDF") {
      bytes = await node.exportAsync({ format: fmt });
    } else {
      // PNG or JPG: apply maxDimension auto-scaling; explicit scale overrides
      const longest = Math.max(node.width, node.height);
      const autoScale = longest > maxDimension ? maxDimension / longest : 1;
      effectiveScale = scale !== 2 ? scale : autoScale;
      bytes = await node.exportAsync({
        format: fmt === "JPG" ? "JPG" : "PNG",
        constraint: { type: "SCALE", value: effectiveScale },
      });
    }
    if (!bytes || bytes.length === 0) {
      throw new Error("exportAsync returned empty bytes — node may be off-canvas or clipped");
    }
    const base64 = figma.base64Encode(bytes);
    return {
      nodeId: node.id,
      width: Math.round(node.width * effectiveScale),
      height: Math.round(node.height * effectiveScale),
      format: fmt,
      base64,
    };
  },

  // ---------- PHASE-09 write-side setters ----------

  async set_node_visibility({ nodeId, nodeIds, visible }) {
    const ids = nodeIds ?? (nodeId ? [nodeId] : null);
    if (!ids || ids.length === 0) throw new Error("set_node_visibility: nodeId or nodeIds required");
    if (typeof visible !== "boolean") throw new Error("set_node_visibility: visible must be a boolean");
    const results = [];
    for (const id of ids) {
      const n = await getNode(id);
      if (!("visible" in n)) throw new Error(`Node ${id} (${n.type}) does not support visible`);
      n.visible = visible;
      results.push({ nodeId: n.id, name: n.name, visible: n.visible });
    }
    return ids.length === 1 ? results[0] : { results };
  },

  async set_stroke_properties({ nodeId, nodeIds, color, weight, strokeAlign, dashPattern, strokeCap, strokeJoin }) {
    const ids = nodeIds ?? (nodeId ? [nodeId] : null);
    if (!ids || ids.length === 0) throw new Error("set_stroke_properties: nodeId or nodeIds required");
    const VALID_ALIGN = ["INSIDE", "CENTER", "OUTSIDE"];
    if (strokeAlign != null && !VALID_ALIGN.includes(strokeAlign)) {
      throw new Error(`set_stroke_properties: strokeAlign must be one of ${VALID_ALIGN.join(", ")}`);
    }
    const results = [];
    for (const id of ids) {
      const n = await getNode(id);
      if (!("strokes" in n)) throw new Error(`Node ${id} (${n.type}) does not support strokes`);
      const applied = {};
      // Color: update the first stroke paint, or create one if none exist.
      if (color != null) {
        const paint = hexToPaint(color);
        const existing = Array.isArray(n.strokes) && typeof n.strokes !== "symbol" ? n.strokes : [];
        if (existing.length > 0) {
          const updated = existing.map((p, i) => i === 0 ? { ...p, ...paint } : p);
          n.strokes = updated;
        } else {
          n.strokes = [paint];
        }
        applied.color = color;
      }
      if (weight != null) {
        if (!("strokeWeight" in n)) throw new Error(`Node ${id} does not support strokeWeight`);
        n.strokeWeight = weight;
        applied.weight = weight;
      }
      if (strokeAlign != null) {
        if (!("strokeAlign" in n)) throw new Error(`Node ${id} does not support strokeAlign`);
        n.strokeAlign = strokeAlign;
        applied.strokeAlign = strokeAlign;
      }
      if (dashPattern != null) {
        if (!("dashPattern" in n)) throw new Error(`Node ${id} does not support dashPattern`);
        if (!Array.isArray(dashPattern)) throw new Error("set_stroke_properties: dashPattern must be an array");
        const pattern = dashPattern.map((v, i) => {
          if (typeof v !== "number" || v < 0) throw new Error(`dashPattern[${i}] must be a non-negative number`);
          return v;
        });
        n.dashPattern = pattern;
        applied.dashPattern = pattern;
      }
      if (strokeCap != null) {
        if (!("strokeCap" in n)) throw new Error(`Node ${id} does not support strokeCap`);
        n.strokeCap = strokeCap;
        applied.strokeCap = strokeCap;
      }
      if (strokeJoin != null) {
        if (!("strokeJoin" in n)) throw new Error(`Node ${id} does not support strokeJoin`);
        n.strokeJoin = strokeJoin;
        applied.strokeJoin = strokeJoin;
      }
      results.push({ nodeId: n.id, name: n.name, applied });
    }
    return ids.length === 1 ? results[0] : { results };
  },

  async set_gradient_fill({ nodeId, nodeIds, gradientType = "LINEAR", gradientStops, gradientTransform, opacity, target = "fill" }) {
    const ids = nodeIds ?? (nodeId ? [nodeId] : null);
    if (!ids || ids.length === 0) throw new Error("set_gradient_fill: nodeId or nodeIds required");
    const paintType = `GRADIENT_${gradientType}`;
    const VALID_TYPES = ["GRADIENT_LINEAR", "GRADIENT_RADIAL", "GRADIENT_ANGULAR", "GRADIENT_DIAMOND"];
    if (!VALID_TYPES.includes(paintType)) {
      throw new Error(`set_gradient_fill: gradientType must be one of LINEAR, RADIAL, ANGULAR, DIAMOND`);
    }
    if (!Array.isArray(gradientStops) || gradientStops.length < 2) {
      throw new Error("set_gradient_fill: gradientStops must be an array with at least 2 entries");
    }
    // Build gradient stops — each stop: {color: hex, opacity?, position}
    const stops = gradientStops.map((s, i) => {
      if (typeof s.position !== "number") throw new Error(`gradientStops[${i}].position must be a number`);
      let h = String(s.color || "#000000").replace("#", "").trim();
      if (h.length === 3) h = h.split("").map((c) => c + c).join("");
      if (!/^[0-9a-fA-F]{6}$/.test(h)) throw new Error(`gradientStops[${i}].color is not a valid hex: ${s.color}`);
      const r = parseInt(h.slice(0, 2), 16) / 255;
      const g = parseInt(h.slice(2, 4), 16) / 255;
      const b = parseInt(h.slice(4, 6), 16) / 255;
      const a = typeof s.opacity === "number" ? s.opacity : 1;
      return { color: { r, g, b, a }, position: s.position };
    });
    // gradientTransform: caller passes [[a,b,c],[d,e,f]] (2×3). Figma expects that exact shape.
    // Default: identity transform (horizontal left-to-right gradient).
    const transform = Array.isArray(gradientTransform) && gradientTransform.length === 2
      ? gradientTransform
      : [[1, 0, 0], [0, 1, 0]];
    const paint = {
      type: paintType,
      gradientStops: stops,
      gradientTransform: transform,
    };
    if (typeof opacity === "number") paint.opacity = opacity;
    const results = [];
    for (const id of ids) {
      const n = await getNode(id);
      if (target === "stroke") {
        if (!("strokes" in n)) throw new Error(`Node ${id} (${n.type}) does not support strokes`);
        n.strokes = [paint];
      } else {
        if (!("fills" in n)) throw new Error(`Node ${id} (${n.type}) does not support fills`);
        n.fills = [paint];
      }
      results.push({ nodeId: n.id, name: n.name, applied: { target, gradientType: paintType, stops: stops.length } });
    }
    return ids.length === 1 ? results[0] : { results };
  },

  async set_effects({ nodeId, nodeIds, effects }) {
    const ids = nodeIds ?? (nodeId ? [nodeId] : null);
    if (!ids || ids.length === 0) throw new Error("set_effects: nodeId or nodeIds required");
    if (!Array.isArray(effects)) throw new Error("set_effects: effects must be an array (pass [] to clear)");
    const built = effects.map((raw, i) => {
      const type = raw.type;
      if (type === "DROP_SHADOW" || type === "INNER_SHADOW") {
        if (typeof raw.color !== "string") throw new Error(`effects[${i}].color must be a hex string`);
        const offset = raw.offset;
        if (!offset || typeof offset.x !== "number" || typeof offset.y !== "number") {
          throw new Error(`effects[${i}].offset must be {x, y} numbers`);
        }
        if (typeof raw.radius !== "number") throw new Error(`effects[${i}].radius must be a number`);
        let h = String(raw.color).replace("#", "").trim();
        if (h.length === 3) h = h.split("").map((c) => c + c).join("");
        if (!/^[0-9a-fA-F]{6}$/.test(h)) throw new Error(`effects[${i}].color is not a valid hex: ${raw.color}`);
        const r = parseInt(h.slice(0, 2), 16) / 255;
        const g = parseInt(h.slice(2, 4), 16) / 255;
        const b = parseInt(h.slice(4, 6), 16) / 255;
        const a = typeof raw.opacity === "number" ? raw.opacity : 1;
        return {
          type,
          color: { r, g, b, a },
          offset: { x: offset.x, y: offset.y },
          radius: raw.radius,
          spread: typeof raw.spread === "number" ? raw.spread : 0,
          visible: raw.visible === undefined ? true : Boolean(raw.visible),
          blendMode: typeof raw.blendMode === "string" ? raw.blendMode : "NORMAL",
        };
      }
      if (type === "LAYER_BLUR" || type === "BACKGROUND_BLUR") {
        if (typeof raw.radius !== "number") throw new Error(`effects[${i}].radius must be a number`);
        return {
          type,
          radius: raw.radius,
          visible: raw.visible === undefined ? true : Boolean(raw.visible),
        };
      }
      throw new Error(`effects[${i}]: unsupported type "${type}" (allowed: DROP_SHADOW, INNER_SHADOW, LAYER_BLUR, BACKGROUND_BLUR)`);
    });
    const results = [];
    for (const id of ids) {
      const n = await getNode(id);
      if (!("effects" in n)) throw new Error(`Node ${id} (${n.type}) does not support effects`);
      n.effects = built;
      results.push({ nodeId: n.id, name: n.name, applied: { count: built.length } });
    }
    return ids.length === 1 ? results[0] : { results };
  },

  async create_svg({ svg, x, y, parentId, name }) {
    if (typeof svg !== "string" || !svg.trim()) throw new Error("create_svg: svg must be a non-empty string");
    let node;
    try {
      node = figma.createNodeFromSvg(svg);
    } catch (e) {
      throw new Error(`create_svg: SVG parse error — ${e && e.message ? e.message : String(e)} (first 200 chars: ${svg.slice(0, 200)})`);
    }
    if (name) node.name = name;
    const parent = await getParent(parentId);
    parent.appendChild(node);
    if (!("layoutMode" in parent) || parent.layoutMode === "NONE") {
      node.x = x ?? 0;
      node.y = y ?? 0;
    }
    return await summarize(node);
  },

  // Execute an ordered array of commands in a single plugin invocation.
  // Each command: { command, params?, _ref? }. Strings matching "_ref:<alias>" in
  // params are rewritten to the real node ID returned by the aliased command.
  // Failure policy: sequential-best-effort. Aborts if any single command type
  // fails more than 5 times (threshold from mcp-note.md).
  async run_batch({ commands }) {
    if (!Array.isArray(commands)) throw new Error("run_batch: `commands` must be an array");
    const refMap = {};
    const results = [];
    const failuresByType = {};
    const FAIL_THRESHOLD = 5;
    let aborted = false;
    let abortReason = null;

    const resolveRefs = (value) => {
      if (value == null) return value;
      if (typeof value === "string") {
        if (value.startsWith("_ref:")) {
          const alias = value.slice(5);
          if (!(alias in refMap)) {
            throw new Error(`Unresolved ref: _ref:${alias} (alias not yet defined)`);
          }
          return refMap[alias];
        }
        return value;
      }
      if (Array.isArray(value)) return value.map(resolveRefs);
      if (typeof value === "object") {
        const out = {};
        for (const k of Object.keys(value)) out[k] = resolveRefs(value[k]);
        return out;
      }
      return value;
    };

    for (let i = 0; i < commands.length; i++) {
      const cmd = commands[i];
      const entry = { index: i, command: cmd.command, _ref: cmd._ref };
      const handler = handlers[cmd.command];
      if (!handler) {
        entry.error = `Unknown command: ${cmd.command}`;
        results.push(entry);
        failuresByType[cmd.command] = (failuresByType[cmd.command] || 0) + 1;
        if (failuresByType[cmd.command] > FAIL_THRESHOLD) {
          aborted = true;
          abortReason = `command type "${cmd.command}" failed ${failuresByType[cmd.command]} times`;
          break;
        }
        continue;
      }
      try {
        const resolvedParams = resolveRefs(cmd.params || {});
        const result = await handler(resolvedParams);
        entry.result = result;
        if (cmd._ref) {
          if (result && typeof result.id === "string") {
            refMap[cmd._ref] = result.id;
          } else {
            entry.warning = `Command had _ref "${cmd._ref}" but result has no id to alias`;
          }
        }
        results.push(entry);
      } catch (e) {
        entry.error = e && e.message ? e.message : String(e);
        results.push(entry);
        failuresByType[cmd.command] = (failuresByType[cmd.command] || 0) + 1;
        if (failuresByType[cmd.command] > FAIL_THRESHOLD) {
          aborted = true;
          abortReason = `command type "${cmd.command}" failed ${failuresByType[cmd.command]} times`;
          break;
        }
      }
    }

    return { results, aborted, reason: abortReason, refMap };
  },
};

figma.ui.onmessage = async (msg) => {
  if (msg.type !== "command") return;
  const { id, command, params } = msg.payload;
  const handler = handlers[command];
  try {
    if (!handler) throw new Error(`Unknown command: ${command}`);
    const result = await handler(params || {});
    figma.ui.postMessage({ type: "result", payload: { id, result } });
  } catch (e) {
    figma.ui.postMessage({ type: "result", payload: { id, error: e.message || String(e) } });
  }
};
