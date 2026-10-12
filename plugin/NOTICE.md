# Third-Party Notices

## gethopp/figma-mcp-bridge — serializer.ts

The serializer logic in `plugin/code.js` (`summarize()` and its helpers
`_serializePaints`, `_serializeEffects`, `_serializeStyles`) is a port of
`figma-mcp-bridge/plugin/src/main/serializer.ts` from the gethopp project.

```
MIT License

Copyright (c) 2026 GETHOPP LTD

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Modifications from the original

- Translated from TypeScript to vanilla JavaScript (no build step).
- All existing flat fields from the pre-PHASE-08 `summarize()` are preserved
  alongside the new `styles{}` block for backwards compatibility.
- Added `opts.lean` (omit `styles` block for tree scans) and
  `opts.includeGeometry` (include `vectorPaths` on VECTOR nodes).
- Added extras not in the original: `mainComponentId` on INSTANCE nodes,
  `pointCount` and `innerRadius` on STAR nodes, `vectorPaths` on VECTOR nodes.
- `starAngle` was omitted: it is not exposed by the Figma Plugin API.
- `mainComponentId` uses `node.mainComponent?.id` (synchronous; can be null
  if the source component was deleted).
- `vectorPaths` is read synchronously from `node.vectorPaths` (not via
  `getFillGeometryAsync`, which returns fill geometry, not path data).
- Invisible effects are filtered out (matching gethopp behaviour).
- Invisible fills/strokes are filtered from `styles.fills`/`styles.strokes`
  (gethopp behaviour); the legacy flat `fill` field retains the old behaviour
  of returning the first SOLID regardless of visibility.

---

## gethopp/figma-mcp-bridge — code.ts (PHASE-11 read extensions)

The handlers `get_variable_defs`, `get_styles`, `get_layout_tree`, `get_design_context`,
and the `export_frame` format/scale extension in `plugin/code.js` are ported from
`figma-mcp-bridge/plugin/src/main/code.ts` from the gethopp project
(MIT License, Copyright 2026 GETHOPP LTD — same licence text as above).

Source line ranges ported:
- `get_layout_tree` — lines 447–497
- `get_styles` — lines 571–657
- `get_design_context` — lines 675–730
- `get_variable_defs` — lines 732–820
- `export_frame` format/scale — lines 821–887 (gethopp calls this `get_screenshot`)

### Modifications from the original

- Translated from TypeScript to vanilla JavaScript (no build step).
- `get_design_context` uses our PHASE-08 `summarize(node, 0, {lean:true})` instead
  of gethopp's `serializeNode`; adds a `meta: {approxTokens, truncatedAtDepth, droppedChildren}`
  envelope for budget planning (consistent with `scripts/phase10/budget.py` heuristic).
- `get_styles` paint and effect fields are serialized through `_serializePaints` /
  `_serializeEffects` (our PHASE-08 helpers) rather than raw Figma objects, so the
  returned shapes are consistent with what `get_node` returns.
- `get_variable_defs` wraps the entire call in a single try/catch (instead of a
  FigJam branch) and returns `{collections: [], note: "..."}` on any API error.
- `get_layout_tree` strips gethopp's `capture` / `snapshotId` / `atomicWithScreenshot`
  fields from the return value — those are gethopp-server metadata, not needed here.
- `export_frame` (our existing tool) is extended with `format` and `scale` params
  using gethopp's branch logic; the auto-scale from `maxDimension` is preserved as
  the default scale calculation for PNG/JPG, overridden when an explicit `scale` is
  provided. SVG/PDF export the node with no constraint (vector formats).
- `serializeVariableValue` is inlined into `get_variable_defs` (was a module-level
  helper in the original).
