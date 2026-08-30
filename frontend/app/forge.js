/* The World Forge - rebuilt.
 *
 * This is the most important screen in the product and it was a text box.
 * What the backend could already do and the UI never offered:
 *
 *   SCALE        worldforge.SCALES builds a town, a city, a region or a whole
 *                world, in that many passes, at an honestly stated cost. The
 *                UI never asked, so every world anyone ever built was a town.
 *
 *   SESSION ZERO the questions that decide what KIND of story this is - where
 *                on the timeline you arrive, where you sit in the world's own
 *                power system, what limits you. For a researched setting the
 *                options are the world's REAL arcs, so a player picks an arc
 *                rather than typing a guess. The endpoint existed and nothing
 *                called it.
 *
 *   THE PREMISE  a player writes a sentence, not a title. What we understood
 *                them to mean - the host world, who they are carrying in and
 *                from where - is now read back to them BEFORE they commit,
 *                because getting that wrong is expensive and silent.
 *
 * Built as steps rather than one wall of fields: each step asks one question
 * and says why it matters. The old modal asked for everything at once and
 * explained none of it, which is how you get a world nobody meant to build.
 */

import { $, $$, card, cardRow, esc, field, heading, note } from './dom.js';
import { get, on, patch, set } from './store.js';

const STEPS = [
  { id: 'premise', name: 'The world', blurb: 'What are we building?' },
  { id: 'shape', name: 'The shape', blurb: 'How big, and how it feels.' },
  { id: 'you', name: 'You in it', blurb: 'Where you stand.' },
  { id: 'build', name: 'Build', blurb: 'What you are about to get.' },
];

const SUGGESTIONS = [
  'a frozen post-collapse Earth',
  'I and Charlie from Hazbin Hotel are inside the world of The Last of Us',
  'a lighthouse at the end of the world',
  'Tanjiro from Demon Slayer, set in Middle-earth',
  'a generation ship two hundred years in',
  'a plague town in 1348',
];

const BUILD_MODES = [
  ['auto', 'Decide for me', 'Look it up. If it is real, continue it. If not, build it fresh.'],
  ['canon', 'Continue a real world', 'Use its actual characters, places and factions.'],
  ['original', 'Build my own', 'Nothing is looked up. Pure invention from what you wrote.'],
];

let api = null;          // injected, so this module has no transport of its own
let whoami = () => '';   // and no idea who the player is except through the bridge
let onBuilt = null;
let peekTimer = null;

export function init({ apiFn, userId, onWorldBuilt }) {
  api = apiFn;
  whoami = userId || (() => '');
  onBuilt = onWorldBuilt;
}

export function open() {
  set('forge', {
    step: 'premise', setting: '', tone: '', mode: 'auto', scale: 'town',
    peek: null, peeking: false, zero: null, answers: {}, building: false,
  });
  render();
  // Fetched alongside the first paint rather than blocking it: the premise
  // step does not need the numbers, and the size step is one click away.
  loadScales().then(() => {
    const f = get('forge');
    if (f && (f.step === 'shape' || f.step === 'build')) render();
  });
}

/* ------------------------------------------------------------------ view */

function render() {
  const box = $('#forgeBody');
  if (!box) return;
  const f = get('forge') || {};
  box.innerHTML = `
    <nav class="steps">${STEPS.map((s, i) => `
      <button class="step ${s.id === f.step ? 'on' : ''} ${stepDone(f, s.id) ? 'done' : ''}"
              data-forge-step="${s.id}">
        <em>${i + 1}</em><b>${esc(s.name)}</b><small>${esc(s.blurb)}</small>
      </button>`).join('')}</nav>
    <div class="step-body">${stepView(f)}</div>`;
}

function stepDone(f, id) {
  if (id === 'premise') return Boolean(f.setting.trim());
  if (id === 'shape') return Boolean(f.scale);
  if (id === 'you') return Object.keys(f.answers || {}).length > 0;
  return false;
}

function stepView(f) {
  if (f.step === 'premise') return premiseStep(f);
  if (f.step === 'shape') return shapeStep(f);
  if (f.step === 'you') return youStep(f);
  return buildStep(f);
}

/* --------------------------------------------------------------- premise */

