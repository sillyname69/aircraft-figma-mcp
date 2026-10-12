#!/usr/bin/env python3
"""extract-geometry.py — deterministic CV pre-pass.

Reads a reference image, segments it into sections -> columns -> cells using
luminance-variance whitespace detection + contour grouping. Emits a schema-valid
layout-spec JSON with role="unknown" everywhere and `_confidence` / `_source`
tags on every bbox. Idempotent: same bytes in -> byte-identical bytes out.

Usage:
  python3 scripts/extract-geometry.py <image> <out.json> [--no-roles]

Dependencies: opencv-python, numpy. pytesseract is optional (OCR hints).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

try:
    import pytesseract
    _HAVE_PYTESSERACT = True
except Exception:
    _HAVE_PYTESSERACT = False


# ---------------------------------------------------------------------------
# Tuneables (deterministic constants — do not read from env / argv)
# ---------------------------------------------------------------------------
MIN_BAND_PX = 24          # whitespace band length to split sections
MIN_COL_BAND_PX = 20      # vertical whitespace band length to split columns
VAR_LOW = 60.0            # per-row/col variance threshold (grayscale 0..255)
MIN_SECTION_H = 60        # reject sections thinner than this
MIN_COLUMN_W = 60         # reject columns thinner than this
MIN_CELL_AREA = 400       # px^2
MIN_SECTIONS = 2          # fail-loud bound (per spec)
MAX_SECTIONS = 20
CONTOUR_PAD = 4           # morphology kernel for contour grouping


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Bbox:
    x: int
    y: int
    w: int
    h: int
    confidence: float
    source: str

    def to_dict(self) -> dict:
        d = {"x": int(self.x), "y": int(self.y), "w": int(self.w), "h": int(self.h)}
        d["_confidence"] = round(float(self.confidence), 3)
        d["_source"] = self.source
        return d


@dataclass
class Cell:
    bbox: Bbox
    role: str = "unknown"


@dataclass
class Column:
    bbox: Bbox
    cells: List[Cell] = field(default_factory=list)


@dataclass
class Section:
    id: str
    bbox: Bbox
    columns: List[Column] = field(default_factory=list)
    background: Optional[dict] = None
    role: str = "unknown"


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------
def load_grayscale(path: str) -> Tuple[np.ndarray, np.ndarray]:
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"could not read image: {path}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img, gray


def find_whitespace_bands(variance: np.ndarray, min_band: int, thresh: float) -> List[Tuple[int, int]]:
    """Return list of (start, end) spans where variance < thresh for at least `min_band` samples."""
    low = variance < thresh
    bands: List[Tuple[int, int]] = []
    run_start: Optional[int] = None
    for i, is_low in enumerate(low):
        if is_low and run_start is None:
            run_start = i
        elif (not is_low) and run_start is not None:
            if i - run_start >= min_band:
                bands.append((run_start, i))
            run_start = None
    if run_start is not None and len(low) - run_start >= min_band:
        bands.append((run_start, len(low)))
    return bands


def _confidence_from_variance(var_slice_before: float, var_slice_after: float) -> float:
    """Confidence proxy: higher variance delta at the boundary -> higher confidence."""
    delta = abs(var_slice_before - var_slice_after)
    # Normalize: 0 -> 0.3 (bare minimum), 200+ -> 1.0
    return float(max(0.3, min(1.0, 0.3 + delta / 200.0)))


def segment_sections(gray: np.ndarray) -> List[Section]:
    """Step 2: per-row variance -> section bands between whitespace."""
    h, w = gray.shape
    row_var = gray.var(axis=1)  # shape (h,)
    bands = find_whitespace_bands(row_var, MIN_BAND_PX, VAR_LOW)

    # Convert whitespace bands to content spans (the gaps between whitespace).
    boundaries = [0]
    for (bs, be) in bands:
        boundaries.append(bs)
        boundaries.append(be)
    boundaries.append(h)
    # Pair-wise: (end-of-whitespace, start-of-next-whitespace) = content span.
    spans: List[Tuple[int, int, float]] = []
    # Walk pairs: even indices are starts, odd indices are ends of content.
    i = 0
    while i < len(boundaries) - 1:
        s = boundaries[i]
        e = boundaries[i + 1]
        if e - s >= MIN_SECTION_H:
            # Confidence: compare variance just inside vs. just outside the span.
            var_in = float(row_var[s:e].mean()) if e > s else 0.0
            var_edge = 0.0
            if s > 0:
                var_edge = float(row_var[max(0, s - MIN_BAND_PX):s].mean())
            conf = _confidence_from_variance(var_in, var_edge)
            spans.append((s, e, conf))
        i += 2  # jump past the whitespace band

    sections: List[Section] = []
    for idx, (s, e, conf) in enumerate(spans):
        bbox = Bbox(x=0, y=int(s), w=int(w), h=int(e - s),
                    confidence=conf, source="whitespace")
        sections.append(Section(id=f"section-{idx + 1:02d}", bbox=bbox))
    return sections


def segment_columns(gray: np.ndarray, section: Section) -> None:
    """Step 3: per-column variance within the section's row range."""
    y0, y1 = section.bbox.y, section.bbox.y + section.bbox.h
    strip = gray[y0:y1, :]
    col_var = strip.var(axis=0)
    bands = find_whitespace_bands(col_var, MIN_COL_BAND_PX, VAR_LOW)

    w = gray.shape[1]
    boundaries = [0]
    for (bs, be) in bands:
        boundaries.append(bs)
        boundaries.append(be)
    boundaries.append(w)

    spans: List[Tuple[int, int, float]] = []
    i = 0
    while i < len(boundaries) - 1:
        s = boundaries[i]
        e = boundaries[i + 1]
        if e - s >= MIN_COLUMN_W:
            var_in = float(col_var[s:e].mean()) if e > s else 0.0
            var_edge = float(col_var[max(0, s - MIN_COL_BAND_PX):s].mean()) if s > 0 else 0.0
            conf = _confidence_from_variance(var_in, var_edge)
            spans.append((s, e, conf))
        i += 2

    # Fallback: if no column splits were found, treat the whole section as one column.
    if not spans:
        spans = [(0, w, 0.5)]

    for (s, e, conf) in spans:
        bbox = Bbox(x=int(s), y=int(y0), w=int(e - s), h=int(y1 - y0),
                    confidence=conf, source="whitespace")
        section.columns.append(Column(bbox=bbox))


