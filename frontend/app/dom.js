/* The handful of DOM helpers every panel needs, in one place.
 *
 * app.js redefined `esc` inline, escaped in three slightly different ways, and
 * built every panel by string concatenation with no shared idea of what a
 * field or a chip looks like. These are the primitives the rebuilt panels are
 * assembled from, so a control looks and behaves the same wherever it appears.
 */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** HTML-escape. The one and only version. */
export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** A labelled text field. */
export function field({ id, label, hint = '', value = '', placeholder = '', max = 160 }) {
  return `<label class="fld">
    <span class="fld-label">${esc(label)}${hint
    ? `<em class="fld-hint">${esc(hint)}</em>` : ''}</span>
    <input class="fld-input" id="${esc(id)}" maxlength="${max}"
           value="${esc(value)}" placeholder="${esc(placeholder)}">
  </label>`;
}

/**
 * A choosable card. `selected` is drawn, never inferred from the DOM - a
 * control whose truth lives in a class attribute is a control that
 * disagrees with the state the moment anything else re-renders.
 */
export function card({ attr, id, title, blurb, meta = '', selected = false,
  disabled = false }) {
  return `<button class="pick ${selected ? 'on' : ''}" ${attr}="${esc(id)}"
      ${disabled ? 'disabled' : ''}>
    <b class="pick-title">${esc(title)}</b>
    <small class="pick-blurb">${esc(blurb)}</small>
    ${meta ? `<small class="pick-meta">${esc(meta)}</small>` : ''}
  </button>`;
}

/** A row of choosable cards. */
export function cardRow(items) {
  return `<div class="pick-row">${items.join('')}</div>`;
}

/** A section heading with an optional right-aligned note. */
export function heading(title, note = '') {
  return `<div class="sec-head"><h3>${esc(title)}</h3>${note
    ? `<span class="sec-note">${esc(note)}</span>` : ''}</div>`;
}

/** A short explanatory line. Used for the "why this matters" text. */
export function note(text, kind = '') {
  return `<p class="sec-why ${kind}">${esc(text)}</p>`;
}
