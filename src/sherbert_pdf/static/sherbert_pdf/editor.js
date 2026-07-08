/* Sherbert PDF annotation editor (v1).
 *
 * Architecture:
 *  - One Konva.Stage per PDF page, stacked vertically inside #sp-scroll.
 *  - Stage units are PDF POINTS (exactly what the DB stores and what
 *    pymupdf consumes on export; y-down, origin top-left, no flip).
 *  - Display scaling happens ONLY via stage.scale({x: k, y: k}) where
 *    k = RENDER_SCALE * zoom. Node coordinates never contain display scale.
 *  - Two layers per stage: a non-listening background layer holding the
 *    pdf.js-rendered page bitmap, and an annotation layer above it.
 *
 * Konva and pdfjsLib are loaded globally by the template (pinned CDN builds).
 */
import {
  initApi,
  listAnnotations,
  createAnnotation,
  updateAnnotation,
  deleteAnnotation,
} from './api.js';

const cfg = JSON.parse(document.getElementById('sherbert-config').textContent);
initApi(cfg.apiBase);

const RENDER_SCALE = 1.5;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 4.0;
const ZOOM_STEP = 1.25;

/* Same palette (0-1 floats) as quick_edit4's getColorRgbArray(). */
const COLORS = {
  black: [0, 0, 0],
  red: [239 / 255, 68 / 255, 68 / 255],
  blue: [59 / 255, 130 / 255, 246 / 255],
  green: [34 / 255, 197 / 255, 94 / 255],
  yellow: [234 / 255, 179 / 255, 8 / 255],
  orange: [249 / 255, 115 / 255, 22 / 255],
};

const HIGHLIGHT_OPACITY = 0.3;
const EDITABLE_TYPES = ['pen', 'highlighter', 'text', 'stamp', 'cloud'];

/* Revision-cloud defaults, matching quick_edit4: red/green palette, a modest
 * scallop stroke, and a 20-point minimum drag size. */
const CLOUD_STROKE_WIDTH = 2;
const CLOUD_MIN_SIZE = 20;
/* Signature is a text-variant tool: same overlay flow, a cursive font, and
 * italic styling — export detects 'cursive'/'script' and renders italic. */
const SIGNATURE_FONT = 'Arial, cursive';
const TEXT_FONT = 'Arial, sans-serif';
/* Anchor sets. Stamps scale proportionally from the four corners (keepRatio);
 * clouds are rect-defined and get all eight anchors with the scallops
 * REGENERATED (never point-scaled) from the derived rect. */
const STAMP_ANCHORS = ['top-left', 'top-right', 'bottom-left', 'bottom-right'];

/* Transformer anchor sets per node type. Text: corners scale font
 * proportionally (keepRatio), middle-left/right adjust the wrap-box WIDTH, and
 * middle-top/bottom are disabled (text height is derived from wrapping, so
 * stretching it has no data-model meaning). Lines: all eight anchors, free
 * (non-uniform) stretch — legitimate for pen/highlighter strokes. */
const TEXT_ANCHORS = ['top-left', 'top-right', 'bottom-left', 'bottom-right', 'middle-left', 'middle-right'];
const LINE_ANCHORS = [
  'top-left', 'top-center', 'top-right',
  'middle-left', 'middle-right',
  'bottom-left', 'bottom-center', 'bottom-right',
];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  zoom: 1,
  tool: 'pen',
  colors: { pen: 'black', highlighter: 'yellow', text: 'black', signature: 'black', cloud: 'red' },
  sizes: { pen: 2, highlighter: 20, text: 14, signature: 18, eraser: 15 },
  stamps: cfg.stamps || [], // [{ label, url }]
  selectedStamp: 0,
  pages: [], // { index, widthPts, heightPts, wrap, stage, bgLayer, annLayer, transformer }
  drawing: null, // { page, line }
  cloud: null, // { page, rect, startX, startY }
  erasing: null, // { modifiedIds: Set }
  eraserCursor: null, // { page, circle }
  selected: null, // Konva node
  overlay: null, // active textarea overlay
  undoStack: [],
  redoStack: [],
  busy: false,
};

const scrollEl = document.getElementById('sp-scroll');
const pagesEl = document.getElementById('sp-pages');

const deleteBtn = document.createElement('button');
deleteBtn.type = 'button';
deleteBtn.className = 'sp-delete-btn';
deleteBtn.textContent = '✕';
deleteBtn.title = 'Delete annotation';
deleteBtn.style.display = 'none';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function k() {
  return RENDER_SCALE * state.zoom;
}

function rgbToCss(stroke) {
  const [r, g, b] = stroke || [0, 0, 0];
  return `rgb(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)})`;
}

function currentRgb() {
  return COLORS[state.colors[state.tool]] || COLORS.black;
}

function clone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function flatToPairs(flat) {
  const pairs = [];
  for (let i = 0; i + 1 < flat.length; i += 2) pairs.push([flat[i], flat[i + 1]]);
  return pairs;
}

function pairsToFlat(pairs) {
  const flat = [];
  for (const p of pairs) {
    flat.push(p[0], p[1]);
  }
  return flat;
}

/* Port of models._generate_cloud_points: scalloped outline for the "new"
 * cloud storage format where vertices[0] == [[x, y, w, h]]. */
function cloudPoints(x, y, width, height, scallop = 35, steps = 8) {
  const hCount = Math.max(2, Math.round(width / scallop));
  const vCount = Math.max(2, Math.round(height / scallop));
  const hStep = width / hCount;
  const vStep = height / vCount;
  const hr = hStep / 2;
  const vr = vStep / 2;

  const arcPts = (x0, y0, r, x1, y1) => {
    const mx = (x0 + x1) / 2;
    const my = (y0 + y1) / 2;
    const dx = x1 - x0;
    const dy = y1 - y0;
    const chord = Math.hypot(dx, dy);
    if (chord === 0 || r <= 0) return [[x1, y1]];
    r = Math.max(r, chord / 2);
    const d = Math.sqrt(r * r - (chord / 2) ** 2);
    const nx = dy / chord;
    const ny = -dx / chord;
    const cx = mx + nx * d;
    const cy = my + ny * d;
    const a0 = Math.atan2(y0 - cy, x0 - cx);
    const a1 = Math.atan2(y1 - cy, x1 - cx);
    let da = a1 - a0;
    if (da < 0) da += 2 * Math.PI;
    const pts = [];
    for (let s = 1; s <= steps; s++) {
      const a = a0 + (da * s) / steps;
      pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
    }
    return pts;
  };

  const pts = [[x, y]];
  let prev = [x, y];
  const edges = [
    [Array.from({ length: hCount }, (_, i) => [x + (i + 1) * hStep, y]), hr],
    [Array.from({ length: vCount }, (_, i) => [x + width, y + (i + 1) * vStep]), vr],
    [Array.from({ length: hCount }, (_, i) => [x + width - (i + 1) * hStep, y + height]), hr],
    [Array.from({ length: vCount }, (_, i) => [x, y + height - (i + 1) * vStep]), vr],
  ];
  for (const [targets, radius] of edges) {
    for (const [ex, ey] of targets) {
      pts.push(...arcPts(prev[0], prev[1], radius, ex, ey));
      prev = [ex, ey];
    }
  }
  pts.push([x, y]);
  return pts;
}

// ---------------------------------------------------------------------------
// Page rendering (pdf.js -> offscreen canvas -> Konva.Image background)
// ---------------------------------------------------------------------------

async function renderPages() {
  const pdf = await pdfjsLib.getDocument(cfg.fileUrl).promise;
  state.pdf = pdf;
  const dpr = window.devicePixelRatio || 1;

  for (let n = 1; n <= pdf.numPages; n++) {
    const pdfPage = await pdf.getPage(n);
    const unitViewport = pdfPage.getViewport({ scale: 1 }); // PDF points
    const widthPts = unitViewport.width;
    const heightPts = unitViewport.height;

    // Render the bitmap at a devicePixelRatio-aware resolution.
    const bitmapScale = RENDER_SCALE * dpr;
    const renderViewport = pdfPage.getViewport({ scale: bitmapScale });
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(renderViewport.width);
    canvas.height = Math.floor(renderViewport.height);
    await pdfPage.render({
      canvasContext: canvas.getContext('2d'),
      viewport: renderViewport,
    }).promise;

    const wrap = document.createElement('div');
    wrap.className = 'sp-page';
    wrap.dataset.pageIndex = String(n - 1);
    pagesEl.appendChild(wrap);

    const scale = k();
    const stage = new Konva.Stage({
      container: wrap,
      width: widthPts * scale,
      height: heightPts * scale,
      scale: { x: scale, y: scale },
    });

    const bgLayer = new Konva.Layer({ listening: false });
    const bgImage = new Konva.Image({
      image: canvas,
      x: 0,
      y: 0,
      width: widthPts, // PDF points; bitmap resolution is independent
      height: heightPts,
    });
    bgLayer.add(bgImage);

    const annLayer = new Konva.Layer();
    const transformer = new Konva.Transformer({
      rotateEnabled: false,
      ignoreStroke: false,
      flipEnabled: false,
    });
    annLayer.add(transformer);

    stage.add(bgLayer, annLayer);

    const page = {
      index: n - 1,
      widthPts,
      heightPts,
      wrap,
      stage,
      bgLayer,
      bgImage,
      annLayer,
      transformer,
      pdfPage,
      bitmapScale,
      bitmapCanvas: canvas, // reused in place by rerenderVisibleBitmaps
      pendingScale: null, // set when an off-screen stage resize is deferred
      rendering: false,
    };
    // Keep even the zoom-1 backing store within budget for large-format pages.
    applyStagePixelRatio(page, stagePixelRatio(page, scale));
    state.pages.push(page);
    bindStageEvents(page);
  }
  applyTouchAction();
}