def extract_cells(gray: np.ndarray, column: Column) -> None:
    """Step 4: contour-based cell extraction within a column."""
    x0, y0 = column.bbox.x, column.bbox.y
    x1, y1 = x0 + column.bbox.w, y0 + column.bbox.h
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return

    # Adaptive threshold -> morphology to merge nearby strokes -> contours.
    try:
        thresh = cv2.adaptiveThreshold(
            crop, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 7)
    except cv2.error:
        return
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (CONTOUR_PAD * 4, CONTOUR_PAD * 2))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    rects: List[Tuple[int, int, int, int]] = []
    for ct in contours:
        x, y, w, h = cv2.boundingRect(ct)
        if w * h < MIN_CELL_AREA:
            continue
        # Reject near-full-column rects (would just duplicate the column)
        if w >= column.bbox.w * 0.98 and h >= column.bbox.h * 0.98:
            continue
        rects.append((x, y, w, h))

    # Deduplicate by sorting + collapsing near-identical rects.
    rects.sort(key=lambda r: (r[1], r[0]))
    merged: List[Tuple[int, int, int, int]] = []
    for r in rects:
        if any(_rect_similar(r, m) for m in merged):
            continue
        merged.append(r)

    for (x, y, w, h) in merged:
        bbox = Bbox(
            x=int(x + x0),
            y=int(y + y0),
            w=int(w),
            h=int(h),
            confidence=0.6,
            source="contour",
        )
        column.cells.append(Cell(bbox=bbox))


