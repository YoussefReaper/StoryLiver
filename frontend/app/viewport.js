/* Pan and zoom for the SVG panels.
 *
 * Neither map had any of this. The Atlas carried `cursor: grab` in CSS with
 * NO drag handler behind it - a promise the code never kept - and Threads
 * simply widened its viewBox as the story grew until the far end was off
 * screen with no way to reach it. On a long run the narrative graph became
 * unusable exactly when it became interesting.
 *
 * One module drives both, because they are the same problem: an SVG larger
 * than its frame. It works on the `viewBox` rather than a CSS transform so
 * that stroke widths and text stay crisp at every zoom, and so a click still
 * hits the shape the player thinks they clicked.
 */

const MIN_SCALE = 0.35;
const MAX_SCALE = 3.0;

const attached = new WeakMap();

/**
 * Make an <svg> pannable and zoomable.
 *
 * `base` is the untransformed viewBox - the natural extent of the content.
 * Panels recompute it when their content grows, so pass it again on redraw
 * and the current view is preserved rather than snapping back.
 */
export function attach(svg, base) {
  if (!svg) return null;
  let view = attached.get(svg);
  if (!view) {
    view = { x: 0, y: 0, scale: 1, base, dragging: false, moved: false };
    attached.set(svg, view);
    bind(svg, view);
  }
  view.base = base;
  apply(svg, view);
  return view;
}

/** Frame the whole content again. */
export function reset(svg) {
  const view = attached.get(svg);
  if (!view) return;
  view.x = 0;
  view.y = 0;
  view.scale = 1;
  apply(svg, view);
}

/** Centre a point in the content's own coordinates. */
export function centreOn(svg, cx, cy) {
  const view = attached.get(svg);
  if (!view) return;
  const [, , w, h] = view.base;
  view.x = cx - w / (2 * view.scale);
  view.y = cy - h / (2 * view.scale);
  apply(svg, view);
}

/**
 * Did the pointer MOVE between down and up? A panel's click handler asks
 * this so that dragging across a node does not also select it - without it
 * every pan that happens to start on a node opens that node's detail.
 */
export function wasDrag(svg) {
  return Boolean(attached.get(svg)?.moved);
}

function apply(svg, view) {
  const [bx, by, bw, bh] = view.base;
  const w = bw / view.scale;
  const h = bh / view.scale;
  // Clamped so the content cannot be flung off into empty space and lost.
  const maxX = bx + bw - w / 2;
  const maxY = by + bh - h / 2;
  view.x = Math.max(bx - w / 2, Math.min(maxX, view.x));
  view.y = Math.max(by - h / 2, Math.min(maxY, view.y));
  svg.setAttribute('viewBox', `${view.x} ${view.y} ${w} ${h}`);
}

function bind(svg, view) {
  svg.style.cursor = 'grab';
  svg.style.touchAction = 'none';

  let startX = 0;
  let startY = 0;
  let originX = 0;
  let originY = 0;

  svg.addEventListener('pointerdown', (e) => {
    view.dragging = true;
    view.moved = false;
    startX = e.clientX;
    startY = e.clientY;
    originX = view.x;
    originY = view.y;
    svg.setPointerCapture(e.pointerId);
    svg.style.cursor = 'grabbing';
  });

  svg.addEventListener('pointermove', (e) => {
    if (!view.dragging) return;
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    // Convert screen pixels to content units so a drag tracks the cursor
    // exactly, at any zoom.
    const dx = (e.clientX - startX) * (view.base[2] / view.scale) / rect.width;
    const dy = (e.clientY - startY) * (view.base[3] / view.scale) / rect.height;
    if (Math.abs(e.clientX - startX) > 3 || Math.abs(e.clientY - startY) > 3) {
      view.moved = true;
    }
    view.x = originX - dx;
    view.y = originY - dy;
    apply(svg, view);
  });

  const end = (e) => {
    if (!view.dragging) return;
    view.dragging = false;
    svg.style.cursor = 'grab';
    try { svg.releasePointerCapture(e.pointerId); } catch { /* already gone */ }
  };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);

  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    // Zoom toward the cursor rather than the centre: zooming to the middle of
    // a map you are reading the edge of moves the thing you were looking at.
    const px = (e.clientX - rect.left) / rect.width;
    const py = (e.clientY - rect.top) / rect.height;
    const before = view.scale;
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    view.scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, view.scale * factor));
    const [, , bw, bh] = view.base;
    view.x += (bw / before - bw / view.scale) * px;
    view.y += (bh / before - bh / view.scale) * py;
    apply(svg, view);
  }, { passive: false });

  // Keyboard, because a map you can only reach with a mouse is a map some
  // people cannot reach at all.
  svg.setAttribute('tabindex', '0');
  svg.addEventListener('keydown', (e) => {
    const step = view.base[2] / view.scale / 12;
    const moves = {
      ArrowLeft: [-step, 0], ArrowRight: [step, 0],
      ArrowUp: [0, -step], ArrowDown: [0, step],
    };
    if (moves[e.key]) {
      e.preventDefault();
      view.x += moves[e.key][0];
      view.y += moves[e.key][1];
      apply(svg, view);
      return;
    }
    if (e.key === '+' || e.key === '=') {
      view.scale = Math.min(MAX_SCALE, view.scale * 1.15);
      apply(svg, view);
    } else if (e.key === '-') {
      view.scale = Math.max(MIN_SCALE, view.scale / 1.15);
      apply(svg, view);
    } else if (e.key === '0') {
      reset(svg);
    }
  });
}