// ---------------------------------------------------------------------------
// Memory budget: cap every canvas backing store at pdf.js's default
// maxCanvasPixels (16 MP). Konva scene AND hit canvases scale with the stage
// pixel size (× devicePixelRatio²); at zoom 4 a Letter page is a ~70 MB buffer
// per layer, and large-format pages are hundreds of MB — multiplied across all
// pages. We keep the CSS size at widthPts*k but clamp the backing-store pixel
// ratio so width_px * height_px stays within the budget. Slightly softer at
// extreme zoom — the same trade-off pdf.js makes.
// ---------------------------------------------------------------------------

const PIXEL_BUDGET = 16777216; // 16 MP (pdf.js maxCanvasPixels)

/* Pixel ratio for a stage's backing store at a given committed scale: capped
 * so cssW*pr * cssH*pr <= PIXEL_BUDGET, and never above the device ratio. */
function stagePixelRatio(page, scale) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = page.widthPts * scale;
  const cssH = page.heightPts * scale;
  if (cssW <= 0 || cssH <= 0) return dpr;
  return Math.min(dpr, Math.sqrt(PIXEL_BUDGET / (cssW * cssH)));
}

/* Apply a pixel ratio to a stage's scene and hit canvases (both layers).
 * Pointer math (getRelativePointerPosition) is ratio-independent and the
 * Transformer reads getClientRect, so selection/hit-testing are unaffected. */
function applyStagePixelRatio(page, pr) {
  for (const layer of page.stage.getLayers()) {
    const scene = layer.getCanvas && layer.getCanvas();
    if (scene && scene.getPixelRatio() !== pr) scene.setPixelRatio(pr);
    const hit = layer.getHitCanvas && layer.getHitCanvas();
    if (hit && hit.getPixelRatio() !== pr) hit.setPixelRatio(pr);
  }
}

/* Commit a page's stage to `scale`: set the budgeted pixel ratio FIRST (so the
 * backing store is never allocated at the uncapped device resolution even
 * transiently), then resize and redraw. Clears any deferred/reserved state. */
function applyStageScale(page, scale) {
  page.pendingScale = null;
  applyStagePixelRatio(page, stagePixelRatio(page, scale));
  page.stage.scale({ x: scale, y: scale });
  page.stage.size({ width: page.widthPts * scale, height: page.heightPts * scale });
  page.stage.batchDraw();
  // Drop any layout reservation now that the real canvas carries the size.
  page.wrap.style.width = '';
  page.wrap.style.height = '';
}

/* Defer an off-screen page's stage resize to keep commit cost and peak memory
 * proportional to VISIBLE pages, not document size. Reserve the wrap's layout
 * box (cheap, no canvas allocation) so pages below don't jump, and record the
 * pending scale; applyPendingStageScales() finishes the job on scroll-in. */
function deferStageScale(page, scale) {
  page.pendingScale = scale;
  page.wrap.style.width = `${page.widthPts * scale}px`;
  page.wrap.style.height = `${page.heightPts * scale}px`;
}

let rerenderTimer = null;

function scheduleBitmapRerender() {
  clearTimeout(rerenderTimer);
  rerenderTimer = setTimeout(rerenderVisibleBitmaps, 220);
}

function pageIsNearViewport(page) {
  const view = scrollEl.getBoundingClientRect();
  const box = page.wrap.getBoundingClientRect();
  const margin = view.height; // pre-render one viewport above/below
  return box.bottom > view.top - margin && box.top < view.bottom + margin;
}

/* Effective render scale for a page's bitmap, area-capped so the bitmap canvas
 * itself stays within the pixel budget (a scale cap would let a large-format
 * page balloon into a gigapixel canvas). */
function bitmapTargetScale(page) {
  const dpr = window.devicePixelRatio || 1;
  const desired = Math.max(RENDER_SCALE, state.zoom * RENDER_SCALE) * dpr;
  const areaCap = Math.sqrt(PIXEL_BUDGET / (page.widthPts * page.heightPts));
  return Math.min(desired, areaCap);
}

async function rerenderVisibleBitmaps() {
  for (const page of state.pages) {
    const target = bitmapTargetScale(page);
    if (page.rendering || Math.abs(page.bitmapScale - target) < 0.01) continue;
    if (!pageIsNearViewport(page)) continue;
    page.rendering = true;
    try {
      const viewport = page.pdfPage.getViewport({ scale: target });
      // Reuse the page's existing bitmap canvas in place to avoid allocating a
      // fresh multi-MB buffer on every zoom settle (old ones churn the GC).
      const canvas = page.bitmapCanvas || document.createElement('canvas');
      page.bitmapCanvas = canvas;
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      await page.pdfPage.render({
        canvasContext: canvas.getContext('2d'),
        viewport,
      }).promise;
      page.bitmapScale = target;
      page.bgImage.image(canvas);
      page.bgLayer.batchDraw();
    } catch (err) {
      console.error(`Failed to re-render page ${page.index + 1}:`, err);
    } finally {
      page.rendering = false;
    }
    // Zoom changed again mid-render: this bitmap is stale — re-schedule.
    if (Math.abs(bitmapTargetScale(page) - target) > 0.01) scheduleBitmapRerender();
  }
}

/* Stage resizes deferred while off-screen must land BEFORE the page is drawn
 * on/interacted with, so apply them synchronously (not debounced) as pages
 * scroll near — otherwise pointer coordinates would use a stale stage scale. */
function applyPendingStageScales() {
  for (const page of state.pages) {
    if (page.pendingScale != null && pageIsNearViewport(page)) {
      applyStageScale(page, page.pendingScale);
    }
  }
}

// Pages scrolled into view after a zoom need their committed stage size (sync)
// and their sharp bitmap (debounced).
scrollEl.addEventListener(
  'scroll',
  () => {
    applyPendingStageScales();
    scheduleBitmapRerender();
  },
  { passive: true }
);

// ---------------------------------------------------------------------------
// Node materialization (shared by initial load, create, and undo/redo replay)
// ---------------------------------------------------------------------------

function setMeta(node, meta) {
  node.setAttr('sherbert', meta);
}

function getMeta(node) {
  return node && typeof node.getAttr === 'function' ? node.getAttr('sherbert') : null;
}

/* Climb from a hit target to the nearest ancestor carrying annotation meta.
 * A stroke with erasures is rendered as a cached Konva.Group (Line + erasure
 * Circles), so a click may land on the inner Line — the meta lives on the
 * group. Returns the meta-bearing node, or null. */
function findMetaNode(target) {
  let n = target;
  while (n && !(n instanceof Konva.Layer) && !(n instanceof Konva.Stage)) {
    if (getMeta(n)) return n;
    n = n.getParent();
  }
  return null;
}

/* The bounding rect [x, y, w, h] (PDF points) of a cloud annotation, from the
 * new-format single-rect vertex when present, else the bbox of a legacy point
 * array (which converts it to the new format on the first edit). */
function cloudRectFromData(data) {
  const seg = (data.vertices && data.vertices[0]) || [];
  if (seg.length === 1 && seg[0].length === 4) {
    return seg[0].slice();
  }
  if (seg.length >= 2) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [px, py] of seg) {
      if (px < minX) minX = px;
      if (py < minY) minY = py;
      if (px > maxX) maxX = px;
      if (py > maxY) maxY = py;
    }
    return [minX, minY, maxX - minX, maxY - minY];
  }
  return null;
}

/* A destination-out circle that punches a hole in its cached-group buffer. */
function makeErasureCircle(e) {
  return new Konva.Circle({
    x: e.cx,
    y: e.cy,
    radius: e.r,
    fill: '#000',
    globalCompositeOperation: 'destination-out',
    listening: false,
  });
}

