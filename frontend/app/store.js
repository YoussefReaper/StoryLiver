/* The state layer.
 *
 * app.js grew to five thousand lines with one shared `S` object that every
 * render function read and any of them could write. That is workable at a
 * thousand lines and stops being workable well before five: nothing declares
 * what it depends on, so a change anywhere means re-rendering everything and
 * hoping.
 *
 * This is deliberately small - a plain object, a subscribe, and a notify. No
 * framework, because the project has NO BUILD STEP and that is worth keeping:
 * `python run.py` serves the frontend as static files, and the layout tests
 * read the real CSS and HTML off disk. A bundler would cost both.
 */

const listeners = new Map();          // key -> Set<fn>
let nextId = 1;

const state = {};

/** Read a slice. Returns undefined rather than throwing on an unset key. */
export function get(key) {
  return state[key];
}

/** Write a slice and wake only what asked for it. */
export function set(key, value) {
  const previous = state[key];
  if (previous === value) return value;
  state[key] = value;
  notify(key, value, previous);
  return value;
}

/** Merge into an object slice. Convenience for the common patch case. */
export function patch(key, partial) {
  return set(key, { ...(state[key] || {}), ...partial });
}

/** Subscribe to one key. Returns an unsubscribe. */
export function on(key, fn) {
  if (!listeners.has(key)) listeners.set(key, new Map());
  const id = nextId += 1;
  listeners.get(key).set(id, fn);
  return () => listeners.get(key)?.delete(id);
}

function notify(key, value, previous) {
  for (const fn of listeners.get(key)?.values() || []) {
    try {
      fn(value, previous);
    } catch (err) {
      // One broken subscriber must not stop the others: a render that throws
      // should cost its own panel, never the whole screen.
      console.error(`[store] subscriber for ${key} threw`, err);
    }
  }
}

/** For debugging and for tests - never mutate the result. */
export function snapshot() {
  return { ...state };
}