def _rect_similar(a, b, tol=4):
    return (abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol
            and abs(a[2] - b[2]) <= tol and abs(a[3] - b[3]) <= tol)


def clip_cells_to_column(column: Column) -> None:
    """Clamp cell bboxes to lie within their column (defensive)."""
    cx0, cy0 = column.bbox.x, column.bbox.y
    cx1, cy1 = cx0 + column.bbox.w, cy0 + column.bbox.h
    for cell in column.cells:
        x0 = max(cx0, cell.bbox.x)
        y0 = max(cy0, cell.bbox.y)
        x1 = min(cx1, cell.bbox.x + cell.bbox.w)
        y1 = min(cy1, cell.bbox.y + cell.bbox.h)
        if x1 - x0 < 1 or y1 - y0 < 1:
            cell.bbox.w = 0
            cell.bbox.h = 0
            continue
        cell.bbox.x = x0
        cell.bbox.y = y0
        cell.bbox.w = x1 - x0
        cell.bbox.h = y1 - y0
    column.cells = [c for c in column.cells if c.bbox.w > 0 and c.bbox.h > 0]


def sample_background(bgr: np.ndarray, bbox: Bbox) -> Optional[dict]:
    """Sample a thin strip at the top of the section for a background hex."""
    y0, y1 = bbox.y, min(bbox.y + max(1, bbox.h // 20), bgr.shape[0])
    x0, x1 = bbox.x, min(bbox.x + bbox.w, bgr.shape[1])
    strip = bgr[y0:y1, x0:x1]
    if strip.size == 0:
        return None
    # BGR mean -> RGB hex
    b, g, r = strip.mean(axis=(0, 1))[:3]
    return {"kind": "solid", "approxHex": f"#{int(r):02X}{int(g):02X}{int(b):02X}"}


def _collect_ocr_words(gray: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Return a list of (x, y, w, h) word bboxes from tesseract. Empty if OCR
    unavailable or no text found."""
    if not _HAVE_PYTESSERACT:
        return []
    try:
        data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)
    except Exception:
        return []
    rects: List[Tuple[int, int, int, int]] = []
    for i, txt in enumerate(data.get("text", [])):
        if not txt or not txt.strip():
            continue
        try:
            conf = int(float(data["conf"][i]))
        except (ValueError, TypeError):
            conf = -1
        if conf < 30:
            continue
        rects.append((
            int(data["left"][i]),
            int(data["top"][i]),
            int(data["width"][i]),
            int(data["height"][i]),
        ))
    return rects


# Decision: contour cells that look like buttons / images / icons / dividers
# are preserved during the OCR merge even if OCR text overlaps them. OCR text
# inside a button is a label, not a paragraph (designer call 2026-10-05 B.i).
def _is_non_text_shape(bbox: Bbox) -> bool:
    w, h = bbox.w, bbox.h
    if w <= 0 or h <= 0:
        return False
    # Tall rectangle: likely image slot.
    if h >= 2 * w and h >= 60:
        return True
    # Near-square with meaningful area: likely icon tile.
    if 0.75 <= w / max(h, 1) <= 1.33 and max(w, h) >= 40 and w * h >= 1600:
        return True
    # Wide short rectangle with meaningful area: likely button or divider.
    if w >= 3 * h and h <= 40 and w * h >= 2000:
        return True
    return False


# Cluster OCR word rects into paragraphs:
#  1. Group words into lines (vertical-centre tolerance = 0.6 × median word height).
#  2. Group lines into paragraphs (line gap < 1.5 × median line height — designer call A).
#  3. Each paragraph -> union bbox of its words.
#
# Returns paragraph rects (x, y, w, h) sorted top-to-bottom.
PARA_LINE_GAP_FACTOR = 1.5  # designer-approved 2026-10-05


def _cluster_words_to_paragraphs(words: List[Tuple[int, int, int, int]]) -> List[Tuple[int, int, int, int]]:
    if not words:
        return []
    heights = [h for (_, _, _, h) in words]
    median_h = sorted(heights)[len(heights) // 2] or 1

    # Step 1: group into lines by vertical centre.
    words_sorted = sorted(words, key=lambda r: (r[1], r[0]))
    lines: List[List[Tuple[int, int, int, int]]] = []
    line_tol = max(3, int(median_h * 0.6))
    for word in words_sorted:
        wx, wy, ww, wh = word
        wcy = wy + wh // 2
        placed = False
        for line in lines:
            line_cy = sum((y + h // 2) for (_, y, _, h) in line) // len(line)
            if abs(line_cy - wcy) <= line_tol:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])

    # Step 2: compute line bboxes + heights, group lines into paragraphs by vertical gap.
    def union(rects):
        xs = [r[0] for r in rects]; ys = [r[1] for r in rects]
        xe = [r[0] + r[2] for r in rects]; ye = [r[1] + r[3] for r in rects]
        return (min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))

    line_rects = [union(line) for line in lines]
    line_rects.sort(key=lambda r: r[1])
    line_heights = [h for (_, _, _, h) in line_rects]
    median_line_h = sorted(line_heights)[len(line_heights) // 2] or median_h

    paragraphs: List[List[Tuple[int, int, int, int]]] = []
    for lr in line_rects:
        if not paragraphs:
            paragraphs.append([lr])
            continue
        last = paragraphs[-1][-1]
        gap = lr[1] - (last[1] + last[3])
        if gap <= PARA_LINE_GAP_FACTOR * median_line_h:
            paragraphs[-1].append(lr)
        else:
            paragraphs.append([lr])

    return [union(para) for para in paragraphs]


def merge_text_cells(gray: np.ndarray, sections: List[Section]) -> int:
    """Replace small text-shaped contour cells with paragraph-sized cells
    derived from OCR word clustering. Returns the net cell delta
    (positive = cells added, negative = cells removed)."""
    words = _collect_ocr_words(gray)
    if not words:
        return 0
    paragraphs = _cluster_words_to_paragraphs(words)
    if not paragraphs:
        return 0

    original_cell_count = sum(len(c.cells) for s in sections for c in s.columns)

    for section in sections:
        for column in section.columns:
            # Paragraphs that fall inside this column.
            cbox = (column.bbox.x, column.bbox.y, column.bbox.w, column.bbox.h)
            local_paragraphs: List[Tuple[int, int, int, int]] = []
            for p in paragraphs:
                # Keep paragraph if its centre is inside the column.
                pcx = p[0] + p[2] // 2
                pcy = p[1] + p[3] // 2
                if (column.bbox.x <= pcx <= column.bbox.x + column.bbox.w
                        and column.bbox.y <= pcy <= column.bbox.y + column.bbox.h):
                    # Clip paragraph to column.
                    x0 = max(cbox[0], p[0])
                    y0 = max(cbox[1], p[1])
                    x1 = min(cbox[0] + cbox[2], p[0] + p[2])
                    y1 = min(cbox[1] + cbox[3], p[1] + p[3])
                    if x1 > x0 and y1 > y0:
                        local_paragraphs.append((x0, y0, x1 - x0, y1 - y0))
            if not local_paragraphs:
                continue

            # Partition existing cells: keep non-text-shaped as-is; remove any
            # text-shaped cell whose centre falls inside a paragraph.
            kept: List[Cell] = []
            for cell in column.cells:
                if _is_non_text_shape(cell.bbox):
                    kept.append(cell)
                    continue
                ccx = cell.bbox.x + cell.bbox.w // 2
                ccy = cell.bbox.y + cell.bbox.h // 2
                in_para = any(
                    px <= ccx <= px + pw and py <= ccy <= py + ph
                    for (px, py, pw, ph) in local_paragraphs
                )
                if not in_para:
                    kept.append(cell)

            # Add one cell per paragraph.
            for (px, py, pw, ph) in local_paragraphs:
                kept.append(Cell(bbox=Bbox(
                    x=int(px), y=int(py), w=int(pw), h=int(ph),
                    confidence=0.85,
                    source="ocr-paragraph",
                )))

            # Re-sort cells top-to-bottom, then left-to-right.
            kept.sort(key=lambda c: (c.bbox.y, c.bbox.x))
            column.cells = kept

    final_cell_count = sum(len(c.cells) for s in sections for c in s.columns)
    return final_cell_count - original_cell_count


def ocr_hint_cells(gray: np.ndarray, sections: List[Section]) -> None:
    """OCR pass: mark cells overlapping text with `_source` += ',ocr'. Does not
    tag cells already sourced as 'ocr-paragraph' (they came from OCR directly)."""
    if not _HAVE_PYTESSERACT:
        return
    text_rects = _collect_ocr_words(gray)
    if not text_rects:
        return
    for section in sections:
        for column in section.columns:
            for cell in column.cells:
                if "ocr" in cell.bbox.source:
                    continue
                if any(_rect_overlaps(
                        (cell.bbox.x, cell.bbox.y, cell.bbox.w, cell.bbox.h), tr
                ) for tr in text_rects):
                    cell.bbox.source = f"{cell.bbox.source},ocr"


def _rect_overlaps(a, b):
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    return not (ax0 + aw <= bx0 or bx0 + bw <= ax0
                or ay0 + ah <= by0 or by0 + bh <= ay0)


# ---------------------------------------------------------------------------
# Validation (self-contained — mirror of validate-spec.js)
# ---------------------------------------------------------------------------
ROLES = {
    "nav", "heading_block", "body_text", "cta_button", "image_slot",
    "icon_tile", "stat_cell", "card_row", "pricing_card", "feature_row",
    "footer", "divider", "hero", "unknown",
}


def validate(spec: dict) -> List[str]:
    errors: List[str] = []
    if not isinstance(spec.get("canvas"), dict):
        errors.append("missing canvas")
    else:
        for k in ("width", "height"):
            v = spec["canvas"].get(k)
            if not isinstance(v, int) or v < 1:
                errors.append(f"canvas.{k} must be positive int")
    sections = spec.get("sections")
    if not isinstance(sections, list) or len(sections) < MIN_SECTIONS:
        errors.append(f"sections < {MIN_SECTIONS} (got {len(sections) if isinstance(sections, list) else 0})")
        return errors
    if len(sections) > MAX_SECTIONS:
        errors.append(f"sections > {MAX_SECTIONS} (got {len(sections)})")
    canvas_w = spec["canvas"].get("width", 0)
    canvas_h = spec["canvas"].get("height", 0)
    for si, s in enumerate(sections):
        path = f"sections[{si}]"
        for key in ("id", "bbox", "role", "columns"):
            if key not in s:
                errors.append(f"{path}.{key} missing")
        bb = s.get("bbox") or {}
        if bb.get("w", 0) < 1 or bb.get("h", 0) < 1:
            errors.append(f"{path}.bbox has non-positive dim")
        if bb.get("x", 0) < 0 or bb.get("y", 0) < 0:
            errors.append(f"{path}.bbox has negative origin")
        if bb.get("x", 0) + bb.get("w", 0) > canvas_w + 1:
            errors.append(f"{path}.bbox exceeds canvas width")
        if bb.get("y", 0) + bb.get("h", 0) > canvas_h + 1:
            errors.append(f"{path}.bbox exceeds canvas height")
        if s.get("role") not in ROLES:
            errors.append(f"{path}.role invalid")
        for ci, c in enumerate(s.get("columns", [])):
            cpath = f"{path}.columns[{ci}]"
            cb = c.get("bbox") or {}
            if cb.get("w", 0) < 1 or cb.get("h", 0) < 1:
                errors.append(f"{cpath}.bbox has non-positive dim")
            if not _contains(bb, cb):
                errors.append(f"{cpath}.bbox exceeds section")
            for kk, cell in enumerate(c.get("cells", [])):
                kpath = f"{cpath}.cells[{kk}]"
                kb = cell.get("bbox") or {}
                if kb.get("w", 0) < 1 or kb.get("h", 0) < 1:
                    errors.append(f"{kpath}.bbox has non-positive dim")
                if not _contains(cb, kb):
                    errors.append(f"{kpath}.bbox exceeds column")
                if cell.get("role") not in ROLES:
                    errors.append(f"{kpath}.role invalid")
    return errors


def _contains(outer: dict, inner: dict, tol: int = 1) -> bool:
    return (inner.get("x", 0) >= outer.get("x", 0) - tol
            and inner.get("y", 0) >= outer.get("y", 0) - tol
            and inner.get("x", 0) + inner.get("w", 0) <= outer.get("x", 0) + outer.get("w", 0) + tol
            and inner.get("y", 0) + inner.get("h", 0) <= outer.get("y", 0) + outer.get("h", 0) + tol)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_spec(image_path: str) -> dict:
    bgr, gray = load_grayscale(image_path)
    h, w = gray.shape

    sections = segment_sections(gray)
    for sec in sections:
        segment_columns(gray, sec)
        for col in sec.columns:
            extract_cells(gray, col)
            clip_cells_to_column(col)
        sec.background = sample_background(bgr, sec.bbox)

    # OCR paragraph merge: replace thin text contours with paragraph-sized
    # cells derived from word clustering. Runs before ocr_hint_cells so the
    # downstream subagent-labeling path sees fewer, bigger text blocks.
    merge_delta = merge_text_cells(gray, sections)

    ocr_hint_cells(gray, sections)

    warnings: List[dict] = []
    if not _HAVE_PYTESSERACT:
        warnings.append({
            "kind": "optional_dep_missing",
            "note": "pytesseract not available; cells have no OCR hints and no paragraph merge",
        })
    if _HAVE_PYTESSERACT and merge_delta != 0:
        warnings.append({
            "kind": "merge_text_cells_applied",
            "note": f"OCR paragraph merge changed cell count by {merge_delta:+d}",
        })

    spec = {
        "canvas": {"width": int(w), "height": int(h)},
        "sections": [_section_to_dict(s) for s in sections],
        "warnings": warnings,
    }
    return spec


def _section_to_dict(section: Section) -> dict:
    return {
        "id": section.id,
        "bbox": section.bbox.to_dict(),
        "role": section.role,
        "background": section.background,
        "columns": [
            {
                "bbox": col.bbox.to_dict(),
                "cells": [
                    {"bbox": cell.bbox.to_dict(), "role": cell.role}
                    for cell in col.cells
                ],
            }
            for col in section.columns
        ],
    }


def main() -> int:
    args = sys.argv[1:]
    no_roles = False
    positional: List[str] = []
    for a in args:
        if a == "--no-roles":
            no_roles = True
        else:
            positional.append(a)
    if len(positional) != 2:
        sys.stderr.write("usage: extract-geometry.py <image> <out.json> [--no-roles]\n")
        return 2
    image_path, out_path = positional

    try:
        spec = build_spec(image_path)
    except Exception as e:
        sys.stderr.write(f"extract-geometry: {e}\n")
        return 1

    errors = validate(spec)
    if errors:
        spec["warnings"].extend({"kind": "validation_error", "note": e} for e in errors)
        sys.stderr.write(f"extract-geometry: {len(errors)} validation error(s)\n")
        for e in errors:
            sys.stderr.write(f"  {e}\n")
        # Still write the file for debugging, but exit non-zero.
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, sort_keys=False)
        return 1

    if no_roles:
        spec["_mode"] = "no-roles"

    # Deterministic serialization: fixed key order via sort_keys=False + manual
    # order already enforced by dict construction. Pretty-print with 2-space
    # indent for byte-identical output across runs.
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, sort_keys=False)
        f.write("\n")

    sys.stderr.write(
        f"extract-geometry: {len(spec['sections'])} sections, "
        f"{sum(len(s['columns']) for s in spec['sections'])} columns, "
        f"{sum(len(c['cells']) for s in spec['sections'] for c in s['columns'])} cells "
        f"-> {out_path}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