/* The bare stroke Line for a pen/highlighter record (no erasures applied). */
function buildStrokeLine(data, type) {
  const seg = (data.vertices && data.vertices[0]) || [];
  return new Konva.Line({
    points: pairsToFlat(seg),
    stroke: rgbToCss(data.colors && data.colors.stroke),
    strokeWidth: (data.border && data.border.width) || 1,
    lineCap: 'round',
    lineJoin: 'round',
    hitStrokeWidth: Math.max((data.border && data.border.width) || 1, 8),
    ...(type === 'highlighter'
      ? { opacity: HIGHLIGHT_OPACITY, globalCompositeOperation: 'multiply' }
      : { opacity: data.opacity == null ? 1 : data.opacity }),
  });
}

/* Build the display node for a pen/highlighter stroke. With erasures it is a
 * CACHED group (Line + destination-out Circles) so the holes are isolated to
 * the group's own buffer and never punch through the PDF background. */
function buildStrokeNode(data, type) {
  const line = buildStrokeLine(data, type);
  const erasures = data.erasures || [];
  if (!erasures.length) return line;
  const group = new Konva.Group();
  group.add(line);
  for (const e of erasures) group.add(makeErasureCircle(e));
  // Caching (which isolates the destination-out holes to the group buffer) is
  // deferred to the caller, once the group is attached to a layer.
  return group;
}

/* Build a Konva node from an AnnotationOut-shaped record:
 * { id, annotation_type, annotation_data, is_owner } */
function materialize(page, record) {
  const type = record.annotation_type;
  const data = record.annotation_data;
  let node = null;

  if (type === 'pen' || type === 'highlighter') {
    const seg = (data.vertices && data.vertices[0]) || [];
    if (seg.length < 1) return null;
    node = buildStrokeNode(data, type);
  } else if (type === 'text') {
    const rect = data.rect || [0, 0, 100, 30];
    const attrs = {
      x: rect[0],
      y: rect[1],
      text: data.content || '',
      fontSize: data.fontSize || 14,
      fontFamily: data.fontFamily || 'Arial, sans-serif',
      fontStyle: data.fontStyle === 'italic' ? 'italic' : 'normal',
      fill: rgbToCss(data.colors && data.colors.stroke),
    };
    // Round-trip the stored box width so text wraps exactly as the server-side
    // PyMuPDF export (insert_htmlbox flows the content into this rect). Without
    // an explicit width, wrapped annotations would render on a single line.
    const boxWidth = rect[2] - rect[0];
    if (boxWidth > 0) attrs.width = boxWidth;
    node = new Konva.Text(attrs);
  } else if (type === 'cloud') {
    // Fully editable: derive the rect (new-format or legacy bbox) and draw the
    // scalloped outline. On the first edit a legacy point-array is regenerated
    // from its bbox and re-saved in the new [x,y,w,h] format.
    const rect = cloudRectFromData(data);
    if (!rect) return null;
    const pairs = cloudPoints(rect[0], rect[1], rect[2], rect[3]);
    if (!pairs || pairs.length < 2) return null;
    node = new Konva.Line({
      points: pairsToFlat(pairs),
      stroke: rgbToCss(data.colors && data.colors.stroke),
      strokeWidth: (data.border && data.border.width) || 1,
      lineCap: 'round',
      lineJoin: 'round',
      closed: true,
      hitStrokeWidth: Math.max((data.border && data.border.width) || 1, 10),
      opacity: data.opacity == null ? 1 : data.opacity,
    });
  } else if (type === 'stamp') {
    node = new Konva.Image({
      x: data.x || 0,
      y: data.y || 0,
      width: data.width || 100,
      height: data.height || 50,
    });
    const img = new window.Image();
    img.onload = () => {
      node.image(img);
      page.annLayer.batchDraw();
    };
    img.src = data.imageUrl;
  }

  if (!node) return null;

  setMeta(node, {
    id: record.id,
    type,
    data: clone(data),
    isOwner: !!record.is_owner,
  });

  if (type === 'cloud') setMeta(node, { ...getMeta(node), cloudRect: cloudRectFromData(data) });

  if (type === 'text' && record.is_owner) {
    node.on('dblclick dbltap', () => openTextOverlay(page, null, node));
  }

  page.annLayer.add(node);
  // A stroke rendered as an erasure group must be cached once attached so its
  // destination-out holes stay isolated to the group's own buffer.
  if (node instanceof Konva.Group) node.cache();
  // Keep the transformer on top of annotation content.
  page.transformer.moveToTop();
  page.annLayer.batchDraw();
  return node;
}

function findNode(annotationId) {
  for (const page of state.pages) {
    for (const child of page.annLayer.getChildren()) {
      const meta = getMeta(child);
      if (meta && meta.id === annotationId) return { page, node: child };
    }
  }
  return null;
}

async function loadAnnotations() {
  const records = await listAnnotations(cfg.pdfId);
  for (const record of records) {
    const page = state.pages[record.page_number];
    if (!page) continue;
    materialize(page, record);
  }
}

// ---------------------------------------------------------------------------
// Undo / redo — command stack replayed against the API
// ---------------------------------------------------------------------------

function pushCommand(cmd) {
  state.undoStack.push(cmd);
  state.redoStack.length = 0;
  updateUndoButtons();
}

function updateUndoButtons() {
  document.getElementById('sp-undo').disabled = state.undoStack.length === 0;
  document.getElementById('sp-redo').disabled = state.redoStack.length === 0;
}

async function replayCreate(cmd) {
  const res = await createAnnotation(cfg.pdfId, cmd.pageIndex, cmd.type, cmd.data);
  cmd.annotationId = res.id; // server assigns a fresh id on re-create
  materialize(state.pages[cmd.pageIndex], {
    id: res.id,
    annotation_type: cmd.type,
    annotation_data: cmd.data,
    is_owner: true,
  });
}

async function replayDelete(cmd) {
  await deleteAnnotation(cmd.annotationId);
  const found = findNode(cmd.annotationId);
  if (found) {
    if (state.selected === found.node) deselect();
    found.node.destroy();
    found.page.annLayer.batchDraw();
  }
}

async function replayUpdate(cmd, data) {
  await updateAnnotation(cmd.annotationId, data);
  const found = findNode(cmd.annotationId);
  if (found) {
    if (state.selected === found.node) deselect();
    found.node.destroy();
    materialize(found.page, {
      id: cmd.annotationId,
      annotation_type: cmd.type,
      annotation_data: data,
      is_owner: true,
    });
  }
}

async function undo() {
  if (state.busy || !state.undoStack.length) return;
  state.busy = true;
  const cmd = state.undoStack.pop();
  try {
    if (cmd.kind === 'create') await replayDelete(cmd);
    else if (cmd.kind === 'delete') await replayCreate(cmd);
    else await replayUpdate(cmd, cmd.before);
    state.redoStack.push(cmd);
  } catch (err) {
    console.error('Undo failed:', err);
    state.undoStack.push(cmd);
  } finally {
    state.busy = false;
    updateUndoButtons();
  }
}

async function redo() {
  if (state.busy || !state.redoStack.length) return;
  state.busy = true;
  const cmd = state.redoStack.pop();
  try {
    if (cmd.kind === 'create') await replayCreate(cmd);
    else if (cmd.kind === 'delete') await replayDelete(cmd);
    else await replayUpdate(cmd, cmd.after);
    state.undoStack.push(cmd);
  } catch (err) {
    console.error('Redo failed:', err);
    state.redoStack.push(cmd);
  } finally {
    state.busy = false;
    updateUndoButtons();
  }
}

// ---------------------------------------------------------------------------
// Pen / highlighter drawing
// ---------------------------------------------------------------------------

function startStroke(page, pos) {
  const highlighter = state.tool === 'highlighter';
  const line = new Konva.Line({
    points: [pos.x, pos.y],
    stroke: rgbToCss(currentRgb()),
    strokeWidth: state.sizes[state.tool],
    lineCap: 'round',
    lineJoin: 'round',
    listening: false,
    ...(highlighter
      ? { opacity: HIGHLIGHT_OPACITY, globalCompositeOperation: 'multiply' }
      : {}),
  });
  page.annLayer.add(line);
  state.drawing = { page, line, type: highlighter ? 'highlighter' : 'pen' };
}

function extendStroke(pos) {
  const { line, page } = state.drawing;
  line.points(line.points().concat([pos.x, pos.y]));
  page.annLayer.batchDraw();
}

async function finishStroke() {
  const { page, line, type } = state.drawing;
  state.drawing = null;

  let flat = line.points();
  if (flat.length === 2) {
    // Single click: keep a visible dot.
    flat = flat.concat([flat[0] + 0.1, flat[1] + 0.1]);
    line.points(flat);
  }

  const data = {
    vertices: [flatToPairs(flat)],
    colors: { stroke: currentRgb().slice() },
    border: { width: line.strokeWidth() },
    opacity: 1.0, // matches quick_edit4; highlighter opacity is a display/export concern
  };

  try {
    const res = await createAnnotation(cfg.pdfId, page.index, type, data);
    setMeta(line, { id: res.id, type, data: clone(data), isOwner: true });
    line.listening(true);
    line.hitStrokeWidth(Math.max(line.strokeWidth(), 8));
    pushCommand({ kind: 'create', annotationId: res.id, pageIndex: page.index, type, data: clone(data) });
  } catch (err) {
    console.error('Failed to save stroke:', err);
    line.destroy();
    page.annLayer.batchDraw();
  }
}