function premiseStep(f) {
  return `
    ${heading('What are we building?')}
    ${note('Write it however you think about it. A title, a description, or a '
      + 'whole sentence — "Charlie from Hazbin Hotel, inside The Last of Us" '
      + 'is a thing you can ask for.')}
    <label class="fld">
      <span class="fld-label">The world</span>
      <textarea class="fld-input tall" id="forgeSetting" maxlength="240"
        placeholder="a frozen post-collapse Earth">${esc(f.setting)}</textarea>
    </label>
    <div class="suggests">${SUGGESTIONS.map((s) => `
      <button class="suggest" data-forge-suggest="${esc(s)}">${esc(s)}</button>`).join('')}</div>

    <div id="forgePeek" class="peek">${peekView(f)}</div>

    ${heading('How literally to take it')}
    ${cardRow(BUILD_MODES.map(([id, title, blurb]) => card({
    attr: 'data-forge-mode', id, title, blurb, selected: f.mode === id,
  })))}

    ${field({ id: 'forgeTone', label: 'Tone', hint: 'optional',
    value: f.tone, placeholder: 'quiet, cold, and merciless' })}

    <div class="step-nav">
      <button class="btn btn-primary" data-forge-next="shape"
        ${f.setting.trim() ? '' : 'disabled'}>Next — the shape</button>
    </div>`;
}

/**
 * The read-back. This is the whole reason the crossover bug was invisible:
 * the player typed a canon request, the parser understood it perfectly, and
 * nothing on screen told them what it had understood until a world arrived
 * that was not what they asked for.
 */
function peekView(f) {
  if (f.peeking) return '<div class="peek-busy">Reading that…</div>';
  const p = f.peek;
  if (!p || !f.setting.trim()) return '';
  const parsed = p.premise || {};
  const read = [];
  if (parsed.host) {
    read.push(`<div class="read-line"><i>World</i><b>${esc(parsed.host)}</b></div>`);
  }
  for (const im of parsed.imports || []) {
    read.push(`<div class="read-line"><i>Bringing in</i><b>${esc(im.character)}</b>
      <span>from ${esc(im.from)}</span></div>`);
  }

  const grounded = (p.grounded || []).length;
  const ungrounded = p.ungrounded || [];

  return `<div class="peek-box ${p.found ? 'found' : 'unknown'}">
    ${read.length ? `<div class="read">${read.join('')}</div>` : ''}
    ${p.found
    ? `<div class="peek-line"><b>Found it${p.canonical_name
      ? ` — ${esc(p.canonical_name)}` : ''}.</b>
        Real names from this setting will be used.</div>`
    : `<div class="peek-line"><b>${read.length
      ? 'Nothing confirmed by a wiki.'
      : 'An original setting.'}</b>
        ${read.length
      ? 'It will be built from what the model already knows about these.'
      : 'It will be built from imagination, which is exactly right here.'}</div>`}
    ${ungrounded.length && read.length
    ? `<div class="peek-partial">Unconfirmed: ${ungrounded.map(esc).join(', ')}</div>` : ''}
    ${grounded ? `<div class="peek-src">${grounded} looked up</div>` : ''}
  </div>`;
}

/* ----------------------------------------------------------------- shape */

/* The sizes, fetched rather than restated. This list used to be written out
   here by hand, a second hand-written copy sat in the backend's blurbs, and a
   third number was computed in scale_plan() - and all three disagreed. Every
   size was quoted one model call cheaper than it actually is, which is the
   one number a player is entitled to have right before they spend on it. */
const FALLBACK_SCALES = [
  { scale: 'town', label: 'A town', blurb: 'One dense, playable place.' },
  { scale: 'city', label: 'A city', blurb: 'Three districts, each with its own people.' },
  { scale: 'region', label: 'A region', blurb: 'Five settlements, connected by road.' },
  { scale: 'world', label: 'A whole world', blurb: "Eight regions, a continent's worth." },
];

let scalePlans = null;

async function loadScales() {
  if (scalePlans) return scalePlans;
  try {
    scalePlans = (await api('/forge/scales')).scales || FALLBACK_SCALES;
  } catch {
    // The step still works without the numbers; it just cannot promise them.
    scalePlans = FALLBACK_SCALES;
  }
  return scalePlans;
}

/** "9–11 places · 10–12 people · 2 model calls" — what you actually get. */
function scaleMeta(p) {
  const range = (r) => (Array.isArray(r) ? (r[0] === r[1] ? `${r[0]}` : `${r[0]}–${r[1]}`) : '');
  const bits = [];
  if (p.locations) bits.push(`${range(p.locations)} places`);
  if (p.characters) bits.push(`${range(p.characters)} people`);
  if (p.model_calls) {
    bits.push(`${p.model_calls} model call${p.model_calls === 1 ? '' : 's'}`);
  }
  return bits.join(' · ');
}

