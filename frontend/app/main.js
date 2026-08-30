/* Entry point for the rebuilt panels.
 *
 * The old app.js is a five-thousand-line script with one shared mutable
 * object and no module boundaries. Rewriting it in one sweep would mean
 * breaking everything at once and hoping; instead the rebuilt panels live
 * here as ES modules and take over one at a time, starting with the World
 * Forge because it is the screen the whole product depends on.
 *
 * The two files talk through a narrow bridge (`window.__appBridge`) rather
 * than sharing scope. That boundary is the point: it is what stops the new
 * code growing back into the old shape.
 */

import * as forge from './forge.js';
import * as viewport from './viewport.js';
import { $ } from './dom.js';

function bridge() {
  return window.__appBridge || {};
}

function ready(fn) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fn, { once: true });
  } else {
    fn();
  }
}

ready(() => {
  const b = bridge();
  if (!b.api) {
    console.warn('[app] bridge missing - rebuilt panels are inert');
    return;
  }

  forge.init({
    apiFn: b.api,
    userId: b.userId,
    onWorldBuilt: (world) => b.onWorldBuilt(world),
  });

  // Pan and zoom, handed to the old script because both maps still render
  // there. Neither had any of it: the Atlas carried `cursor: grab` with no
  // drag handler behind it, and Threads grew past its own frame with no way
  // to reach the far end.
  window.__viewport = viewport;

  // The forge opens into the existing modal chrome, so the shell, the scrim
  // and the close button keep behaving exactly as they do everywhere else.
  window.__forge = {
    openModal() {
      b.showModal(`${b.head('World Forge',
        'You are the world-builder. Nobody here is an author.')}
        <div class="modal-body"><div id="forgeBody"></div></div>`);
      forge.open();
    },
  };

  // One listener for the rebuilt panels. Delegated, because every panel here
  // re-renders its own subtree and bound handlers would not survive it.
  document.addEventListener('click', (ev) => {
    try {
      forge.handle(ev.target);
    } catch (err) {
      b.toast(err.message || 'That did not work.', 'err');
    }
  });
  document.addEventListener('input', (ev) => forge.onInput(ev.target));
});