// ---------------------------------------------------------------------------
// Text tool (positioned textarea overlay — the standard Konva pattern)
// ---------------------------------------------------------------------------

function closeOverlay(commit) {
  const overlay = state.overlay;
  if (!overlay) return;
  state.overlay = null;
  overlay.el.removeEventListener('blur', overlay.onBlur);
  overlay.el.remove();
  if (overlay.node) {
    overlay.node.visible(true);
    overlay.page.annLayer.batchDraw();
  }
  if (commit) overlay.commit();
}

function openTextOverlay(page, posPts, existingNode) {
  if (state.overlay) closeOverlay(true);

  const scale = k();
  const meta = existingNode ? getMeta(existingNode) : null;
  // New nodes inherit the active tool's font (signature = cursive/italic);
  // existing nodes keep their stored font when re-edited.
  const isSignature = !meta && state.tool === 'signature';
  const fontSize = meta ? meta.data.fontSize || 14 : state.sizes[state.tool] || state.sizes.text;
  const fontFamily = meta ? meta.data.fontFamily || TEXT_FONT : isSignature ? SIGNATURE_FONT : TEXT_FONT;
  const fontStyle = meta ? meta.data.fontStyle || 'normal' : isSignature ? 'italic' : 'normal';
  const colorCss = meta
    ? rgbToCss(meta.data.colors && meta.data.colors.stroke)
    : rgbToCss(currentRgb());
  const pos = existingNode ? { x: existingNode.x(), y: existingNode.y() } : posPts;

  const ta = document.createElement('textarea');
  ta.className = 'sp-text-overlay';
  ta.value = meta ? meta.data.content || '' : '';
  ta.style.left = `${pos.x * scale}px`;
  ta.style.top = `${pos.y * scale}px`;
  ta.style.fontSize = `${fontSize * scale}px`;
  ta.style.fontFamily = fontFamily;
  ta.style.fontStyle = fontStyle;
  ta.style.color = colorCss;
  ta.style.width = existingNode
    ? `${Math.max(existingNode.width() * scale + 20, 80)}px`
    : '160px';
  ta.rows = 1;
  page.wrap.appendChild(ta);
  // Focus after the pointer sequence completes; focusing synchronously
  // inside pointerdown loses to the browser's default focus handling.
  requestAnimationFrame(() => ta.focus());

  const autosize = () => {
    ta.style.height = 'auto';
    ta.style.height = `${ta.scrollHeight}px`;
  };
  autosize();
  ta.addEventListener('input', autosize);

  if (existingNode) {
    existingNode.visible(false);
    page.annLayer.batchDraw();
  }

  const openedAt = performance.now();
  const overlay = {
    el: ta,
    page,
    node: existingNode || null,
    commit: () => commitText(page, pos, ta.value, existingNode, fontSize, colorCss, fontFamily, fontStyle),
    onBlur: () => {
      // Blur fired by the tail of the opening click: reclaim focus.
      if (performance.now() - openedAt < 250) {
        ta.focus();
        return;
      }
      closeOverlay(true);
    },
  };
  state.overlay = overlay;

  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      closeOverlay(true);
    } else if (e.key === 'Escape') {
      closeOverlay(false);
    }
  });
  ta.addEventListener('blur', overlay.onBlur);
}

async function commitText(page, posPts, raw, existingNode, fontSize, colorCss, fontFamily, fontStyle) {
  const content = raw.replace(/\s+$/, '');
  if (existingNode) {
    const meta = getMeta(existingNode);
    if (!content || content === meta.data.content) return;
    const before = clone(meta.data);
    existingNode.text(content);
    meta.data.content = content;
    meta.data.rect = [
      existingNode.x(),
      existingNode.y(),
      existingNode.x() + existingNode.width(),
      existingNode.y() + existingNode.height(),
    ];
    page.annLayer.batchDraw();
    try {
      await updateAnnotation(meta.id, meta.data);
      pushCommand({
        kind: 'update',
        annotationId: meta.id,
        pageIndex: page.index,
        type: meta.type,
        before,
        after: clone(meta.data),
      });
    } catch (err) {
      console.error('Failed to update text annotation:', err);
    }
    return;
  }

  if (!content) return;
  const family = fontFamily || TEXT_FONT;
  const style = fontStyle === 'italic' ? 'italic' : 'normal';
  const node = new Konva.Text({
    x: posPts.x,
    y: posPts.y,
    text: content,
    fontSize,
    fontFamily: family,
    fontStyle: style,
    fill: colorCss,
  });
  page.annLayer.add(node);
  page.transformer.moveToTop();
  page.annLayer.batchDraw();

  const data = {
    rect: [posPts.x, posPts.y, posPts.x + node.width(), posPts.y + node.height()],
    content,
    colors: { stroke: currentRgb().slice() },
    fontSize,
    fontFamily: family,
    fontStyle: style,
  };

  try {
    const res = await createAnnotation(cfg.pdfId, page.index, 'text', data);
    setMeta(node, { id: res.id, type: 'text', data: clone(data), isOwner: true });
    node.on('dblclick dbltap', () => openTextOverlay(page, null, node));
    pushCommand({ kind: 'create', annotationId: res.id, pageIndex: page.index, type: 'text', data: clone(data) });
  } catch (err) {
    console.error('Failed to save text annotation:', err);
    node.destroy();
    page.annLayer.batchDraw();
  }
}

// ---------------------------------------------------------------------------
// Stamp tool: place a configured palette image, then select/move/resize/bake
// ---------------------------------------------------------------------------

const DEFAULT_STAMP_WIDTH = 120; // PDF points; height derives from aspect ratio

function currentStamp() {
  return state.stamps[state.selectedStamp] || state.stamps[0] || null;
}

function placeStamp(page, pos) {
  const stamp = currentStamp();
  if (!stamp) return;
  const img = new window.Image();
  img.onload = async () => {
    const natW = img.naturalWidth || 200;
    const natH = img.naturalHeight || 80;
    const width = DEFAULT_STAMP_WIDTH;
    const height = width * (natH / natW);
    // Center the stamp on the click point (quick_edit4 places x - w/2, y - h/2).
    const x = pos.x - width / 2;
    const y = pos.y - height / 2;
    const node = new Konva.Image({ x, y, width, height, image: img });
    page.annLayer.add(node);
    page.transformer.moveToTop();
    page.annLayer.batchDraw();

    const data = { type: 'stamp', x, y, width, height, imageUrl: stamp.url };
    try {
      const res = await createAnnotation(cfg.pdfId, page.index, 'stamp', data);
      setMeta(node, { id: res.id, type: 'stamp', data: clone(data), isOwner: true });
      pushCommand({ kind: 'create', annotationId: res.id, pageIndex: page.index, type: 'stamp', data: clone(data) });
    } catch (err) {
      console.error('Failed to save stamp annotation:', err);
      node.destroy();
      page.annLayer.batchDraw();
    }
  };
  img.src = stamp.url;
}

// ---------------------------------------------------------------------------
// Revision-cloud tool: drag a rect (dashed live preview), then emit a closed
// scalloped Konva.Line stored in the new [x, y, w, h] rect format.
// ---------------------------------------------------------------------------

function startCloud(page, pos) {
  const rect = new Konva.Rect({
    x: pos.x,
    y: pos.y,
    width: 0,
    height: 0,
    stroke: rgbToCss(currentRgb()),
    strokeWidth: CLOUD_STROKE_WIDTH,
    dash: [5, 5],
    listening: false,
  });
  page.annLayer.add(rect);
  page.annLayer.batchDraw();
  state.cloud = { page, rect, startX: pos.x, startY: pos.y };
}

function extendCloud(pos) {
  const { page, rect, startX, startY } = state.cloud;
  rect.setAttrs({
    x: Math.min(startX, pos.x),
    y: Math.min(startY, pos.y),
    width: Math.abs(pos.x - startX),
    height: Math.abs(pos.y - startY),
  });
  page.annLayer.batchDraw();
}