function shapeStep(f) {
  const plans = scalePlans || FALLBACK_SCALES;
  return `
    ${heading('How much world?')}
    ${note('Anything past a town is built in several passes and stitched — which '
      + 'is why it costs more and takes longer. You are told the number before '
      + 'you commit, not after.')}
    ${cardRow(plans.map((p) => card({
    attr: 'data-forge-scale', id: p.scale, title: p.label, blurb: p.blurb,
    meta: scaleMeta(p), selected: f.scale === p.scale,
  })))}
    ${note('Every world gets the same depth per place: people with a voice and a '
      + 'card, institutions that can actually detain you, characters who have '
      + 'opinions about each other, and nine laws the engine enforces before a '
      + 'word is written.', 'calm')}
    <div class="step-nav">
      <button class="btn btn-ghost" data-forge-next="premise">Back</button>
      <button class="btn btn-primary" data-forge-next="you">Next — you in it</button>
    </div>`;
}

/* ------------------------------------------------------------------- you */

function youStep(f) {
  const z = f.zero;
  if (!z) {
    return `${heading('Where you stand')}
      <div class="peek-busy">Working out what to ask…</div>`;
  }
  if (!(z.questions || []).length) {
    return `${heading('Where you stand')}
      ${note('Nothing to settle for this one — the world will place you.')}
      <div class="step-nav">
        <button class="btn btn-ghost" data-forge-next="shape">Back</button>
        <button class="btn btn-primary" data-forge-next="build">Next — build</button>
      </div>`;
  }
  return `
    ${heading('Where you stand', z.canon ? 'grounded in the real setting' : '')}
    ${note(z.crossover
    ? 'A crossover lives or dies on two answers: how they got here, and whether '
      + 'what they could do still works. Neither is in the source, so the world '
      + 'is built around whatever you say — and the people in it react to it.'
    : 'This is the question canon worlds live or die on. A protagonist with '
      + 'no defined place in the power system becomes either a god or a '
      + 'bystander, and both are boring.')}
    ${(z.questions || []).map((q) => `
      <div class="zero-q">
        <div class="zero-ask">${esc(q.q)}</div>
        ${q.why ? `<div class="zero-why">${esc(q.why)}</div>` : ''}
        ${q.kind === 'choice'
    ? cardRow((q.options || []).map((o) => card({
      attr: `data-zero-${q.id}`, id: o.id, title: o.label,
      blurb: o.blurb || '', selected: f.answers[q.id] === o.id,
    })))
    : `<input class="fld-input" data-zero-text="${esc(q.id)}"
             maxlength="160" value="${esc(f.answers[q.id] || '')}"
             placeholder="${esc(q.placeholder || '')}">`}
      </div>`).join('')}
    <div class="step-nav">
      <button class="btn btn-ghost" data-forge-next="shape">Back</button>
      <button class="btn btn-primary" data-forge-next="build">Next — build</button>
    </div>`;
}

/* ----------------------------------------------------------------- build */

function buildStep(f) {
  const plans = scalePlans || FALLBACK_SCALES;
  const scale = plans.find((s) => s.scale === f.scale) || plans[0];
  const parsed = (f.peek && f.peek.premise) || {};
  const answered = Object.keys(f.answers || {}).length;
  return `
    ${heading('What you are about to get')}
    <div class="summary">
      <div class="sum-row"><i>World</i><b>${esc(parsed.host || f.setting || '—')}</b></div>
      ${(parsed.imports || []).map((im) => `<div class="sum-row">
        <i>Carrying in</i><b>${esc(im.character)}</b>
        <span>from ${esc(im.from)}</span></div>`).join('')}
      <div class="sum-row"><i>Size</i><b>${esc(scale.label)}</b>
        <span>${esc(scaleMeta(scale))}</span></div>
      <div class="sum-row"><i>Sourcing</i><b>${esc(
    (BUILD_MODES.find((m) => m[0] === f.mode) || BUILD_MODES[0])[1])}</b></div>
      ${f.tone ? `<div class="sum-row"><i>Tone</i><b>${esc(f.tone)}</b></div>` : ''}
      ${answered ? `<div class="sum-row"><i>Your place</i>
        <b>${answered} settled</b></div>` : ''}
    </div>
    ${(parsed.host || (parsed.imports || []).length) ? note(
    'A world that continues somebody else’s setting is yours and private: '
    + 'you play it, edit it and export it, and it is never listed publicly.',
    'calm') : ''}
    <div class="step-nav">
      <button class="btn btn-ghost" data-forge-next="you">Back</button>
      <button class="btn btn-primary btn-lg" data-forge-build="1"
        ${f.building ? 'disabled' : ''}>
        ${f.building ? 'Building…' : 'Build the world'}</button>
    </div>`;
}

