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
const EDITABLE_TYPES = ['pen', 'highlighter', 'text'];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  zoom: 1,
  tool: 'pen',
  colors: { pen: 'black', highlighter: 'yellow', text: 'black' },
  sizes: { pen: 2, highlighter: 20, text: 14 },
  pages: [], // { index, widthPts, heightPts, wrap, stage, bgLayer, annLayer, transformer }
  drawing: null, // { page, line }
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
      rendering: false,
    };
    state.pages.push(page);
    bindStageEvents(page);
  }
  applyTouchAction();
}

// ---------------------------------------------------------------------------
// Crisp zoom: re-render page bitmaps at the effective scale once zoom
// settles (the Mozilla pdf.js approach) — visible pages only, debounced,
// resolution capped to bound memory on large drawings.
// ---------------------------------------------------------------------------

const BITMAP_MAX_SCALE = 6;
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

async function rerenderVisibleBitmaps() {
  const dpr = window.devicePixelRatio || 1;
  const target = Math.min(
    Math.max(RENDER_SCALE, state.zoom * RENDER_SCALE) * dpr,
    BITMAP_MAX_SCALE
  );
  for (const page of state.pages) {
    if (page.rendering || Math.abs(page.bitmapScale - target) < 0.01) continue;
    if (!pageIsNearViewport(page)) continue;
    page.rendering = true;
    try {
      const viewport = page.pdfPage.getViewport({ scale: target });
      const canvas = document.createElement('canvas');
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
  }
}

// Pages scrolled into view after a zoom still need their sharp bitmap.
scrollEl.addEventListener('scroll', scheduleBitmapRerender, { passive: true });

// ---------------------------------------------------------------------------
// Node materialization (shared by initial load, create, and undo/redo replay)
// ---------------------------------------------------------------------------

function setMeta(node, meta) {
  node.setAttr('sherbert', meta);
}

function getMeta(node) {
  return node && typeof node.getAttr === 'function' ? node.getAttr('sherbert') : null;
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
    node = new Konva.Line({
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
  } else if (type === 'text') {
    const rect = data.rect || [0, 0, 100, 30];
    node = new Konva.Text({
      x: rect[0],
      y: rect[1],
      text: data.content || '',
      fontSize: data.fontSize || 14,
      fontFamily: data.fontFamily || 'Arial, sans-serif',
      fontStyle: data.fontStyle === 'italic' ? 'italic' : 'normal',
      fill: rgbToCss(data.colors && data.colors.stroke),
    });
  } else if (type === 'cloud') {
    // Read-only in v1.
    const seg = (data.vertices && data.vertices[0]) || [];
    let pairs;
    if (seg.length === 1 && seg[0].length === 4) {
      const [rx, ry, rw, rh] = seg[0];
      pairs = cloudPoints(rx, ry, rw, rh);
    } else {
      pairs = seg;
    }
    if (!pairs || pairs.length < 2) return null;
    node = new Konva.Line({
      points: pairsToFlat(pairs),
      stroke: rgbToCss(data.colors && data.colors.stroke),
      strokeWidth: (data.border && data.border.width) || 1,
      lineCap: 'round',
      lineJoin: 'round',
      closed: true,
      opacity: data.opacity == null ? 1 : data.opacity,
      listening: false,
    });
  } else if (type === 'stamp') {
    // Read-only in v1.
    node = new Konva.Image({
      x: data.x || 0,
      y: data.y || 0,
      width: data.width || 100,
      height: data.height || 50,
      listening: false,
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

  if (type === 'text' && record.is_owner) {
    node.on('dblclick dbltap', () => openTextOverlay(page, null, node));
  }

  page.annLayer.add(node);
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
  const fontSize = meta ? meta.data.fontSize || 14 : state.sizes.text;
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
  ta.style.fontFamily = 'Arial, sans-serif';
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
    commit: () => commitText(page, pos, ta.value, existingNode, fontSize, colorCss),
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

async function commitText(page, posPts, raw, existingNode, fontSize, colorCss) {
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
  const node = new Konva.Text({
    x: posPts.x,
    y: posPts.y,
    text: content,
    fontSize,
    fontFamily: 'Arial, sans-serif',
    fontStyle: 'normal',
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
    fontFamily: 'Arial, sans-serif',
    fontStyle: 'normal',
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
// Select tool: Transformer + drag-to-move; bake transforms into geometry
// ---------------------------------------------------------------------------

function select(page, node) {
  if (state.selected === node) return;
  deselect();
  state.selected = node;
  node.draggable(true);
  page.transformer.nodes([node]);
  page.transformer.moveToTop();
  page.annLayer.batchDraw();

  node.on('dragend.spsel transformend.spsel', () => bakeAndSave(page, node));
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

  if (node instanceof Konva.Line) {
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
    const newFontSize = Math.max(1, Math.round(node.fontSize() * sy));
    node.fontSize(newFontSize);
    node.scale({ x: 1, y: 1 });
    meta.data.fontSize = newFontSize;
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
    // Ignore interactions with the transformer's anchors.
    if (e.target.getParent() instanceof Konva.Transformer) return;

    if (state.tool === 'pen' || state.tool === 'highlighter') {
      e.evt.preventDefault();
      const pos = page.stage.getRelativePointerPosition();
      if (pos) startStroke(page, pos);
    } else if (state.tool === 'text') {
      // preventDefault stops the browser's mousedown default from stealing
      // focus back to the canvas, which would instantly blur the overlay.
      e.evt.preventDefault();
      if (state.overlay) {
        closeOverlay(true);
        return;
      }
      const pos = page.stage.getRelativePointerPosition();
      if (pos) openTextOverlay(page, pos, null);
    } else if (state.tool === 'select') {
      const meta = getMeta(e.target);
      if (meta && meta.isOwner && EDITABLE_TYPES.includes(meta.type)) {
        select(page, e.target);
      } else if (e.target === page.stage || !meta) {
        deselect();
      } else {
        deselect(); // other users' or read-only annotations are not selectable
      }
    }
  });

  // pointermove/up are bound on window so strokes survive leaving the stage.
}

window.addEventListener('pointermove', (e) => {
  if (!state.drawing) return;
  e.preventDefault();
  const { page } = state.drawing;
  page.stage.setPointersPositions(e);
  const pos = page.stage.getRelativePointerPosition();
  if (pos) extendStroke(pos);
});

window.addEventListener('pointerup', () => {
  if (state.drawing) finishStroke();
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
// Zoom: ctrl+wheel and toolbar buttons; applied ONLY via stage.scale
// ---------------------------------------------------------------------------

function setZooms(newZoom, anchorX, anchorY) {
  newZoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, newZoom));
  if (newZoom === state.zoom) return;

  const px = anchorX == null ? scrollEl.clientWidth / 2 : anchorX;
  const py = anchorY == null ? scrollEl.clientHeight / 2 : anchorY;
  const contentX = scrollEl.scrollLeft + px;
  const contentY = scrollEl.scrollTop + py;
  const ratio = newZoom / state.zoom;

  state.zoom = newZoom;
  const scale = k();
  for (const page of state.pages) {
    page.stage.scale({ x: scale, y: scale });
    page.stage.size({ width: page.widthPts * scale, height: page.heightPts * scale });
    page.stage.batchDraw();
  }

  // Zoom-to-cursor: keep the content point under the anchor stationary.
  scrollEl.scrollLeft = contentX * ratio - px;
  scrollEl.scrollTop = contentY * ratio - py;

  document.getElementById('sp-zoom-label').textContent = `${Math.round(state.zoom * 100)}%`;

  if (state.selected) {
    const found = findNode(getMeta(state.selected).id);
    if (found) positionDeleteButton(found.page, state.selected);
  }
  if (state.overlay) closeOverlay(true);

  // Re-render the page bitmaps sharply for the new effective scale.
  scheduleBitmapRerender();
}

scrollEl.addEventListener(
  'wheel',
  (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    const rect = scrollEl.getBoundingClientRect();
    // Proportional exponential mapping: smooth for trackpad pinch (many
    // small pixel deltas) and responsive for notched mice. deltaMode 1 is
    // line-based (Firefox) and needs a larger coefficient than pixels.
    const factor = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.03 : 0.002));
    setZooms(state.zoom * factor, e.clientX - rect.left, e.clientY - rect.top);
  },
  { passive: false }
);

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

function applyTouchAction() {
  const drawingTool = ['pen', 'highlighter', 'text'].includes(state.tool);
  for (const page of state.pages) {
    page.wrap.style.touchAction = drawingTool ? 'none' : 'auto';
    page.stage.container().style.cursor =
      state.tool === 'select' ? 'default' : drawingTool && state.tool !== 'text' ? 'crosshair' : 'text';
  }
}

function setTool(tool) {
  if (state.overlay) closeOverlay(true);
  if (tool !== 'select') deselect();
  state.tool = tool;
  document.querySelectorAll('#sp-toolbar .sp-tool').forEach((btn) => {
    btn.classList.toggle('sp-active', btn.dataset.tool === tool);
  });
  document.querySelectorAll('#sp-toolbar .sp-swatches').forEach((group) => {
    group.classList.toggle('sp-visible', group.dataset.for === tool);
  });
  applyTouchAction();
}

function wireToolbar() {
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