async function finishCloud() {
  const { page, rect } = state.cloud;
  state.cloud = null;
  const x = rect.x();
  const y = rect.y();
  const w = rect.width();
  const h = rect.height();
  rect.destroy();
  page.annLayer.batchDraw();
  if (w < CLOUD_MIN_SIZE || h < CLOUD_MIN_SIZE) return; // ignore stray clicks/tiny drags

  const stroke = currentRgb().slice();
  const pairs = cloudPoints(x, y, w, h);
  const node = new Konva.Line({
    points: pairsToFlat(pairs),
    stroke: rgbToCss(stroke),
    strokeWidth: CLOUD_STROKE_WIDTH,
    lineCap: 'round',
    lineJoin: 'round',
    closed: true,
    hitStrokeWidth: Math.max(CLOUD_STROKE_WIDTH, 10),
  });
  page.annLayer.add(node);
  page.transformer.moveToTop();
  page.annLayer.batchDraw();

  const data = {
    vertices: [[[x, y, w, h]]],
    colors: { stroke },
    border: { width: CLOUD_STROKE_WIDTH },
    opacity: 1.0,
  };
  try {
    const res = await createAnnotation(cfg.pdfId, page.index, 'cloud', data);
    setMeta(node, { id: res.id, type: 'cloud', data: clone(data), isOwner: true, cloudRect: [x, y, w, h] });
    pushCommand({ kind: 'create', annotationId: res.id, pageIndex: page.index, type: 'cloud', data: clone(data) });
  } catch (err) {
    console.error('Failed to save cloud annotation:', err);
    node.destroy();
    page.annLayer.batchDraw();
  }
}

// ---------------------------------------------------------------------------
// Eraser tool: append erasure circles to the user's own pen/highlighter strokes
// and render them as destination-out holes inside a cached group.
// ---------------------------------------------------------------------------

function eraserRadiusPts() {
  // The slider is a screen-pixel radius (quick_edit4's 15px default); convert to
  // PDF points so the erasure geometry is stored zoom-independently.
  return state.sizes.eraser / k();
}

function strokeNodesOnPage(page) {
  return page.annLayer.getChildren((n) => {
    const meta = getMeta(n);
    return meta && (meta.type === 'pen' || meta.type === 'highlighter');
  });
}

function distPointToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  if (dx === 0 && dy === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy);
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function strokeHit(meta, pos, r) {
  const seg = (meta.data.vertices && meta.data.vertices[0]) || [];
  if (!seg.length) return false;
  const sw = (meta.data.border && meta.data.border.width) || 1;
  const reach = r + sw / 2;
  if (seg.length === 1) return Math.hypot(pos.x - seg[0][0], pos.y - seg[0][1]) <= reach;
  for (let i = 1; i < seg.length; i++) {
    if (distPointToSegment(pos.x, pos.y, seg[i - 1][0], seg[i - 1][1], seg[i][0], seg[i][1]) <= reach) {
      return true;
    }
  }
  return false;
}

function updateEraserCursor(page, pos) {
  const r = eraserRadiusPts();
  if (!state.eraserCursor || state.eraserCursor.page !== page) {
    hideEraserCursor();
    const circle = new Konva.Circle({
      x: pos.x,
      y: pos.y,
      radius: r,
      fill: 'rgba(128,128,128,0.15)',
      stroke: 'rgba(80,80,80,0.6)',
      strokeWidth: 1,
      dash: [3, 2],
      listening: false,
    });
    page.annLayer.add(circle);
    state.eraserCursor = { page, circle };
  }
  const c = state.eraserCursor.circle;
  c.position({ x: pos.x, y: pos.y });
  c.radius(r);
  c.moveToTop();
  page.annLayer.batchDraw();
}

function hideEraserCursor() {
  if (!state.eraserCursor) return;
  const { page, circle } = state.eraserCursor;
  state.eraserCursor = null;
  circle.destroy();
  page.annLayer.batchDraw();
}

/* Convert a plain stroke Line into a cached group so destination-out erasure
 * circles punch holes only inside the group's own buffer. Meta transfers to the
 * group; a node that is already a group is returned unchanged. */
function ensureErasureGroup(page, node) {
  if (node instanceof Konva.Group) return node;
  const meta = getMeta(node);
  const group = new Konva.Group();
  node.remove();
  node.setAttr('sherbert', null);
  group.add(node);
  setMeta(group, meta);
  page.annLayer.add(group);
  page.transformer.moveToTop();
  return group;
}

function appendErasure(page, node, cx, cy, r) {
  const group = ensureErasureGroup(page, node);
  const meta = getMeta(group);
  if (!meta.data.erasures) meta.data.erasures = [];
  meta.data.erasures.push({ cx, cy, r });
  group.add(makeErasureCircle({ cx, cy, r }));
  group.clearCache();
  group.cache();
  if (state.eraserCursor && state.eraserCursor.page === page) state.eraserCursor.circle.moveToTop();
  page.annLayer.batchDraw();
  return group;
}

function startErasing(page, pos) {
  state.erasing = { page, modifiedIds: new Set() };
  updateEraserCursor(page, pos);
  applyEraserAt(page, pos);
}

function applyEraserAt(page, pos) {
  if (!state.erasing) return;
  const r = eraserRadiusPts();
  for (const node of strokeNodesOnPage(page)) {
    const meta = getMeta(node);
    if (!meta.isOwner) continue; // only the current user's own strokes
    if (strokeHit(meta, pos, r)) {
      const eraseNode = appendErasure(page, node, pos.x, pos.y, r);
      state.erasing.modifiedIds.add(getMeta(eraseNode).id);
    }
  }
}

/* Batch the PUTs on pointerup: one per modified stroke, persisting its full
 * (now erasure-carrying) annotation_data. */
async function finishErasing() {
  const erasing = state.erasing;
  state.erasing = null;
  if (!erasing || !erasing.modifiedIds.size) return;
  for (const id of erasing.modifiedIds) {
    const found = findNode(id);
    if (!found) continue;
    const meta = getMeta(found.node);
    try {
      await updateAnnotation(meta.id, meta.data);
    } catch (err) {
      console.error('Failed to persist erasure:', err);
    }
  }
}

// ---------------------------------------------------------------------------
// Select tool: Transformer + drag-to-move; bake transforms into geometry
// ---------------------------------------------------------------------------

/* Point the shared Transformer at the anchor set / keepRatio appropriate for
 * the node type being selected (text vs. line). Called on every select so the
 * config always matches the current selection, and reset on deselect. */
function configureTransformer(transformer, node) {
  const meta = getMeta(node);
  const type = meta && meta.type;
  if (type === 'text') {
    transformer.setAttrs({ enabledAnchors: TEXT_ANCHORS, keepRatio: true });
  } else if (type === 'stamp') {
    // Proportional corner-only scaling keeps the stamp's aspect ratio.
    transformer.setAttrs({ enabledAnchors: STAMP_ANCHORS, keepRatio: true });
  } else {
    // pen/highlighter strokes and rect-defined clouds: free 8-anchor stretch.
    transformer.setAttrs({ enabledAnchors: LINE_ANCHORS, keepRatio: false });
  }
}

/* Live handler during a text transform: for the middle-left/right anchors,
 * fold the horizontal scale into the box WIDTH (text reflows/wraps live) and
 * reset the scale so width changes accumulate without ever scaling the font.
 * Corner anchors are left alone here — their proportional scale is baked into
 * fontSize at transformend by bakeAndSave. */
function reflowTextDuringTransform(transformer, node) {
  if (!(node instanceof Konva.Text)) return;
  const anchor = transformer.getActiveAnchor();
  if (anchor === 'middle-left' || anchor === 'middle-right') {
    node.width(Math.max(30, node.width() * node.scaleX()));
    node.scaleX(1);
    node.scaleY(1);
  }
}

function select(page, node) {
  if (state.selected === node) return;
  deselect();
  state.selected = node;
  node.draggable(true);
  configureTransformer(page.transformer, node);
  page.transformer.nodes([node]);
  page.transformer.moveToTop();
  page.annLayer.batchDraw();

  node.on('dragend.spsel transformend.spsel', () => bakeAndSave(page, node));
  node.on('transform.spsel', () => reflowTextDuringTransform(page.transformer, node));
  node.on('dragmove.spsel transform.spsel', () => positionDeleteButton(page, node));
  positionDeleteButton(page, node);
}

function deselect() {
  const node = state.selected;
  if (!node) return;
  state.selected = null;
  node.off('.spsel');
  node.draggable(false);
  for (const page of state.pages) {
    if (page.transformer.nodes().length) {
      page.transformer.nodes([]);
      page.annLayer.batchDraw();
    }
    // Restore the free-stretch line config so a stale text config never leaks
    // into the next selection before configureTransformer runs.
    page.transformer.setAttrs({ enabledAnchors: LINE_ANCHORS, keepRatio: false });
  }
  deleteBtn.style.display = 'none';
}

function positionDeleteButton(page, node) {
  const rect = node.getClientRect(); // container pixels (includes stage scale)
  if (deleteBtn.parentElement !== page.wrap) page.wrap.appendChild(deleteBtn);
  deleteBtn.style.display = 'block';
  deleteBtn.style.left = `${Math.max(rect.x + rect.width + 4, 0)}px`;
  deleteBtn.style.top = `${Math.max(rect.y - 24, 0)}px`;
}

/* Bake the node's interactive transform into its geometry (PDF points),
 * reset the transform, and PUT the full annotation_data. */