/* --------------------------------------------------------------- actions */

export function handle(target) {
  const f = get('forge');
  if (!f) return false;

  const step = target.closest('[data-forge-step]');
  if (step) { goto(step.dataset.forgeStep); return true; }

  const next = target.closest('[data-forge-next]');
  if (next) { goto(next.dataset.forgeNext); return true; }

  const sug = target.closest('[data-forge-suggest]');
  if (sug) {
    patch('forge', { setting: sug.dataset.forgeSuggest });
    render();
    peek();
    return true;
  }

  const mode = target.closest('[data-forge-mode]');
  if (mode) {
    patch('forge', { mode: mode.dataset.forgeMode, zero: null });
    render();
    peek();
    return true;
  }

  const scale = target.closest('[data-forge-scale]');
  if (scale) { patch('forge', { scale: scale.dataset.forgeScale }); render(); return true; }

  for (const el of [target, target.parentElement, target.closest('button')]) {
    if (!el || !el.dataset) continue;
    const key = Object.keys(el.dataset).find((k) => k.startsWith('zero') && k !== 'zeroText');
    if (key) {
      const qid = key.replace(/^zero/, '').replace(/^[A-Z]/, (c) => c.toLowerCase());
      patch('forge', { answers: { ...f.answers, [qid]: el.dataset[key] } });
      render();
      return true;
    }
  }

  if (target.closest('[data-forge-build]')) { build(); return true; }
  return false;
}

export function onInput(target) {
  const f = get('forge');
  if (!f) return;
  if (target.id === 'forgeSetting') {
    patch('forge', { setting: target.value, zero: null });
    // Reflect onto the nav rather than re-rendering: a full render on every
    // keystroke would rebuild the textarea and throw the caret to the end.
    syncNav();
    clearTimeout(peekTimer);
    peekTimer = setTimeout(peek, 420);
    return;
  }
  if (target.id === 'forgeTone') patch('forge', { tone: target.value });
  const textQ = target.dataset && target.dataset.zeroText;
  if (textQ) {
    patch('forge', { answers: { ...f.answers, [textQ]: target.value } });
  }
}

/**
 * Push state onto the controls that depend on it without a re-render. Only
 * the step nav needs this - everything else is redrawn on a real change.
 */
function syncNav() {
  const f = get('forge');
  const next = $('[data-forge-next="shape"]');
  if (next) next.disabled = !(f.setting || '').trim();
  const stepBtn = $('[data-forge-step="premise"]');
  if (stepBtn) stepBtn.classList.toggle('done', Boolean((f.setting || '').trim()));
}


function goto(step) {
  patch('forge', { step });
  render();
  const f = get('forge');
  if (step === 'you' && !f.zero) loadZero();
}

async function peek() {
  const f = get('forge');
  if (!f) return;
  const setting = (f.setting || '').trim();
  if (setting.length < 3 || f.mode === 'original') {
    patch('forge', { peek: null, peeking: false });
    return renderPeek();
  }
  patch('forge', { peeking: true });
  renderPeek();
  try {
    const r = await api(`/forge/research?setting=${encodeURIComponent(setting)}`);
    // The player may have kept typing while that was in flight.
    if ((get('forge').setting || '').trim() !== setting) return;
    patch('forge', { peek: r, peeking: false });
  } catch {
    patch('forge', { peek: null, peeking: false });
  }
  renderPeek();
}

function renderPeek() {
  const box = $('#forgePeek');
  if (box) box.innerHTML = peekView(get('forge'));
}

async function loadZero() {
  const f = get('forge');
  try {
    const z = await api(`/forge/session-zero?setting=${encodeURIComponent(f.setting)}`
      + `&mode=${encodeURIComponent(f.mode)}`);
    patch('forge', { zero: z });
  } catch {
    patch('forge', { zero: { canon: false, questions: [] } });
  }
  if (get('forge').step === 'you') render();
}

async function build() {
  const f = get('forge');
  patch('forge', { building: true });
  render();
  try {
    const world = await api('/forge/bootstrap', {
      method: 'POST',
      body: {
        // Required by the endpoint's own model. api() only puts it on the
        // query string, so leaving it out of the body failed every build
        // with a 422 that reported nothing.
        user_id: whoami(),
        setting: f.setting, tone: f.tone, mode: f.mode, scale: f.scale,
        answers: f.answers,
      },
    });
    if (onBuilt) onBuilt(world);
  } catch (e) {
    patch('forge', { building: false });
    render();
    throw e;
  }
}