async function bakeAndSave(page, node) {
  const meta = getMeta(node);
  if (!meta) return;
  const before = clone(meta.data);

  const sx = node.scaleX();
  const sy = node.scaleY();

  if (meta.type === 'stamp') {
    // Bake the interactive scale into width/height (aspect preserved by the
    // corner-only keepRatio transformer), reset the transform, PUT x/y/w/h.
    const w = Math.max(1, node.width() * sx);
    const h = Math.max(1, node.height() * sy);
    node.width(w);
    node.height(h);
    node.scale({ x: 1, y: 1 });
    meta.data.x = node.x();
    meta.data.y = node.y();
    meta.data.width = w;
    meta.data.height = h;
  } else if (meta.type === 'cloud') {
    // Cloud is rect-defined: derive the new rect from the transform applied to
    // the previous rect, then REGENERATE the scallops (never scale points).
    const [rx, ry, rw, rh] = meta.cloudRect;
    const nx = node.x();
    const ny = node.y();
    const newRect = [rx * sx + nx, ry * sy + ny, rw * sx, rh * sy];
    const pairs = cloudPoints(newRect[0], newRect[1], newRect[2], newRect[3]);
    node.points(pairsToFlat(pairs));
    node.position({ x: 0, y: 0 });
    node.scale({ x: 1, y: 1 });
    meta.cloudRect = newRect;
    meta.data.vertices = [[newRect]];
  } else if (node instanceof Konva.Group) {
    // Pen/highlighter stroke WITH erasures: bake into the inner line points and
    // the erasure-circle coords, then re-cache so destination-out stays isolated.
    const dx = node.x();
    const dy = node.y();
    const avg = (Math.abs(sx) + Math.abs(sy)) / 2;
    const line = node.findOne('Line');
    const circles = node.find('Circle');
    const flat = line.points();
    const baked = [];
    for (let i = 0; i + 1 < flat.length; i += 2) {
      baked.push(flat[i] * sx + dx, flat[i + 1] * sy + dy);
    }
    const newWidth = line.strokeWidth() * avg;
    line.points(baked);
    line.strokeWidth(newWidth);
    line.hitStrokeWidth(Math.max(newWidth, 8));
    const erasures = [];
    circles.forEach((c) => {
      const ncx = c.x() * sx + dx;
      const ncy = c.y() * sy + dy;
      const nr = c.radius() * avg;
      c.position({ x: ncx, y: ncy });
      c.radius(nr);
      erasures.push({ cx: ncx, cy: ncy, r: nr });
    });
    node.position({ x: 0, y: 0 });
    node.scale({ x: 1, y: 1 });
    node.clearCache();
    node.cache();
    meta.data.vertices = [flatToPairs(baked)];
    meta.data.border = { width: newWidth };
    meta.data.erasures = erasures;
  } else if (node instanceof Konva.Line) {
    const dx = node.x();
    const dy = node.y();
    const flat = node.points();
    const baked = [];
    for (let i = 0; i + 1 < flat.length; i += 2) {
      baked.push(flat[i] * sx + dx, flat[i + 1] * sy + dy);
    }
    const newWidth = node.strokeWidth() * ((Math.abs(sx) + Math.abs(sy)) / 2);
    node.points(baked);
    node.position({ x: 0, y: 0 });
    node.scale({ x: 1, y: 1 });
    node.strokeWidth(newWidth);
    node.hitStrokeWidth(Math.max(newWidth, 8));
    meta.data.vertices = [flatToPairs(baked)];
    meta.data.border = { width: newWidth };
  } else if (node instanceof Konva.Text) {
    // Two transform paths land here:
    //  - Middle-left/right (width) drags were baked live by
    //    reflowTextDuringTransform, which leaves scaleX == scaleY == 1, so the
    //    fontSize is untouched and only the reflowed width/rect is persisted.
    //  - Corner drags keep ratio (scaleX == scaleY == scale) and are baked now:
    //    scale BOTH the font AND the box width proportionally so the wrap stays
    //    visually identical while the whole annotation grows/shrinks.
    if (sx !== 1 || sy !== 1) {
      node.fontSize(Math.max(1, Math.round(node.fontSize() * sy)));
      node.width(Math.max(30, node.width() * sx));
      node.scale({ x: 1, y: 1 });
    }
    meta.data.fontSize = node.fontSize();
    meta.data.rect = [node.x(), node.y(), node.x() + node.width(), node.y() + node.height()];
  } else {
    return;
  }

  page.transformer.forceUpdate();
  page.annLayer.batchDraw();
  positionDeleteButton(page, node);

  if (JSON.stringify(before) === JSON.stringify(meta.data)) return;

  try {
    await updateAnnotation(meta.id, meta.data);
    pushCommand({
      kind: 'update',
      annotationId: meta.id,
      pageIndex: page.index,
      type: meta.type,
      before,
      after: clone(meta.data),
    });
  } catch (err) {
    console.error('Failed to update annotation:', err);
  }
}

async function deleteSelected() {
  const node = state.selected;
  if (!node) return;
  const meta = getMeta(node);
  if (!meta) return;
  const found = findNode(meta.id);
  const page = found ? found.page : null;
  deselect();
  try {
    await deleteAnnotation(meta.id);
    node.destroy();
    if (page) page.annLayer.batchDraw();
    pushCommand({
      kind: 'delete',
      annotationId: meta.id,
      pageIndex: page ? page.index : 0,
      type: meta.type,
      data: clone(meta.data),
      before: clone(meta.data),
    });
  } catch (err) {
    console.error('Failed to delete annotation:', err);
  }
}

// ---------------------------------------------------------------------------
// Stage input events (pointer events: mouse, touch, and stylus)
// ---------------------------------------------------------------------------

function bindStageEvents(page) {
  page.stage.on('pointerdown', (e) => {
    // A zoom gesture is in flight (CSS preview active): pointer coordinates
    // map to raster-scaled preview space, not committed stage points, so a
    // stray click must not draw/place anything until the gesture commits.
    if (zoomPreview) return;
    // Ignore interactions with the transformer's anchors.
    if (e.target.getParent() instanceof Konva.Transformer) return;

    if (state.tool === 'pen' || state.tool === 'highlighter') {
      e.evt.preventDefault();
      const pos = page.stage.getRelativePointerPosition();
      if (pos) startStroke(page, pos);
    } else if (state.tool === 'text' || state.tool === 'signature') {
      // preventDefault stops the browser's mousedown default from stealing
      // focus back to the canvas, which would instantly blur the overlay.
      e.evt.preventDefault();
      if (state.overlay) {
        closeOverlay(true);
        return;
      }
      const pos = page.stage.getRelativePointerPosition();
      if (pos) openTextOverlay(page, pos, null);
    } else if (state.tool === 'stamp') {
      e.evt.preventDefault();
      const pos = page.stage.getRelativePointerPosition();
      if (pos) placeStamp(page, pos);
    } else if (state.tool === 'cloud') {
      e.evt.preventDefault();
      const pos = page.stage.getRelativePointerPosition();
      if (pos) startCloud(page, pos);
    } else if (state.tool === 'eraser') {
      e.evt.preventDefault();
      const pos = page.stage.getRelativePointerPosition();
      if (pos) startErasing(page, pos);
    } else if (state.tool === 'select') {
      const node = findMetaNode(e.target);
      const meta = getMeta(node);
      if (meta && meta.isOwner && EDITABLE_TYPES.includes(meta.type)) {
        select(page, node);
      } else {
        deselect(); // other users' / read-only annotations and empty space
      }
    }
  });

  // Eraser cursor tracking: a dashed grey circle following the pointer.
  page.stage.on('pointermove', () => {
    if (state.tool !== 'eraser' || zoomPreview) return;
    const pos = page.stage.getRelativePointerPosition();
    if (pos) updateEraserCursor(page, pos);
  });
  page.stage.on('pointerleave', () => {
    if (state.tool === 'eraser' && !state.erasing) hideEraserCursor();
  });

  // pointermove/up are bound on window so strokes survive leaving the stage.
}

window.addEventListener('pointermove', (e) => {
  if (state.drawing) {
    e.preventDefault();
    const { page } = state.drawing;
    page.stage.setPointersPositions(e);
    const pos = page.stage.getRelativePointerPosition();
    if (pos) extendStroke(pos);
  } else if (state.cloud) {
    e.preventDefault();
    const { page } = state.cloud;
    page.stage.setPointersPositions(e);
    const pos = page.stage.getRelativePointerPosition();
    if (pos) extendCloud(pos);
  } else if (state.erasing) {
    e.preventDefault();
    const { page } = state.erasing;
    page.stage.setPointersPositions(e);
    const pos = page.stage.getRelativePointerPosition();
    if (pos) {
      updateEraserCursor(page, pos);
      applyEraserAt(page, pos);
    }
  }
});

window.addEventListener('pointerup', () => {
  if (state.drawing) finishStroke();
  else if (state.cloud) finishCloud();
  else if (state.erasing) finishErasing();
});

window.addEventListener('keydown', (e) => {
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'textarea' || tag === 'input') return;

  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    if (e.shiftKey) redo();
    else undo();
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
    e.preventDefault();
    redo();
  } else if (e.key === 'Delete' || e.key === 'Backspace') {
    if (state.selected) {
      e.preventDefault();
      deleteSelected();
    }
  }
});

deleteBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  deleteSelected();
});

// ---------------------------------------------------------------------------
// Zoom — pdf.js-style two-phase model
//
//  Phase 1 (per wheel event, during the gesture): a pure CSS transform on
//  #sp-pages previews the new scale. This is O(css-compositor) — zero Konva
//  work, no canvas reallocation — so the gesture stays smooth. Content may
//  look raster-scaled/blurry while pinching; that matches pdf.js.
//
//  Phase 2 (once, ~180ms after the last wheel event): clear the preview and
//  run the committed path (setZooms) exactly once. Stage units stay PDF
//  points and committed scaling is exclusively stage.scale; the CSS preview
//  transform must NEVER survive commit or it corrupts pointer coordinate math.
//
//  Discrete entry points (toolbar ± buttons, __sherbertEditor.setZoom) commit
//  immediately via setZooms with no preview.
// ---------------------------------------------------------------------------

const ZOOM_COMMIT_DELAY = 180;
// active gesture: { pending, anchorAx, anchorAy, startScrollLeft, startScrollTop, anchorPage }
let zoomPreview = null;
let zoomCommitTimer = null;

/* Widest page — the fallback anchor for the per-axis fit test when the cursor
 * resolves to no page (e.g. an empty document). */
function widestPage() {
  let best = null;
  for (const p of state.pages) if (!best || p.widthPts > best.widthPts) best = p;
  return best;
}

/* Per-axis fit test at a given zoom for the gesture's anchor page: does the
 * page's DISPLAYED size (widthPts * RENDER_SCALE * zoom) fit within the scroll
 * viewport on each axis? A fitting axis is centered (h) / top-anchored (v)
 * during zoom; an overflowing axis is cursor-anchored (pdf.js directional). */
function axisFits(page, zoom) {
  const s = RENDER_SCALE * zoom;
  return {
    x: !page || page.widthPts * s <= scrollEl.clientWidth + 0.5,
    y: !page || page.heightPts * s <= scrollEl.clientHeight + 0.5,
  };
}

/* Resolve a viewport point to a document anchor. (px, py) are cursor coords
 * relative to scrollEl's client rect. Returns { pageIndex, pointPts } where
 * pointPts is the point in PDF points within that page, or null if there are
 * no pages. MUST be called against the committed layout (no CSS preview
 * transform applied), since it reads getBoundingClientRect at the current
 * committed scale k(). The cursor is attributed to the page it falls inside
 * vertically, or the nearest page if it lands in a gutter/margin — matching
 * how pdf.js anchors zoom to a document point rather than a screen point. */
function resolveAnchor(px, py) {
  if (!state.pages.length) return null;
  const scrollRect = scrollEl.getBoundingClientRect();
  const clientX = scrollRect.left + px;
  const clientY = scrollRect.top + py;
  const scale = k();

  let best = null;
  let bestDist = Infinity;
  for (const page of state.pages) {
    const r = page.wrap.getBoundingClientRect();
    // Vertical distance from cursor to this page's rect (0 when inside it).
    const dy = clientY < r.top ? r.top - clientY : clientY > r.bottom ? clientY - r.bottom : 0;
    if (dy < bestDist) {
      bestDist = dy;
      best = { page, r };
      if (dy === 0) break; // pages stack top-to-bottom; first hit is the one
    }
  }
  const { page, r } = best;
  return {
    pageIndex: page.index,
    pointPts: { x: (clientX - r.left) / scale, y: (clientY - r.top) / scale },
  };
}

/* setZooms is the single commit primitive: it applies `newZoom` via
 * stage.scale + stage.size + batchDraw across all pages and re-anchors the
 * scroll so the document point under (anchorX, anchorY) — cursor coords
 * relative to scrollEl's client rect — stays stationary (pdf.js-style).
 *
 * Anchoring is by document point, NOT by proportional scroll scaling: pages
 * are auto-centered (margin: 0 auto) and separated by fixed-pixel gaps, so a
 * content coordinate does not scale linearly with zoom. We instead resolve the
 * anchor to a page + PDF point BEFORE the resize, then read the reflowed page
 * position AFTER it and scroll so that point lands back under the cursor. */
function setZooms(newZoom, anchorX, anchorY) {
  // Defensive: the committed path owns stage.scale exclusively. Any lingering
  // CSS preview transform would desync container pixels from stage points.
  if (pagesEl.style.transform) {
    pagesEl.style.transform = '';
    pagesEl.style.transformOrigin = '';
  }

  newZoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, newZoom));
  if (newZoom === state.zoom) return;

  const px = anchorX == null ? scrollEl.clientWidth / 2 : anchorX;
  const py = anchorY == null ? scrollEl.clientHeight / 2 : anchorY;
  // Resolve the anchor against the CURRENT (pre-zoom) committed layout.
  const anchor = resolveAnchor(px, py);

  state.zoom = newZoom;
  const scale = k();
  // Resize/redraw VISIBLE stages now (bounded, pixel-budgeted); defer the rest
  // so peak memory and commit cost track visible pages, not document length.
  for (const page of state.pages) {
    if (pageIsNearViewport(page)) applyStageScale(page, scale);
    else deferStageScale(page, scale);
  }

  // Re-anchor PER-AXIS (pdf.js). Read the reflowed page position
  // (getBoundingClientRect forces the one synchronous reflow we need).
  //  - Overflowing axis: scroll so the anchored document point lands back under
  //    the cursor (directional zoom); the browser clamps at the edges.
  //  - Fitting axis: no overflow, so margin:auto centering (h) / the top clamp
  //    (v) owns the position. Force scroll to 0 to land exactly where the CSS
  //    preview showed the page — this is what makes "no step at commit" hold.
  if (anchor) {
    const anchorPage = state.pages[anchor.pageIndex];
    const fits = axisFits(anchorPage, state.zoom);
    const scrollRect = scrollEl.getBoundingClientRect();
    const r = anchorPage.wrap.getBoundingClientRect();
    // pageContentLeft is scroll-independent: scrollLeft cancels against the
    // scroll baked into (r.left - scrollRect.left).
    const contentX = scrollEl.scrollLeft + (r.left - scrollRect.left) + anchor.pointPts.x * scale;
    const contentY = scrollEl.scrollTop + (r.top - scrollRect.top) + anchor.pointPts.y * scale;
    scrollEl.scrollLeft = fits.x ? 0 : contentX - px;
    scrollEl.scrollTop = fits.y ? 0 : contentY - py;
  }

  document.getElementById('sp-zoom-label').textContent = `${Math.round(state.zoom * 100)}%`;

  if (state.selected) {
    const found = findNode(getMeta(state.selected).id);
    if (found) positionDeleteButton(found.page, state.selected);
  }
  if (state.overlay) closeOverlay(true);

  // Re-render the page bitmaps sharply for the new effective scale.
  scheduleBitmapRerender();
}

/* Phase 2: clear the preview and commit the accumulated zoom exactly once. */
function commitZoomGesture() {
  clearTimeout(zoomCommitTimer);
  zoomCommitTimer = null;
  const gesture = zoomPreview;
  zoomPreview = null;
  if (!gesture) return;

  // Drop the CSS preview BEFORE committing; setZooms re-anchors the scroll
  // against the same anchor the gesture captured, so the content point that
  // was under the cursor stays under it once stage.scale takes over.
  pagesEl.style.transform = '';
  pagesEl.style.transformOrigin = '';
  setZooms(gesture.pending, gesture.anchorAx, gesture.anchorAy);

  // Assert the preview transform did not survive the commit.
  if (pagesEl.style.transform) {
    pagesEl.style.transform = '';
    pagesEl.style.transformOrigin = '';
  }
}

/* Phase 1: accumulate the pending zoom and preview it with a CSS transform.
 * `ax`/`ay` are the cursor position relative to scrollEl's client rect. */
function previewZoom(deltaY, deltaMode, ax, ay) {
  // Proportional exponential mapping: smooth for trackpad pinch (many small
  // pixel deltas) and responsive for notched mice. deltaMode 1 is line-based
  // (Firefox) and needs a larger coefficient than pixels.
  const factor = Math.exp(-deltaY * (deltaMode === 1 ? 0.03 : 0.002));

  if (!zoomPreview) {
    // First event of the gesture fixes the anchor. #sp-pages is the sole child
    // of #sp-scroll (no scroll-container padding/border), so a cursor point
    // (ax, ay) in the viewport maps to #sp-pages content coordinates
    // A = (scrollLeft + ax, scrollTop + ay). The anchor PAGE (page under the
    // cursor, else the widest) drives the per-axis fit test each event.
    const anchor = resolveAnchor(ax, ay);
    zoomPreview = {
      pending: state.zoom,
      anchorAx: ax,
      anchorAy: ay,
      startScrollLeft: scrollEl.scrollLeft,
      startScrollTop: scrollEl.scrollTop,
      anchorPage: anchor ? state.pages[anchor.pageIndex] : widestPage(),
    };
    // Transient UI would be mis-positioned relative to preview-scaled content.
    deleteBtn.style.display = 'none';
    if (state.overlay) closeOverlay(true);
  }

  const pending = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoomPreview.pending * factor));
  zoomPreview.pending = pending;

  // Preview with an explicit `translate(tx, ty) scale(r)` about origin 0,0 so
  // tx and ty are chosen INDEPENDENTLY per axis (r = pending/committed). This
  // is what lets a fitting axis stay centered/top while an overflowing axis
  // tracks the cursor — a single transform-origin cannot express both.
  const r = pending / state.zoom;
  const fits = axisFits(zoomPreview.anchorPage, pending);
  // A = committed content point under the cursor at gesture start (fixed for
  // the whole gesture; the preview never changes scroll).
  const ax0 = zoomPreview.startScrollLeft + zoomPreview.anchorAx;
  const ay0 = zoomPreview.startScrollTop + zoomPreview.anchorAy;
  //  - fitting x: page center (viewport center, scrollLeft 0) stays put.
  //  - fitting y: the top edge (content at startScrollTop, ~0) stays put.
  //  - overflowing axis: the content point A under the cursor stays put:
  //    displayed = t + r*A - scroll must equal cursor  =>  t = A*(1 - r).
  const tx = fits.x ? (scrollEl.clientWidth / 2) * (1 - r) : ax0 * (1 - r);
  const ty = fits.y ? zoomPreview.startScrollTop * (1 - r) : ay0 * (1 - r);

  pagesEl.style.transformOrigin = '0 0';
  pagesEl.style.transform = `translate(${tx}px, ${ty}px) scale(${r})`;

  document.getElementById('sp-zoom-label').textContent = `${Math.round(pending * 100)}%`;

  clearTimeout(zoomCommitTimer);
  zoomCommitTimer = setTimeout(commitZoomGesture, ZOOM_COMMIT_DELAY);
}

scrollEl.addEventListener(
  'wheel',
  (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    const rect = scrollEl.getBoundingClientRect();
    previewZoom(e.deltaY, e.deltaMode, e.clientX - rect.left, e.clientY - rect.top);
  },
  { passive: false }
);

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

function applyTouchAction() {
  const canvasTool = state.tool !== 'select';
  for (const page of state.pages) {
    page.wrap.style.touchAction = canvasTool ? 'none' : 'auto';
    let cursor = 'crosshair';
    if (state.tool === 'select') cursor = 'default';
    else if (state.tool === 'text' || state.tool === 'signature') cursor = 'text';
    else if (state.tool === 'eraser') cursor = 'none'; // the Konva cursor circle stands in
    else if (state.tool === 'stamp') cursor = 'copy';
    page.stage.container().style.cursor = cursor;
  }
}

function setTool(tool) {
  if (state.overlay) closeOverlay(true);
  if (tool !== 'select') deselect();
  if (tool !== 'eraser') hideEraserCursor();
  state.tool = tool;
  document.querySelectorAll('#sp-toolbar .sp-tool').forEach((btn) => {
    btn.classList.toggle('sp-active', btn.dataset.tool === tool);
  });
  document.querySelectorAll('#sp-toolbar .sp-swatches').forEach((group) => {
    group.classList.toggle('sp-visible', group.dataset.for === tool);
  });
  applyTouchAction();
}

/* Build the stamp palette swatch-row from the configured stamps (thumbnails).
 * Clicking a thumbnail selects it as the active stamp. */
function buildStampSwatches() {
  const container = document.getElementById('sp-stamp-swatches');
  if (!container) return;
  container.innerHTML = '';
  state.stamps.forEach((stamp, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'sp-stamp-swatch' + (i === state.selectedStamp ? ' sp-active' : '');
    btn.dataset.stampIndex = String(i);
    btn.title = stamp.label || `Stamp ${i + 1}`;
    const img = document.createElement('img');
    img.src = stamp.url;
    img.alt = stamp.label || '';
    btn.appendChild(img);
    btn.addEventListener('click', () => {
      state.selectedStamp = i;
      container.querySelectorAll('.sp-stamp-swatch').forEach((s) => s.classList.remove('sp-active'));
      btn.classList.add('sp-active');
    });
    container.appendChild(btn);
  });
}

function wireToolbar() {
  buildStampSwatches();

  document.querySelectorAll('#sp-toolbar .sp-tool').forEach((btn) => {
    btn.addEventListener('click', () => setTool(btn.dataset.tool));
  });

  document.querySelectorAll('#sp-toolbar .sp-swatches').forEach((group) => {
    const tool = group.dataset.for;
    group.querySelectorAll('.sp-swatch').forEach((swatch) => {
      swatch.addEventListener('click', () => {
        state.colors[tool] = swatch.dataset.color;
        group.querySelectorAll('.sp-swatch').forEach((s) => s.classList.remove('sp-active'));
        swatch.classList.add('sp-active');
      });
    });
  });

  document.querySelectorAll('#sp-toolbar .sp-size').forEach((input) => {
    input.addEventListener('input', () => {
      state.sizes[input.dataset.for] = parseInt(input.value, 10);
    });
  });

  document.getElementById('sp-undo').addEventListener('click', undo);
  document.getElementById('sp-redo').addEventListener('click', redo);
  document.getElementById('sp-zoom-in').addEventListener('click', () => setZooms(state.zoom * ZOOM_STEP));
  document.getElementById('sp-zoom-out').addEventListener('click', () => setZooms(state.zoom / ZOOM_STEP));
}

// ---------------------------------------------------------------------------
// Debug handle (used by e2e tests)
// ---------------------------------------------------------------------------

window.__sherbertEditor = {
  ready: false,
  state,
  nodeCount(pageIndex) {
    const page = state.pages[pageIndex];
    if (!page) return 0;
    return page.annLayer.getChildren((n) => !!getMeta(n)).length;
  },
  zoom() {
    return state.zoom;
  },
  setZoom(z) {
    setZooms(z);
  },
  bitmapScale(pageIndex) {
    const page = state.pages[pageIndex];
    return page ? page.bitmapScale : null;
  },
  /* Client-rect (viewport pixels) of the named Transformer anchor for the
   * current selection, e.g. 'middle-right' or 'bottom-right'. Returns the
   * center too, for driving a mouse drag from the anchor. */
  anchorRect(name) {
    const node = state.selected;
    if (!node) return null;
    const found = findNode(getMeta(node).id);
    if (!found) return null;
    const anchor = found.page.transformer.findOne('.' + name);
    if (!anchor) return null;
    const r = anchor.getClientRect(); // container pixels (includes stage scale)
    const cont = found.page.stage.container().getBoundingClientRect();
    return {
      x: cont.left + r.x,
      y: cont.top + r.y,
      width: r.width,
      height: r.height,
      centerX: cont.left + r.x + r.width / 2,
      centerY: cont.top + r.y + r.height / 2,
    };
  },
  /* Konva box width (PDF points) of the first text node on a page — used to
   * assert stored wrap widths round-trip through a reload. */
  textNodeWidth(pageIndex) {
    const page = state.pages[pageIndex];
    if (!page) return null;
    const node = page.annLayer.getChildren((n) => {
      const meta = getMeta(n);
      return meta && meta.type === 'text';
    })[0];
    return node ? node.width() : null;
  },
  /* Count of meta-bearing nodes of a given annotation type on a page. */
  typeCount(pageIndex, type) {
    const page = state.pages[pageIndex];
    if (!page) return 0;
    return page.annLayer.getChildren((n) => {
      const meta = getMeta(n);
      return meta && meta.type === type;
    }).length;
  },
  /* Select the first annotation of the given type on a page (drives resize
   * e2e for stamps/clouds without needing a precise on-canvas click). */
  selectFirst(pageIndex, type) {
    const page = state.pages[pageIndex];
    if (!page) return false;
    const node = page.annLayer.getChildren((n) => {
      const meta = getMeta(n);
      return meta && meta.type === type;
    })[0];
    if (!node) return false;
    setTool('select');
    select(page, node);
    return true;
  },
  /* Choose the active stamp from the configured palette by index. */
  pickStamp(i) {
    state.selectedStamp = i;
    const btns = document.querySelectorAll('#sp-stamp-swatches .sp-stamp-swatch');
    btns.forEach((b, j) => b.classList.toggle('sp-active', j === i));
  },
  setTool,
};

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async function init() {
  wireToolbar();
  setTool('pen');
  try {
    await renderPages();
    await loadAnnotations();
    window.__sherbertEditor.ready = true;
  } catch (err) {
    console.error('Sherbert editor failed to initialize:', err);
    window.__sherbertEditor.error = String(err);
  }
})();
