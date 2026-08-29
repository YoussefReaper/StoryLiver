/* StoryLiver — the table. No framework, no build step. */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const SVGNS = 'http://www.w3.org/2000/svg';

const S = {
  userId: null, ptId: null, sessionId: null, playerId: 'user', role: 'player',
  session: null, state: null, ws: null, wsRetry: 0,
  lastFeedId: 0, busy: false, whisperTo: null,
  economy: null, ethics: null, streak: null, worlds: [], forge: null,
  atlas: null, graph: null, knowledge: null, board: null, safety: null, budgetTable: null,
  combat: null, combatMove: null, combatTarget: null,
  view: 'story', prefs: { theme: 'ink', density: 'comfortable', power: false, atlasStyle: 'map' },
  selectedPlace: null,
};

/* ------------------------------------------------------------------ util */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const paras = (t) => String(t || '').split(/\n{2,}/).map((p) =>
  `<p>${esc(p.trim()).replace(/\n/g, '<br>')}</p>`).join('');
const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : s;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .4s'; }, 4200);
  setTimeout(() => el.remove(), 4700);
}

async function api(path, opts = {}) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(`/api${path}${sep}user_id=${encodeURIComponent(S.userId)}`, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

function userId() {
  let id = localStorage.getItem('storyliver.uid');
  if (!id) {
    id = 'p' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
    localStorage.setItem('storyliver.uid', id);
  }
  return id;
}

function applyPrefs() {
  document.documentElement.dataset.theme = S.prefs.theme;
  document.documentElement.dataset.density = S.prefs.density;
  localStorage.setItem('storyliver.prefs', JSON.stringify(S.prefs));
  api('/prefs', { method: 'POST', body: { user_id: S.userId, data: S.prefs } }).catch(() => {});
}

/* ================================================================ THRESHOLD */
async function initThreshold() {
  const [worlds, saves, econ] = await Promise.all([
    api('/worlds'), api('/playthroughs'), api('/economy'),
  ]);
  S.economy = econ; S.worlds = worlds; S.streak = saves.streak;

  const w = worlds.find((x) => x.kind === 'starter') || worlds[0];
  if (w) {
    $('#qsTitle').textContent = w.name;
    $('#qsTagline').textContent = w.tagline;
    $('#qsPremise').textContent = w.premise;
    $('#beginBtn').querySelector('span').textContent = `Step into ${w.name}`;
    $('#qsStats').innerHTML = [
      [`${w.npc_count}`, 'living minds'], [`${w.fated_events}`, 'fated events'],
      [`${w.rules}`, 'enforced rules'], ['0', 'subscriptions'],
    ].map(([n, l]) => `<span class="stat"><b>${n}</b> ${l}</span>`).join('');
  }
  const list = saves.playthroughs || [];
  $('#libraryBtn').hidden = list.length === 0;
  $('#libraryCount').textContent = list.length === 1
    ? '1 story in progress' : `${list.length} stories in progress`;
  const saved = localStorage.getItem('storyliver.name');
  if (saved) $('#joinName').value = saved;
}

/* ==================================================================== APP */
async function openPlaythrough(id, { sessionId = null, playerId = 'user', role = 'player' } = {}) {
  // Fetch BEFORE committing any state. A remembered story can belong to
  // someone else (cleared storage, a shared browser, a different device), and
  // half-opening it leaves S.ptId pointing at a story this player cannot act
  // on - every later call would then fail in a much more confusing place.
  let feed, ws;
  try {
    [{ feed }, ws] = await Promise.all([
      api(`/playthroughs/${id}?player=${encodeURIComponent(playerId)}`),
      api(`/playthroughs/${id}/workspace?player=${encodeURIComponent(playerId)}`),
    ]);
  } catch (e) {
    localStorage.removeItem('storyliver.last');
    S.ptId = null; S.sessionId = null;
    throw e;
  }

  S.ptId = id; S.sessionId = sessionId; S.playerId = playerId; S.role = role;
  localStorage.setItem('storyliver.last', JSON.stringify({ id, sessionId, playerId, role }));
  absorb(ws);
  S.lastFeedId = 0; lastRenderedTurn = -1;
  $('#feed').innerHTML = '';
  renderFeed(feed);
  renderAll();
  $('#threshold').hidden = true;
  $('#app').hidden = false;
  refreshStreak();
  if (sessionId) connectWS();
  $('#actionInput').focus();

  // A first-time player meets the three decisions that shape the world before
  // they meet a blank input box. Returning players never see this again.
  await refreshAwareness();
  if (needsExplainer()) {
    // What the product IS comes before what this world is - a player who does
    // not know the world is awake cannot make sense of the choices that follow.
    showExplainer(false);
  } else if (needsOnboarding() && S.state?.turn === 0) {
    startOnboarding();
  } else if (S.state?.ended) {
    showEnding({ reason: 'victory', run: { carried: S.state.run || {} } });
  }
}

async function refreshAwareness() {
  await Promise.all([loadAwareness(), loadAuthority()]);
  renderAwareness();
}

function absorb(ws) {
  if (!ws) return;
  if (ws.state) S.state = ws.state;
  if (ws.atlas) S.atlas = ws.atlas;
  if (ws.graph) S.graph = ws.graph;
  if (ws.knowledge) S.knowledge = ws.knowledge;
  if (ws.board) S.board = ws.board;
  if (ws.safety) S.safety = ws.safety;
  if (ws.budget) S.budgetTable = ws.budget;
}

async function refreshWorkspace() {
  try {
    absorb(await api(`/playthroughs/${S.ptId}/workspace?player=${encodeURIComponent(S.playerId)}`));
    renderAll();
  } catch (e) { /* the feed still works */ }
}

async function refreshStreak() {
  try {
    S.streak = await api('/streak');
    $('#streakBtn').hidden = S.streak.current <= 0;
    $('#streakValue').textContent = S.streak.current;
  } catch { /* decoration */ }
}

function renderAll() {
  renderTopbar();
  renderYouCard();
  renderParty();
  renderFate();
  renderSouls();
  renderQuickRow();
  if (S.view === 'atlas') renderAtlas();
  if (S.view === 'graph') renderGraph();
  if (S.view === 'known') renderKnown();
  if (S.view === 'combat') renderCombat();
  const known = (S.knowledge?.facts || []).length;
  $('#knownBadge').textContent = known ? String(known) : '';
  $('#atlasBadge').textContent = S.atlas ? `${S.atlas.discovered}/${S.atlas.total}` : '';
  $('#combatTab').hidden = !S.state?.combat_id;
  renderPremiumState();
  renderAwareness();
  renderAccountChip();
}

function renderAccountChip() {
  const el = $('#accountChip');
  if (!el) return;
  el.innerHTML = S.account
    ? `<span class="acct-name">${esc(S.account.display_name || 'You')}</span>`
    : '<span class="acct-guest">Guest</span>';
  el.title = S.account ? 'Your profile' : 'Playing as a guest — sign in to keep Mana and reputation';
}

function renderPremiumState() {
  const box = $('#premiumToggle'); const wrap = $('#premiumWrap');
  if (!box || !wrap) return;
  if (S.sessionId) {
    // Reflect the ROOM's setting, and make a guest's toggle visibly not-theirs
    // rather than a control that silently does nothing when they click it.
    box.checked = !!S.state?.session?.premium_allowed;
    const guest = S.role !== 'host';
    wrap.classList.toggle('locked', guest);
    wrap.title = guest
      ? 'The host decides Deep Prose for the whole room'
      : 'Deep Prose for the whole room — 4 Mana a turn, from your wallet';
  } else {
    wrap.classList.remove('locked');
    wrap.title = 'The stronger narrator writes the passage — 4 Mana';
  }
}

/* ------------------------------------------------------------- top bar */
const SKY = { clear: '☀', cloud: '☁', rain: '🌧', storm: '⛈', fog: '🌫', wind: '🍃', snow: '❄', ashfall: '🌑' };

function renderTopbar() {
  const st = S.state; if (!st) return;
  const w = st.weather || {};
  const clock = $('#worldClock');
  $('#wcSky').textContent = SKY[w.weather] || '·';
  $('#wcTime').textContent = `Day ${st.day} · ${st.phase}${w.weather_word ? ` · ${w.weather_word}` : ''}`;
  $('#wcHere').textContent = st.location_name;
  clock.classList.toggle('is-night', st.phase === 'night');
  clock.classList.toggle('is-storm', ['storm', 'snow', 'ashfall'].includes(w.weather));
  clock.title = `Turn ${st.turn} · ${w.temperature ?? '?'}°C · light ${w.here?.light ?? w.light}/5`
    + ` · noise ${w.here?.noise ?? w.noise}/5`;

  const m = st.mana;
  const btn = $('#manaBtn');
  $('#manaValue').textContent = m.mode === 'ember' ? 'Ember' : `${m.balance + m.free_left}`;
  btn.classList.toggle('is-ember', m.mode === 'ember');
  btn.title = m.mode === 'ember' ? 'Out of Mana — playing in Ember. Nothing is locked.'
    : `${m.balance} Mana + ${m.free_left} free today${m.payers > 1 ? ` (${m.payers} payers)` : ''}`;

  const chip = $('#roomChip');
  if (st.session) {
    chip.hidden = false;
    $('#roomCode').textContent = st.session.code;
    $('#roomMode').textContent = st.session.mode === 'chaos' ? 'chaos' : 'co-op';
    S.sessionId = st.session.id;
  } else chip.hidden = true;

  const t = Math.round(st.tension * 100);
  $('#tensionFill').style.width = `${t}%`;
  $('#tensionNum').textContent = `${t}%`;
  $('#tensionHint').textContent = t < 30 ? 'Slack. The Director is looking for a way in.'
    : t < 65 ? 'Something is winding up.' : 'Wound tight. Expect the world to move first.';

  const watching = S.role === 'spectator';
  $('#sendLabel').textContent = watching ? 'Watching' : 'Live it';
  $('#sendBtn').disabled = watching;
  $('#actionInput').disabled = watching;
}

/* ------------------------------------------------------------ your card */
function renderYouCard() {
  const st = S.state; if (!st) return;
  const me = (S.session?.players || []).find((p) => p.player_id === S.playerId);
  const name = me?.name || 'You';
  $('#youName').textContent = name;
  $('#youSigil').textContent = (name[0] || '?').toUpperCase();
  $('#youConcept').textContent = st.protagonist || '';

  const rep = (S.knowledge?.factions || [])[0];
  const m = st.mana;
  const vitals = [
    ['Mana', 'mana', clamp((m.balance + m.free_left) / 200 * 100, 2, 100),
      m.mode === 'ember' ? 'Ember' : `${m.balance + m.free_left}`],
  ];
  if (rep) {
    vitals.push(['Standing', 'standing', clamp((rep.standing + 100) / 2, 2, 100),
      `${Math.round(rep.standing)}`]);
  }
  const c = S.combat?.board?.find((b) => b.mine);
  if (c) vitals.unshift(['Health', 'hp', clamp(c.hp / c.max_hp * 100, 0, 100), `${c.hp}/${c.max_hp}`]);
  $('#vitals').innerHTML = vitals.map(([label, key, pct, val]) => `
    <div class="vital v-${key}">
      <span class="vital-label">${label}</span>
      <span class="vital-track"><i class="vital-fill" style="width:${pct}%"></i></span>
      <span class="vital-val">${esc(val)}</span>
    </div>`).join('');

  const known = st.npcs.filter((n) => n.alive && n.disposition !== 'stranger')
    .sort((a, b) => (b.loyalty + b.affinity) - (a.loyalty + a.affinity)).slice(0, 8);
  $('#pips').innerHTML = known.length ? known.map((n) => `
    <button class="pip pip-${esc((n.disposition || 'stranger').split(' ')[0])}" data-soul="${n.id}"
      title="${esc(n.name)} — ${esc(n.disposition)}${n.will_cover ? ' · would cover for you' : ''}">
      <i></i>${esc(n.name.split(' ')[0])}</button>`).join('')
    : '<span class="rail-note" style="margin:0">Nobody has an opinion of you yet.</span>';
}

function renderParty() {
  const card = $('#partyCard');
  if (!S.session) { card.hidden = true; return; }
  card.hidden = false;
  const online = new Set(S.session.present || []);
  const list = S.session.players || [];
  const away = new Set(((S.board?.entries) || []).map((e) => e.player_id));
  $('#partyCount').textContent = `${online.size}/${list.length} here`;
  $('#party').innerHTML = list.map((p) => `
    <div class="party-row ${online.has(p.player_id) ? 'online' : ''} ${p.player_id === S.playerId ? 'me' : ''}">
      <i class="pdot"></i>
      <span class="pname">${esc(p.name)}${p.player_id === S.playerId ? ' (you)' : ''}
        ${away.has(p.player_id) ? '<span class="pgoal"> — away</span>' :
          (p.goal ? `<span class="pgoal"> — ${esc(p.goal)}</span>` : '')}</span>
      <span class="prole">${p.is_host ? 'host' : p.role}</span>
      ${p.player_id !== S.playerId && p.role !== 'spectator'
        ? `<button class="whisper-to" data-whisper-player="${p.player_id}" data-name="${esc(p.name)}" title="Whisper">&#9998;</button>` : ''}
    </div>`).join('');
  $('#partyNote').textContent = S.session.mode === 'chaos'
    ? 'Chaos: contested moves go to the World Master — state first, then a roll.'
    : 'Co-op: one world, one timeline. Every character remembers each of you separately.';
}

function renderFate() {
  const st = S.state; if (!st) return;
  $('#fateList').innerHTML = st.fate.map((f) => `
    <li class="fate-item ${f.status}">
      <span class="ft">T${f.turn}${f.status === 'next' ? ' · next' : ''}</span>
      <span class="fx">${esc(f.title)}</span>
    </li>`).join('');
}

/* --------------------------------------------------------------- souls */
function bar(label, key, value) {
  const v = clamp(value || 0, -100, 100);
  const left = v >= 0 ? 50 : 50 + v / 2;
  return `<div class="bar b-${key}">
    <span class="bar-label">${label}</span>
    <span class="bar-track"><i class="bar-mid"></i><i class="bar-fill" style="left:${left}%;width:${Math.abs(v) / 2}%"></i></span>
    <span class="bar-val">${v > 0 ? '+' : ''}${Math.round(v)}</span></div>`;
}

function renderSouls() {
  const st = S.state; if (!st) return;
  const present = st.npcs.filter((n) => n.present && n.alive);
  $('#presentCount').textContent = `${present.length} here`;
  const order = [...st.npcs].sort((a, b) => (b.present - a.present) || (b.alive - a.alive) ||
    (Math.abs(b.affinity) + Math.abs(b.trust) + b.fear - Math.abs(a.affinity) - Math.abs(a.trust) - a.fear));
  const power = S.prefs.power;
  $('#souls').innerHTML = order.map((n) => `
    <button class="soul ${n.present ? 'present' : ''} ${n.alive ? '' : 'dead'}" data-soul="${n.id}">
      <div class="soul-top">
        ${n.alive ? '<i class="live-dot"></i>' : '<span class="soul-where">&dagger;</span>'}
        <span class="soul-name">${esc(n.name)}</span>
        <span class="soul-where">${n.alive ? esc(n.location_name) : 'gone'}</span>
      </div>
      <div class="soul-role">${esc(n.disposition || n.role)}${n.will_cover ? ' · would cover for you' : ''}</div>
      <div class="bars">
        ${bar('Affinity', 'affinity', n.affinity)}${bar('Trust', 'trust', n.trust)}
        ${bar('Fear', 'fear', n.fear)}${bar('Duty', 'obligation', n.obligation)}
        ${power ? bar('Loyalty', 'loyalty', n.loyalty) + bar('Love', 'love', n.love) + bar('Respect', 'respect', n.respect) : ''}
      </div>
      ${n.latest_reflection ? `<div class="soul-think">${esc(n.latest_reflection)}</div>` : ''}
    </button>`).join('');
}

/* ==================================================================== ATLAS */
const PLACE_GLYPH = { tavern: '🍺', civic: '⚑', work: '⚒', sacred: '✦', open: '◇',
                      threshold: '⇥', place: '·', unknown: '?' };

function renderAtlas() {
  const a = S.atlas; const svg = $('#atlasMap');
  if (!a) { svg.innerHTML = ''; return; }
  const W = 1000, H = 1000, PAD = 90;
  const px = (n) => PAD + n.x * (W - PAD * 2);
  const py = (n) => PAD + n.y * (H - PAD * 2);
  const byId = Object.fromEntries(a.nodes.map((n) => [n.id, n]));

  const parts = [];
  for (const e of a.edges) {
    const A = byId[e.a], B = byId[e.b];
    if (!A || !B) continue;
    if (A.status === 'hidden' && B.status === 'hidden') continue;
    parts.push(`<line class="n-link ${e.known ? 'known' : 'unknown'}"
      x1="${px(A)}" y1="${py(A)}" x2="${px(B)}" y2="${py(B)}"/>`);
  }
  for (const t of a.travellers || []) {
    const A = byId[t.from], B = byId[t.to];
    if (!A || !B) continue;
    const k = clamp(t.progress ?? 0.5, 0, 1);
    const x = px(A) + (px(B) - px(A)) * k, y = py(A) + (py(B) - py(A)) * k;
    parts.push(`<circle class="n-marker-ring" cx="${x}" cy="${y}" r="17"/>
      <circle class="n-marker" cx="${x}" cy="${y}" r="6"/>
      <text x="${x}" y="${y - 24}" style="font-size:13px;fill:var(--crimson)">${esc(t.label || 'hunter')}</text>`);
  }
  for (const n of a.nodes) {
    const cls = n.here ? 'n-here' : n.status === 'ruined' || n.condition === 'ruined' ? 'n-ruined'
      : n.status === 'discovered' ? 'n-disc' : n.status === 'rumoured' ? 'n-rumoured' : 'n-hidden';
    const r = n.here ? 26 : n.status === 'hidden' ? 15 : 21;
    const glyph = PLACE_GLYPH[n.kind] || '·';
    parts.push(`<g class="n-node ${cls}" data-place="${esc(n.id)}" tabindex="0">
      ${n.here ? `<circle class="n-pulse" cx="${px(n)}" cy="${py(n)}" r="26"/>` : ''}
      <circle class="body" cx="${px(n)}" cy="${py(n)}" r="${r}"/>
      <text class="glyph" x="${px(n)}" y="${py(n) + 5}">${n.status === 'hidden' ? '?' : glyph}</text>
      <text x="${px(n)}" y="${py(n) + r + 20}">${esc(n.name)}</text>
    </g>`);
  }
  svg.innerHTML = parts.join('');
  if (!S.selectedPlace) S.selectedPlace = a.here;
  renderAtlasDetail();
  $('#echoList').innerHTML = (a.echoes || []).length
    ? `<h4>What has changed</h4>` + a.echoes.slice(-14).reverse().map((e) =>
        `<div class="echo k-${esc(e.kind)}"><b>turn ${e.turn}</b>${esc(e.text)}</div>`).join('')
    : '';
}

function renderAtlasDetail() {
  const a = S.atlas; if (!a) return;
  const n = a.nodes.find((x) => x.id === S.selectedPlace) || a.nodes.find((x) => x.here);
  const box = $('#atlasDetail');
  if (!n) { box.innerHTML = ''; return; }
  if (n.status === 'hidden') {
    box.innerHTML = `<h3>Somewhere you have not been</h3>
      <div class="ad-status">unknown</div>
      <p>You have not heard this place named. It is on the map because the road goes somewhere.</p>`;
    return;
  }
  const reachable = (S.state?.exits || []).some((e) => e.id === n.id);
  box.innerHTML = `
    <h3>${esc(n.name)}</h3>
    <div class="ad-status">${n.here ? 'you are here' : n.status}${n.condition !== 'intact' ? ` · ${esc(n.condition)}` : ''}</div>
    ${n.desc ? `<p>${esc(n.desc)}</p>` : '<p>Heard of, never seen.</p>'}
    <div class="ad-meta">
      ${n.visits ? `<span>${n.visits} visit${n.visits > 1 ? 's' : ''}</span>` : ''}
      ${n.discovered_turn >= 0 ? `<span>found T${n.discovered_turn}</span>` : ''}
      <span>${esc(n.kind)}</span>
    </div>
    ${n.note ? `<p style="margin-top:8px;font-style:italic">${esc(n.note)}</p>` : ''}
    ${reachable && !n.here ? `<button class="btn btn-ghost" data-travel="${esc(n.name)}">Go there</button>` : ''}`;
}

/* ==================================================================== GRAPH */
function renderGraph() {
  const g = S.graph; const svg = $('#graphMap');
  if (!g) { svg.innerHTML = ''; return; }
  const NW = 148, NH = 40, GAPX = 176, LANEY = 66;
  const nodes = g.nodes.slice(-26);
  const startSeq = nodes.length ? nodes[0].seq : 0;
  const pos = {};
  nodes.forEach((n) => {
    pos[n.id] = { x: 40 + (n.seq - startSeq) * GAPX, y: 190 + n.lane * LANEY };
  });
  const futureBase = 40 + (nodes.length ? (nodes[nodes.length - 1].seq - startSeq + 1) : 0) * GAPX;
  const width = futureBase + (g.futures.length ? g.futures.length * GAPX : 0) + NW + 60;
  svg.setAttribute('viewBox', `0 0 ${Math.max(width, 900)} 400`);
  svg.style.minWidth = `${Math.max(width, 900)}px`;

  const parts = [];
  for (const e of g.edges) {
    const A = pos[e.src], B = pos[e.dst];
    if (!A || !B) continue;
    const mid = (A.x + NW + B.x) / 2;
    parts.push(`<path class="g-edge" d="M${A.x + NW} ${A.y + NH / 2} C${mid} ${A.y + NH / 2}, ${mid} ${B.y + NH / 2}, ${B.x} ${B.y + NH / 2}"/>`);
  }
  const last = nodes[nodes.length - 1];
  g.futures.forEach((f, i) => {
    const x = futureBase + i * GAPX, y = 190 + (f.lane || 0) * LANEY;
    pos[f.id] = { x, y };
    if (last) {
      const A = pos[last.id];
      const mid = (A.x + NW + x) / 2;
      parts.push(`<path class="g-edge future" d="M${A.x + NW} ${A.y + NH / 2} C${mid} ${A.y + NH / 2}, ${mid} ${y + NH / 2}, ${x} ${y + NH / 2}"/>`);
    }
  });
  const label = (t, n) => esc(t.length > n ? t.slice(0, n - 1) + '…' : t);
  for (const n of nodes) {
    const p = pos[n.id];
    parts.push(`<g class="g-node tone-${esc(n.tone)} ${n.id === g.here ? 'here' : ''}"
        data-gnode="${n.id}" transform="translate(${p.x},${p.y})" tabindex="0">
      <rect width="${NW}" height="${NH}"/>
      <text x="11" y="16" style="font-size:9.5px;fill:var(--faint);letter-spacing:.1em">T${n.turn} ${esc(n.kind).toUpperCase()}</text>
      <text x="11" y="31">${label(n.label, 22)}</text></g>`);
  }
  for (const f of g.futures) {
    const p = pos[f.id];
    parts.push(`<g class="g-node future tone-fate" data-gfuture="${esc(f.id)}"
        transform="translate(${p.x},${p.y})" tabindex="0">
      <rect width="${NW}" height="${NH}"/>
      <text x="11" y="16" style="font-size:9.5px;fill:var(--ember-deep);letter-spacing:.1em">T${f.turn} SEALED</text>
      <text x="11" y="31">${label(f.label, 22)}</text></g>`);
  }
  svg.innerHTML = parts.join('');
  $('#graphDetail').innerHTML = '';
}

/* ==================================================================== KNOWN */
function renderKnown() {
  const k = S.knowledge, b = S.board;
  const cols = [];

  cols.push(`<div class="known-col">
    <h3>What you know <span class="pill">${(k?.facts || []).length}</span></h3>
    ${(k?.facts || []).length ? k.facts.map((f) => `
      <div class="fact src-${esc(f.source)}">
        <div class="fact-top"><span>${esc(f.source)}</span><span>T${f.turn}</span>
          ${f.place ? `<span>${esc(f.place)}</span>` : ''}
          <span class="fact-conf">${Math.round(f.confidence * 100)}% sure</span></div>
        <div class="fact-body">${esc(f.summary)}</div>
      </div>`).join('')
      : '<div class="empty">Nothing yet. You only know what you saw or were told.</div>'}
  </div>`);

  cols.push(`<div class="known-col">
    <h3>Who has an opinion</h3>
    ${(k?.factions || []).length ? k.factions.map((f) => {
      const v = clamp(f.standing, -100, 100);
      const left = v >= 0 ? 50 : 50 + v / 2;
      const colour = v < -20 ? 'var(--crimson)' : v > 20 ? 'var(--verdigris)' : 'var(--muted)';
      return `<div class="rep-row"><span>${esc(f.name)}</span>
        <span class="rep-track"><i class="rep-mid"></i>
          <i class="rep-fill" style="left:${left}%;width:${Math.abs(v) / 2}%;background:${colour}"></i></span>
        <span class="rep-val">${v > 0 ? '+' : ''}${Math.round(v)}</span></div>`;
    }).join('') : '<div class="empty">No faction has heard anything about you.</div>'}
    <p class="fineprint" style="margin-top:10px">Reputation only moves for people who actually know.
    A faction that heard nothing feels nothing.</p>
  </div>`);

  cols.push(`<div class="known-col">
    <h3>Who is coming <span class="pill">${(k?.hunts || []).length}</span></h3>
    ${(k?.hunts || []).length ? k.hunts.map((h) => {
      const span = Math.max(1, h.eta_max - h.eta_min);
      const left = clamp((h.turns_out / 12) * 100, 0, 88);
      const wide = clamp((span / 12) * 100, 6, 40);
      return `<div class="hunt">
        <div class="hunt-top"><span class="hunt-who">${esc(h.hunter)}</span>
          <span class="hunt-sev">${esc(h.severity)}</span></div>
        <div class="hunt-why">${esc(h.faction)} — ${esc(h.reason)}</div>
        <div class="hunt-eta"><span>${esc(h.from)} → ${esc(h.to)}</span>
          <span class="eta-track"><i class="eta-window" style="left:${left}%;width:${wide}%"></i></span>
          <span>${esc(h.window)}</span></div></div>`;
    }).join('') : '<div class="empty">Nobody is looking for you. That you know of.</div>'}
  </div>`);

  if (b?.active) {
    cols.push(`<div class="known-col"><h3>The party is split</h3>
      <div class="conspiracy">
        ${b.entries.map((e) => `<div class="consp-row">
          <span class="cr-name">${esc(e.name)}</span>
          <span><span class="cr-said">“${esc(e.said)}”</span>
          ${e.mine && e.truth && e.deceptive ? `<div class="cr-truth">Really: ${esc(e.truth)}</div>` : ''}</span>
          <span class="cr-turns">${e.returns_in > 0 ? `${e.returns_in} turns out` : 'due back'}</span>
        </div>`).join('')}
      </div>
      ${(b.private_log || []).length ? `<h3 style="margin-top:16px">Yours alone</h3>
        ${b.private_log.map((p) => `<div class="fact src-private">
          <div class="fact-top"><span>private</span><span>T${p.turn}</span></div>
          <div class="fact-body">${esc(p.summary)}</div></div>`).join('')}` : ''}
      <p class="fineprint">${esc(b.note || '')}</p></div>`);
  }
  $('#knownCols').innerHTML = cols.join('');
}

/* =================================================================== COMBAT */
const ZONE_LABEL = { back_a: 'your rear', front_a: 'your line', centre: 'the middle',
                     front_b: 'their line', back_b: 'their rear' };

async function loadCombat() {
  if (!S.state?.combat_id) { S.combat = null; return; }
  try {
    S.combat = await api(`/combat/${S.state.combat_id}?player=${encodeURIComponent(S.playerId)}`);
  } catch { S.combat = null; }
}

function renderCombat() {
  const c = S.combat; const box = $('#combatWrap');
  if (!c) {
    box.innerHTML = `<div class="empty">No fight in progress.
      <div style="margin-top:14px"><button class="btn btn-ghost" data-start-combat>Start a fight here</button></div></div>`;
    return;
  }
  const zones = ['back_a', 'front_a', 'centre', 'front_b', 'back_b'];
  const grid = zones.map((z) => `
    <div class="zone ${z === 'centre' ? 'centre' : ''}" data-zone="${z}">
      <div class="zone-label">${ZONE_LABEL[z]}</div>
      ${c.board.filter((b) => b.zone === z).map((b) => `
        <div class="fighter ${b.mine ? 'mine' : ''} ${b.ally ? 'ally' : 'foe'} ${b.down ? 'down' : ''}
             ${S.combatTarget === b.id ? 'targeted' : ''}" data-fighter="${esc(b.id)}">
          <div class="f-name"><b>${esc(b.name)}</b>
            ${b.advantage ? `<span class="f-adv ${b.advantage > 0 ? 'up' : 'down'}">${b.advantage > 0 ? '+' : ''}${b.advantage}</span>` : ''}</div>
          <div class="f-hp"><i style="width:${clamp(b.hp / b.max_hp * 100, 0, 100)}%"></i></div>
          ${(b.status || []).length ? `<div class="f-status">${b.status.map((s) => `<span>${esc(s)}</span>`).join('')}</div>` : ''}
        </div>`).join('')}
    </div>`).join('');

  const moves = Object.entries(c.moves).map(([k, m]) => `
    <button class="move-btn ${S.combatMove === k ? 'on' : ''}" data-move="${esc(k)}">
      <b>${cap(k)}</b><span>${esc(m.desc)}</span>${m.mana ? `<em>${m.mana} Mana</em>` : ''}</button>`).join('');

  box.innerHTML = `
    <div class="combat-head">
      <h3>${esc(S.state.location_name)}</h3>
      <span class="combat-round">round ${c.round + 1} · ${c.status}</span>
      ${c.boss ? `<div class="boss-bar"><div class="boss-name">${esc(c.boss.name)}</div>
        <div class="boss-phase">${esc(c.boss.phase?.name || '')}${c.boss.phase?.only_hurt_by
          ? ` — untouchable until ${esc(c.boss.phase.only_hurt_by.replace(/_/g, ' '))}` : ''}</div></div>` : ''}
    </div>
    <div class="zones">${grid}</div>
    <div>
      <h3 style="font-size:10.5px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:9px">
        Declare — nobody sees anyone's order until everyone has one</h3>
      <div class="moves">${moves}</div>
    </div>
    <div class="combat-actions">
      <button class="btn btn-primary" data-combat-submit ${c.you_declared ? 'disabled' : ''}>
        ${c.you_declared ? 'Declared — waiting' : 'Lock it in'}</button>
      <button class="btn btn-ghost" data-combat-resolve>Resolve the round</button>
      <span class="declared-list">${c.declared.length} declared${S.combatTarget
        ? ` · target ${esc(c.board.find((b) => b.id === S.combatTarget)?.name || '')}` : ''}</span>
    </div>
    ${(c.log || []).length ? `<div class="combat-log">${c.log.slice(-3).map((r) => `
      <div class="cl-round">round ${r.round + 1}</div>
      ${r.steps.map((s) => `<div class="cl-step ${esc(s.kind)}">${esc(stepText(s))}</div>`).join('')}
    `).join('')}</div>` : ''}`;
}

const isYou = (who) => String(who || '').toLowerCase() === 'you';
const vb = (who, third, second) => (isYou(who) ? second : third);

function stepText(s) {
  const w = s.who;
  if (s.kind === 'hit') return `${w} ${vb(w, `${s.move}s`, s.move)} ${s.target} for ${s.damage}`
    + (s.flanking ? ' with the angle' : s.flanked ? ' while flanked' : '') + (s.note ? ` — ${s.note}` : '') + '.';
  if (s.kind === 'blocked') return `${w} ${vb(w, 'comes', 'come')} at ${s.target} and `
    + `${vb(w, 'gets', 'get')} nothing through${s.note ? ` — ${s.note}` : ''}.`;
  if (s.kind === 'move') return `${w} ${vb(w, 'shifts', 'shift')} to ${ZONE_LABEL[s.to] || s.to}.`;
  if (s.kind === 'down') return `${w} ${vb(w, 'goes', 'go')} down.`;
  if (s.kind === 'aid') return `${w} ${vb(w, 'steadies', 'steady')} ${s.target}.`;
  if (s.kind === 'feint') return `${w} ${vb(w, 'draws', 'draw')} ${s.target} out of position.`;
  return '';
}

/* ------------------------------------------------------------- the feed */
let lastRenderedTurn = -1;

function renderFeed(entries) {
  const feed = $('#feed');
  for (const e of entries) {
    if (e.id && e.id <= S.lastFeedId) continue;
    if (e.id) S.lastFeedId = e.id;
    const meta = typeof e.meta === 'string' ? JSON.parse(e.meta || '{}') : (e.meta || {});
    if (e.turn > lastRenderedTurn && e.turn > 0 && e.kind === 'you') {
      feed.insertAdjacentHTML('beforeend', `<div class="turn-rule"><span>Turn ${e.turn}</span></div>`);
      lastRenderedTurn = e.turn;
    }
    feed.insertAdjacentHTML('beforeend', entryHTML(e, meta));
  }
  requestAnimationFrame(() => { feed.scrollTop = feed.scrollHeight; });
}

function entryHTML(e, meta) {
  switch (e.kind) {
    case 'opening':
      return `<article class="entry entry-opening"><div class="prose">${paras(e.text)}</div></article>`;
    case 'you': {
      const mine = !S.sessionId || e.actor === S.playerId;
      return `<div class="entry entry-you"><div class="you-line">
        <b>${esc(mine ? 'You' : (meta.name || 'A player'))}</b>${esc(e.text)}</div></div>`;
    }
    case 'safety':
      return `<article class="entry entry-safety"><div class="safety-slab">${esc(e.text)}</div></article>`;
    case 'refusal':
      return `<article class="entry entry-refusal"><div class="refusal-wrap">
        <div class="refusal-head"><svg viewBox="0 0 16 16" class="ico"><circle cx="8" cy="8" r="6"/><path d="M4.2 11.8 11.8 4.2"/></svg>
          The world refuses</div>
        <div class="prose">${paras(e.text)}</div>
        ${meta.reason && !e.text.includes(meta.reason) ? `<div class="refusal-reason">${esc(meta.reason)}</div>` : ''}
        ${meta.rule_ref ? `<span class="rule-chip">rule ${esc(meta.rule_ref)}${meta.checked_by === 'rules' ? ' &middot; enforced in code' : ''}</span>` : ''}
      </div></article>`;
    case 'fate':
      return `<article class="entry entry-fate"><div class="fate-slab">
        <div class="fate-kicker">Fate <span class="ar" dir="rtl" lang="ar">القدر</span></div>
        <div class="fate-title">${esc(meta.title || 'It happens')}</div>
        <div class="fate-body">${paras(e.text)}</div>
        <div class="fate-seal">Written before you arrived. Nothing could have stopped it.</div></div></article>`;
    case 'contest':
      return `<article class="entry entry-contest"><div class="contest-slab">
        <div class="contest-head"><svg viewBox="0 0 16 16" class="ico"><path d="M3 13 13 3M6 3H3v3M10 13h3v-3"/></svg>Contested</div>
        <div class="contest-vs"><b>${esc(meta.winner_name || '')}</b> over <s>${esc(meta.loser_name || '')}</s></div>
        <div class="fate-body">${paras(e.text)}</div>
        ${meta.loser_cost ? `<div class="contest-why">${esc(meta.loser_cost)}</div>` : ''}
        ${meta.rolls ? `<div class="rolls">${Object.entries(meta.rolls).map(([k, v]) =>
          `<span class="roll">${esc(playerName(k))} d20 &middot; ${v}</span>`).join('')}</div>` : ''}
      </div></article>`;
    case 'combat':
      return `<article class="entry entry-combat"><div class="combat-slab">
        <div class="byline"><i></i>Round ${(meta.round ?? 0) + 1}</div>
        <div class="prose" style="font-size:15px">${paras(e.text)}</div>
        ${meta.status === 'over' ? `<div class="contest-why">The fight is over.</div>` : ''}</div></article>`;
    case 'reveal':
      return `<article class="entry entry-reveal"><div class="reveal-slab">
        <div class="reveal-head">The reveal — all at once</div>
        <div class="reveal-line">${esc(playerName(e.actor))} said <s>“${esc(meta.announced || '')}”</s>
          and actually: <b>${esc(meta.actual || e.text)}</b></div>
        ${(meta.caught_by || []).length ? `<div class="reveal-caught">Seen through by
          ${esc(meta.caught_by.map((c) => c.name).join(', '))}.</div>`
          : meta.deceptive ? `<div class="reveal-caught" style="color:var(--ok)">The story held.</div>` : ''}
      </div></article>`;
    case 'whisper': {
      const w = meta.payload || {};
      return `<article class="entry entry-whisper"><div class="whisper-slab">
        <div class="whisper-head"><svg viewBox="0 0 16 16" class="ico"><path d="M8 1.6a5.4 5.4 0 0 0-3.4 9.6V14l2.2-1.2a5.4 5.4 0 1 0 1.2-11.2z"/></svg>
          ${esc(meta.label || 'Whispered')} &mdash; private</div>
        <div class="whisper-said">&ldquo;${esc(w.text || '')}&rdquo;</div>
        ${w.reply ? `<div class="whisper-reply">${esc(w.reply)}</div>` : ''}</div></article>`;
    }
    default: {
      const tags = [];
      if (meta.npc_initiated) tags.push(`<span class="tag tag-npc">
        <svg viewBox="0 0 16 16" class="ico"><circle cx="8" cy="5" r="2.4"/><path d="M3 13.5c.5-2.6 2.4-3.9 5-3.9s4.5 1.3 5 3.9"/></svg>
        ${esc(meta.npc_initiated.name)} acted on their own</span>`);
      if (meta.director_beat) tags.push(`<span class="tag tag-beat">
        <svg viewBox="0 0 16 16" class="ico"><path d="M8 1.6 9.9 5.7l4.5.5-3.4 3 1 4.4L8 11.4l-4 2.2 1-4.4-3.4-3 4.5-.5z"/></svg>
        The Director turned the story <em>&middot; ${esc(meta.director_beat.kind)}</em></span>`);
      if (meta.discovered) tags.push(`<span class="tag tag-world">Found ${esc(meta.discovered.name)}</span>`);
      if (meta.unseen) tags.push(`<span class="tag tag-unseen">Nobody saw that</span>`);
      for (const r of (meta.reputation || [])) {
        tags.push(`<span class="tag tag-rep">${esc(r.name)} ${r.delta > 0 ? '+' : ''}${Math.round(r.delta)}</span>`);
      }
      for (const h of (meta.hunts_ordered || [])) {
        tags.push(`<span class="tag tag-rep">${esc(h.faction_name)} sent someone</span>`);
      }
      if (meta.premium) tags.push('<span class="tag tag-premium">Deep prose</span>');
      else if (meta.mode === 'ember') tags.push('<span class="tag tag-ember">Ember</span>');

      const rel = [...(meta.relationship_events || []).map(relEventChip),
                   ...(meta.relationship_changes || []).map(deltaChip)];
      const byline = (S.sessionId && meta.actor_name)
        ? `<div class="byline"><i></i>${esc(meta.actor_name)}&rsquo;s turn</div>` : '';
      return `<article class="entry entry-narration">${byline}
        ${tags.length ? `<div class="tags">${tags.join('')}</div>` : ''}
        <div class="prose">${paras(e.text)}</div>
        ${rel.length ? `<div class="deltas">${rel.join('')}</div>` : ''}</article>`;
    }
  }
}

function relEventChip(r) {
  const npc = S.state?.npcs.find((n) => n.id === r.npc);
  const parts = Object.entries(r.deltas || {}).filter(([, v]) => Math.abs(v) >= 0.5)
    .map(([k, v]) => `<span class="${v > 0 ? 'rc-up' : 'rc-dn'}">${k.slice(0, 3)} ${v > 0 ? '+' : ''}${Math.round(v)}</span>`);
  return `<span class="delta rel-chip"><b>${esc(npc?.name || r.npc)}</b> ${esc(r.event.replace(/_/g, ' '))} ${parts.join(' ')}</span>`;
}

function playerName(pid) {
  const p = ((S.session && S.session.players) || []).find((x) => x.player_id === pid);
  return p ? p.name : (pid === S.playerId ? 'You' : pid);
}

function deltaChip(d) {
  const npc = S.state ? S.state.npcs.find((n) => n.id === d.npc) : null;
  const parts = ['affinity', 'trust', 'fear', 'obligation']
    .filter((k) => Math.abs(d[k] || 0) >= 1)
    .map((k) => `${k.slice(0, 3)} ${Math.round(d[k]) > 0 ? '+' : ''}${Math.round(d[k])}`);
  return `<span class="delta"><b>${esc(npc ? npc.name : d.npc)}</b> ${esc(d.note || parts.join(' · ') || 'shifted')}</span>`;
}

/* ------------------------------------------------------------ the turn */
let stageTimer = null;

function thinking(on, who = '') {
  const box = $('#thinking');
  box.hidden = !on;
  $('#thinkingWho').textContent = who;
  if (S.role !== 'spectator') { $('#sendBtn').disabled = on; $('#actionInput').disabled = on; }
  clearInterval(stageTimer);
  const dots = $$('.stage-dot');
  if (!on) { dots.forEach((d) => d.classList.remove('on')); return; }
  let i = 0;
  dots.forEach((d) => d.classList.remove('on'));
  dots[0].classList.add('on');
  stageTimer = setInterval(() => {
    i = (i + 1) % dots.length;
    dots.forEach((d, j) => d.classList.toggle('on', j <= i));
  }, 800);
}

async function submitAction(text) {
  if (S.busy || !text.trim() || S.role === 'spectator' || !S.ptId) return;
  if (text.trim().startsWith('/')) return runCommand(text.trim());
  if (S.whisperTo) { const t = S.whisperTo; stopWhisper(); return sendWhisper(t, text.trim()); }

  S.busy = true; thinking(true);
  setView('story');
  const overSocket = S.ws && S.ws.readyState === WebSocket.OPEN;
  try {
    if (overSocket) {
      S.ws.send(JSON.stringify({ type: 'action', action: text.trim(),
                                 premium: $('#premiumToggle').checked }));
      return;
    }
    const r = await api(`/playthroughs/${S.ptId}/action?player=${encodeURIComponent(S.playerId)}`, {
      method: 'POST', body: { action: text.trim(), premium: $('#premiumToggle').checked },
    });
    if (r.blocked) {
      // A dead character is a different kind of "blocked": there is something
      // to DO about it, so offer the resolutions instead of a warning toast.
      if (r.dead) { toast(r.reason, 'warn'); await offerResolutions(); return; }
      toast(r.reason, 'warn'); return;
    }
    const { feed } = await api(`/playthroughs/${S.ptId}?player=${encodeURIComponent(S.playerId)}`);
    renderFeed(feed.filter((e) => e.id > S.lastFeedId));
    await refreshWorkspace();
    if (r.note) toast(r.note, 'warn');
    refreshStreak();
    await refreshAwareness();
    if (r.ending) showEnding(r.ending);
  } catch (err) {
    toast(`The world stalled: ${err.message}`, 'err');
  } finally {
    if (!overSocket) { S.busy = false; thinking(false); }
    $('#actionInput').focus();
  }
}

async function sendWhisper(target, text) {
  try {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.ws.send(JSON.stringify({ type: 'whisper', target_kind: target.kind, target_id: target.id, text }));
      return;
    }
    const out = await api(`/sessions/${S.sessionId}/whisper`, {
      method: 'POST',
      body: { player_id: S.playerId, target_kind: target.kind, target_id: target.id, text },
    });
    renderFeed([{ kind: 'whisper', turn: S.state.turn, text: '',
                  meta: { payload: out, label: `You to ${target.name}` } }]);
    refreshWorkspace();
  } catch (e) { toast(e.message, 'err'); }
}

/* ------------------------------------------------------------ WebSocket */
function handleAftermath(msg) {
  if (msg.summary) toast(msg.summary);
  if (msg.player_down) offerResolutions();
  refreshWorkspace();
}

function connectWS() {
  if (!S.sessionId) return;
  if (S.ws) { try { S.ws.close(); } catch { /* gone */ } }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/session/${S.sessionId}`
    + `?player=${encodeURIComponent(S.playerId)}&user_id=${encodeURIComponent(S.userId)}`);
  S.ws = ws;
  ws.onopen = () => { S.wsRetry = 0; };
  ws.onclose = () => {
    if (S.ws !== ws || !S.sessionId) return;
    S.wsRetry += 1;
    if (S.wsRetry <= 6) setTimeout(connectWS, Math.min(8000, 600 * S.wsRetry));
    else toast('Lost the room. Reload to rejoin.', 'err');
  };
  ws.onmessage = (ev) => { try { handleWS(JSON.parse(ev.data)); } catch { /* noise */ } };
  clearInterval(connectWS.ping);
  connectWS.ping = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
  }, 20000);
}

function handleWS(msg) {
  switch (msg.type) {
    case 'hello':
      S.session = msg.session; S.role = msg.role || S.role;
      S.state = msg.state; renderAll();
      break;
    case 'presence':
      if (msg.session) S.session = msg.session;
      if (S.session) { S.session.present = (msg.players || []).map((p) => p.player_id); renderParty(); }
      break;
    case 'thinking':
      if (msg.player_id !== S.playerId) thinking(true, `${msg.name} is acting…`);
      break;
    case 'turn':
      renderFeed(msg.entries || []);
      S.busy = false; thinking(false);
      if (msg.note && msg.by === S.playerId) toast(msg.note, 'warn');
      refreshWorkspace(); refreshStreak();
      break;
    case 'whisper': {
      const w = msg.payload || {};
      const label = msg.from === S.playerId ? `You to ${w.name || playerName(w.to)}`
        : `${msg.from_name || playerName(msg.from)} to you`;
      renderFeed([{ kind: 'whisper', turn: S.state?.turn || 0, text: '', meta: { payload: w, label } }]);
      break;
    }
    case 'reveal':
      toast('The reveal landed. Everything at once.');
      refreshWorkspace();
      break;
    case 'party':
      toast(`${playerName(msg.player_id)} left the group: “${msg.said}”`);
      refreshWorkspace();
      break;
    case 'combat':
      if (msg.event === 'resolved' || msg.event === 'start') { loadCombat().then(renderCombat); }
      if (msg.event === 'start') { toast('A fight has started.'); setView('combat'); }
      refreshWorkspace();
      break;
    case 'aftermath':
      handleAftermath(msg);
      break;
    case 'party_motion':
      toast('The table has a decision to make.');
      break;
    case 'xcard':
      toast('Someone touched the X-card. The scene is struck.', 'warn');
      break;
    case 'safety':
      S.safety = msg.payload; toast('Safety lines updated.');
      break;
    case 'card':
      toast(`Character card ${msg.event}.`);
      break;
    case 'queued':
      S.busy = false; thinking(false); toast(msg.message || 'Another player is mid-turn.', 'warn');
      break;
    case 'blocked':
      S.busy = false; thinking(false); toast(msg.reason, 'warn'); break;
    case 'error':
      S.busy = false; thinking(false); toast(msg.error, 'err'); break;
  }
}

/* ================================================================ OVERLAYS */
function closeOverlays() {
  $('#scrim').hidden = true; $('#modal').hidden = true;
  $('#soulDrawer').hidden = true; $('#palette').hidden = true;
}
function showModal(html) {
  $('#scrim').hidden = false; $('#modalCard').innerHTML = html; $('#modal').hidden = false;
}
const closeX = `<button class="close-btn" data-close>
  <svg viewBox="0 0 16 16" class="ico"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>`;
const head = (title, sub) => `<div class="modal-head"><div style="flex:1">
  <h2>${title}</h2>${sub ? `<p>${sub}</p>` : ''}</div>${closeX}</div>`;

/* ---------------------------------------------------------- view switch */
function setView(view) {
  S.view = view;
  $$('#tableTabs button').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('on', v.dataset.pane === view));
  if (view === 'atlas') renderAtlas();
  if (view === 'graph') renderGraph();
  if (view === 'known') renderKnown();
  if (view === 'combat') loadCombat().then(renderCombat);
}

/* --------------------------------------------------------- slash commands */
const COMMANDS = [
  { cmd: '/go', desc: 'Travel to a place you can reach', cost: '1 Mana', arg: 'place' },
  { cmd: '/look', desc: 'Take stock of the room', cost: 'free', run: () => setView('story') },
  { cmd: '/atlas', desc: 'Open the map', cost: 'free', run: () => setView('atlas') },
  { cmd: '/threads', desc: 'Open the narrative graph', cost: 'free', run: () => setView('graph') },
  { cmd: '/known', desc: 'What you know, who is coming', cost: 'free', run: () => setView('known') },
  { cmd: '/whisper', desc: 'Say something privately to one character', cost: '1 Mana', arg: 'who' },
  { cmd: '/stealth', desc: 'Could you move unseen right now?', cost: 'free', run: doStealth },
  { cmd: '/fight', desc: 'Start a fight with who is here', cost: 'free', run: startCombat },
  { cmd: '/depart', desc: 'Leave the group — say one thing, do another', cost: 'free', run: showDepart },
  { cmd: '/return', desc: 'Come back and write your account', cost: 'free', run: showReturn },
  { cmd: '/reveal', desc: 'Everyone reveals at once', cost: 'free', run: doReveal },
  { cmd: '/card', desc: 'Your character card', cost: 'free', run: showCard },
  { cmd: '/safety', desc: 'Lines, veils, and the X-card', cost: 'free', run: showSafety },
  { cmd: '/rules', desc: 'The laws the World Master enforces', cost: 'free', run: showRules },
  { cmd: '/cost', desc: 'What this story has cost', cost: 'free', run: () => showCost() },
  { cmd: '/settings', desc: 'Theme, density, power mode', cost: 'free', run: showSettings },
  { cmd: '/share', desc: 'Make a story card', cost: 'free', run: () => showShare() },
  { cmd: '/xcard', desc: 'Strike this scene. No reason needed.', cost: 'free', run: doXCard },
];

function openPalette(prefill = '') {
  $('#scrim').hidden = false;
  $('#palette').hidden = false;
  const input = $('#paletteInput');
  input.value = prefill;
  renderPalette(prefill);
  input.focus();
}

function renderPalette(q) {
  const query = (q || '').replace(/^\//, '').toLowerCase();
  const hits = COMMANDS.filter((c) => c.cmd.slice(1).startsWith(query) || c.desc.toLowerCase().includes(query));
  $('#paletteList').innerHTML = hits.length ? hits.map((c, i) => `
    <button class="pal-item ${i === 0 ? 'on' : ''}" data-cmd="${esc(c.cmd)}">
      <span class="pi-cmd">${esc(c.cmd)}</span>
      <span class="pi-desc">${esc(c.desc)}${c.arg ? ` <em style="color:var(--faint)">&lt;${c.arg}&gt;</em>` : ''}</span>
      <span class="pi-cost">${esc(c.cost)}</span></button>`).join('')
    : '<div class="pal-empty">No command like that. Just type what you do instead.</div>';
}

async function runCommand(raw) {
  const [word, ...rest] = raw.trim().split(/\s+/);
  const arg = rest.join(' ');
  const c = COMMANDS.find((x) => x.cmd === word);
  $('#actionInput').value = ''; autosize($('#actionInput'));
  if (!c) return toast(`No command ${word}. Press / to see them all.`, 'warn');
  if (word === '/go') {
    if (!arg) { setView('atlas'); return toast('Pick a place on the map.'); }
    return submitAction(`I make my way to ${arg}.`);
  }
  if (word === '/whisper') {
    const npc = (S.state?.npcs || []).find((n) => n.present &&
      n.name.toLowerCase().includes(arg.toLowerCase())) ;
    if (!arg || !npc) { setView('story'); return toast('Whisper to whom? Open a character and use Whisper.'); }
    return startWhisper('npc', npc.id, npc.name);
  }
  if (c.run) return c.run();
}

/* ------------------------------------------------------------- actions */
async function doStealth() {
  try {
    const s = await api(`/playthroughs/${S.ptId}/stealth?player=${encodeURIComponent(S.playerId)}`);
    showModal(`${head('Could you move unseen?', 'Pure arithmetic over light, noise and cover. No dice against a wall.')}
      <div class="modal-body">
        <div class="budget"><b>${s.hidden ? 'Yes — you are unseen.' : 'No — you would be spotted.'}</b>
          ${esc(s.reason)}</div>
        <div class="usage-grid" style="margin-top:14px">
          <div class="usage-row"><span>Light</span><span class="um">${s.light}/5</span><span class="uv">${s.light <= 1 ? 'dark' : s.light >= 4 ? 'bright' : 'dim'}</span></div>
          <div class="usage-row"><span>Noise</span><span class="um">${s.noise}/5</span><span class="uv">${s.noise >= 4 ? 'loud' : s.noise <= 1 ? 'silent' : 'ordinary'}</span></div>
          <div class="usage-row"><span>Cover here</span><span class="um">${s.cover}</span><span class="uv">${s.cover > 0.25 ? 'good' : 'thin'}</span></div>
          <div class="usage-row"><span>Concealment − exposure</span><span class="um">${s.concealment} − ${s.exposure}</span>
            <span class="uv">${s.margin > 0 ? '+' : ''}${s.margin}</span></div>
        </div>
        ${s.spotted_by.length ? `<p class="fineprint">Spotted by:
          ${esc(s.spotted_by.map((id) => S.state.npcs.find((n) => n.id === id)?.name || id).join(', '))}</p>` : ''}
        <p class="fineprint">This is the same maths the world uses to decide who witnessed what you did.</p>
      </div>`);
  } catch (e) { toast(e.message, 'err'); }
}

async function startCombat() {
  try {
    const c = await api(`/playthroughs/${S.ptId}/combat?player=${encodeURIComponent(S.playerId)}`, {
      method: 'POST', body: { place_id: '', enemies: [], allies: [] },
    });
    S.combat = c;
    await refreshWorkspace();
    setView('combat');
  } catch (e) { toast(e.message, 'err'); }
}

function showDepart() {
  showModal(`${head('Leave the group', 'Say one thing. Do another, if you like. Nothing comes out until the reveal.')}
    <div class="modal-body">
      <label class="big-field"><span>What you tell them</span>
        <input id="depSaid" maxlength="200" placeholder="I am going to find water."></label>
      <label class="big-field"><span>What you are actually doing (only you see this)</span>
        <input id="depTruth" maxlength="200" placeholder="Leave blank if they are the same."></label>
      <label class="big-field"><span>Private turns before you must return</span>
        <input id="depTurns" type="number" min="1" max="8" value="3"></label>
      <button class="btn btn-primary btn-lg" data-depart-go>Slip away</button>
      <p class="fineprint">While you are away your turns are yours alone. When everyone returns, all the
      accounts are revealed at the same moment — Diplomacy's rule, so an alliance is worth what the
      people in it are worth.</p>
    </div>`);
}

function showReturn() {
  showModal(`${head('Come back', 'Write your account. It seals until everyone has written theirs.')}
    <div class="modal-body">
      <label class="big-field"><span>What you tell them happened</span>
        <input id="retAccount" maxlength="200" placeholder="I found water."></label>
      <label class="big-field"><span>What actually happened</span>
        <input id="retTruth" maxlength="300" placeholder="I sold you to Corvin."></label>
      <button class="btn btn-primary btn-lg" data-return-go>Seal it</button>
      <p class="fineprint">If the two differ, whether it holds is decided by how much the people listening
      trust you — arithmetic, not a model's opinion.</p>
    </div>`);
}

async function doReveal() {
  try {
    const out = await api(`/playthroughs/${S.ptId}/reveal`, { method: 'POST' });
    if (!out.revealed.length) return toast('Nothing staged to reveal.', 'warn');
    const { feed } = await api(`/playthroughs/${S.ptId}?player=${encodeURIComponent(S.playerId)}`);
    renderFeed(feed.filter((e) => e.id > S.lastFeedId));
    setView('story');
    refreshWorkspace();
  } catch (e) { toast(e.message, 'err'); }
}

async function doXCard() {
  if (!confirm('Strike this scene? No reason needed, and nobody will ask for one.')) return;
  try {
    await api(`/playthroughs/${S.ptId}/xcard`, { method: 'POST',
      body: { player_id: S.playerId, note: '' } });
    const { feed } = await api(`/playthroughs/${S.ptId}?player=${encodeURIComponent(S.playerId)}`);
    renderFeed(feed.filter((e) => e.id > S.lastFeedId));
    setView('story');
    toast('Struck. We move on.');
  } catch (e) { toast(e.message, 'err'); }
}

async function showSafety() {
  const s = S.safety || await api(`/playthroughs/${S.ptId}/safety`);
  S.safety = s;
  showModal(`${head('Session Zero', 'Lines are never. Veils happen off-screen. Both are editable at any moment, by anyone.')}
    <div class="modal-body">
      <label class="big-field"><span>Lines — these never appear, in any form</span>
        <textarea id="safLines" rows="3" placeholder="One per line">${esc((s.lines || []).join('\n'))}</textarea></label>
      <label class="big-field"><span>Veils — these exist but happen off-screen</span>
        <textarea id="safVeils" rows="3" placeholder="One per line">${esc((s.veils || []).join('\n'))}</textarea></label>
      <button class="btn btn-primary" data-safety-save>Save</button>
      <button class="btn btn-ghost" data-xcard>Touch the X-card now</button>
      <p class="fineprint">A Line is enforced before anything is written: the action is cancelled at the
      framework level and the model never gets a vote. This outranks canon, the world's rules, and the
      Narrator.</p>
    </div>`);
}

async function showCard() {
  const cards = await api(`/playthroughs/${S.ptId}/cards`);
  const mine = (cards.cards || []).find((c) => c.player_id === S.playerId);
  const a = mine?.aspects || {};
  // Remembered so the identity panel can be opened straight from the menu
  // without making the player find their card again first.
  S.cardId = mine?.id || null;
  if (mine?.identity) S.identityDraft = mine.identity;
  if (mine?.avatar_url) S.identityAvatar = mine.avatar_url;
  showModal(`${head('Your character card', 'Leave anything blank and roll it, or let the cheapest model fill it in.')}
    <div class="modal-body">
      <label class="big-field"><span>Name</span><input id="cdName" maxlength="40" value="${esc(mine?.name || '')}"></label>
      <label class="big-field"><span>Concept</span><input id="cdConcept" maxlength="120" value="${esc(mine?.concept || '')}" placeholder="a courier who stopped running"></label>
      <label class="big-field"><span>Voice</span><textarea id="cdVoice" rows="2" placeholder="How you speak.">${esc(a.voice || '')}</textarea></label>
      <label class="big-field"><span>Drive</span><input id="cdDrive" maxlength="160" value="${esc(a.drive || '')}"></label>
      <label class="big-field"><span>Flaw</span><input id="cdFlaw" maxlength="160" value="${esc(a.flaw || '')}"></label>
      <label class="big-field"><span>Anomaly — something this world does not normally allow</span>
        <input id="cdAnomaly" maxlength="200" value="${esc(mine?.anomaly || '')}" placeholder="Leave blank unless you mean it."></label>
      <div class="forge-actions">
        <button class="btn btn-ghost" data-card-roll>Roll the blanks</button>
        <button class="btn btn-ghost" data-card-autofill>Fill with AI <em style="color:var(--faint);font-style:normal">1 Mana</em></button>
        <button class="btn btn-primary" data-card-save="${esc(mine?.id || '')}">Save</button>
      </div>
      ${mine ? `<p class="fineprint">Status: <b>${esc(mine.status)}</b>.
        ${S.sessionId ? 'Every seated player approves a card before it enters play, and an approved anomaly becomes a world rule the World Master will enforce.'
          : 'Playing solo, you are the whole table.'}</p>` : ''}
    </div>`);
}

function showSettings() {
  const seg = (key, opts) => `<div class="seg">${opts.map(([v, l]) =>
    `<button class="${S.prefs[key] === v ? 'on' : ''}" data-pref="${key}" data-val="${v}">${l}</button>`).join('')}</div>`;
  showModal(`${head('How it looks', 'Per person, remembered on this device and your account.')}
    <div class="modal-body">
      <div class="setting-row"><div class="sr-main"><div class="sr-t">Reading light</div>
        <div class="sr-d">Ink is built for long sessions; parchment for daylight.</div></div>
        ${seg('theme', [['ink', 'Ink'], ['parchment', 'Parchment']])}</div>
      <div class="setting-row"><div class="sr-main"><div class="sr-t">Density</div>
        <div class="sr-d">Compact narrows the rails and fits more on screen.</div></div>
        ${seg('density', [['comfortable', 'Comfortable'], ['compact', 'Compact']])}</div>
      <div class="setting-row"><div class="sr-main"><div class="sr-t">Power mode</div>
        <div class="sr-d">Shows every relationship scalar, slash hints, and the raw numbers behind
        each panel. Off by default — casual play never needs them.</div></div>
        ${seg('power', [[false, 'Off'], [true, 'On']])}</div>
      <div class="setting-row"><div class="sr-main"><div class="sr-t">Cost table</div>
        <div class="sr-d">What actually costs Mana, and what is free.</div></div>
        <button class="btn btn-ghost" data-show-budget>Show</button></div>
    </div>`);
}

function showBudget() {
  const b = S.budgetTable || {};
  showModal(`${head('What costs what', 'Mana prices model calls. Everything deterministic is free, and runs every turn regardless.')}
    <div class="modal-body">
      <div class="known-col"><h3>Costs Mana (a model is called)</h3>
        ${Object.entries(b.llm_actions || {}).map(([k, v]) =>
          `<div class="usage-row"><span>${esc(k.replace(/_/g, ' '))}</span><span class="um"></span>
           <span class="uv">${v} Mana</span></div>`).join('')}</div>
      <div class="known-col" style="margin-top:18px"><h3>Free, forever</h3>
        <div class="tags">${(b.free_actions || []).map((f) =>
          `<span class="tag tag-ember">${esc(f.replace(/_/g, ' '))}</span>`).join('')}</div>
        <p class="fineprint">Stats, reputation, stealth, witnessing, combat maths, pre-commit, relationship
        deltas and the world tick are all arithmetic. They run for every entity every turn and cost nothing,
        which is the only reason a world this busy is affordable.</p></div>
      <div class="budget" style="margin-top:16px">At most <b>${b.max_llm_calls_per_turn}</b> model calls per
      turn, enforced in code — a turn that tries more raises rather than quietly spending.</div>
    </div>`);
}

/* ------------------------------------------------------------ soul drawer */
async function showSoul(npcId) {
  $('#scrim').hidden = false;
  const d = $('#soulDrawer');
  d.hidden = false;
  d.innerHTML = '<div class="drawer-body"><div class="empty">Opening a mind…</div></div>';
  let n;
  try {
    n = await api(`/playthroughs/${S.ptId}/npc/${npcId}?player=${encodeURIComponent(S.playerId)}`);
  } catch (e) {
    d.innerHTML = `<div class="drawer-body"><div class="empty">${esc(e.message)}</div></div>`; return;
  }
  const a = n.anchors, rel = n.relationship_to_user;
  const live = S.state?.npcs.find((x) => x.id === npcId) || {};
  const li = (arr) => `<ul>${arr.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`;
  const others = n.remembers_others || [];

  d.innerHTML = `
    <div class="drawer-head"><div class="dh-top">
      <div style="flex:1"><h2>${esc(n.name)}${n.alive ? '' : ' &dagger;'}</h2>
        <div class="dh-role">${esc(live.disposition || n.role)}</div></div>${closeX}
    </div></div>
    <div class="drawer-body">
      ${n.alive ? `<button class="btn btn-ghost" data-whisper-npc="${n.id}" data-name="${esc(n.name)}">
        <svg viewBox="0 0 16 16" class="ico"><path d="M8 1.6a5.4 5.4 0 0 0-3.4 9.6V14l2.2-1.2a5.4 5.4 0 1 0 1.2-11.2z"/></svg>
        Whisper to ${esc(n.name)}</button>` : ''}
      <section class="sect"><h3>How they feel about you</h3>
        <div class="bars">
          ${bar('Affinity', 'affinity', rel.affinity)}${bar('Trust', 'trust', rel.trust)}
          ${bar('Fear', 'fear', rel.fear)}${bar('Duty', 'obligation', rel.obligation)}
          ${bar('Loyalty', 'loyalty', live.loyalty)}${bar('Love', 'love', live.love)}
          ${bar('Respect', 'respect', live.respect)}
        </div>
        <p class="anchor-note">${live.will_cover ? '<b>They would cover for you.</b> ' : ''}
        These move by a repeated trust game: kindness compounds with diminishing returns, and a betrayal
        costs a multiple of whatever trust it broke. No model is asked how they feel — it is told.
        ${others.length ? `<br><br>This character keeps ${others.length} separate memory stream${others.length > 1 ? 's' : ''}
        of the other players.` : ''}</p>
      </section>
      <section class="sect"><h3>Persona anchors — injected every single turn</h3>
        <div class="anchor-grid">
          <div class="anchor-row"><div class="ak">Voice</div><div class="av">${esc(a.voice)}</div></div>
          <div class="anchor-row"><div class="ak">Constraints</div><div class="av">${li(a.constraints)}</div></div>
          <div class="anchor-row"><div class="ak">Wants</div><div class="av">${li(a.goals)}</div></div>
          <div class="anchor-row"><div class="ak">Never</div><div class="av">${li(a.taboos)}</div></div>
        </div>
        <p class="anchor-note">Constant, never summarised. A proposed action that breaks one of these is
        cancelled before it happens — the model does not get a vote.</p></section>
      ${(live.plan || []).length ? `<section class="sect"><h3>What they intend next</h3>
        <div class="plan-list">${live.plan.map((p) => `<div class="plan-item">${esc(p)}</div>`).join('')}</div></section>` : ''}
      ${n.reflections.length ? `<section class="sect"><h3>Conclusions about you</h3>
        <div class="mem-list">${n.reflections.slice().reverse().map((r) => `
          <div class="mem k-reflection"><div class="mem-meta"><span>turn ${r.turn}</span><span>reflection</span></div>${esc(r.text)}</div>`).join('')}</div></section>` : ''}
      <section class="sect"><h3>Memory stream <span style="color:var(--faint);font-weight:400">&middot; ${n.memories.length}</span></h3>
        <div class="mem-list">${n.memories.length ? n.memories.map((m) => `
          <div class="mem k-${esc(m.kind)}"><div class="mem-meta"><span>turn ${m.turn}</span><span>${esc(m.kind)}</span>
          <span>weight ${m.importance}</span>${m.player_id === '*' ? '<span>known to all</span>' : ''}</div>${esc(m.text)}</div>`).join('')
          : '<div class="empty">Nothing observed yet.</div>'}</div></section>
    </div>`;
}

/* --------------------------------------------------- the rest of the modals */
async function showTimeline() {
  if (!S.ptId) return;
  showModal(head('Memory &amp; Timeline', 'Loading…'));
  const { events } = await api(`/playthroughs/${S.ptId}/arc`);
  const keys = [['action', 'var(--vellum-dim)'], ['fate', 'var(--ember)'], ['rejection', 'var(--crimson)'],
                ['npc', 'var(--verdigris)'], ['beat', 'var(--rose)'], ['contest', 'var(--ember-soft)']];
  showModal(`${head('Memory &amp; Timeline', `${events.length} events, append-only. The actual memory — not a transcript.`)}
    <div class="modal-body">
      <div class="tl-legend">${keys.map(([k, c]) => `<span class="tl-key"><i style="background:${c}"></i>${k}</span>`).join('')}</div>
      <div class="tl">${events.slice().reverse().map((e) => `
        <div class="tl-row k-${esc(e.kind)}"><span class="tl-turn">T${e.turn}</span>
          <span class="tl-actor">${esc(e.actor)}</span>
          <span class="tl-text"><b>${esc(e.action)}</b>
            ${e.consequence ? `<span class="cons"> &mdash; ${esc(e.consequence)}</span>` : ''}
            ${e.rule_ref ? `<span class="rule-chip">${esc(e.rule_ref)}</span>` : ''}</span></div>`).join('')}</div>
    </div>`);
}

async function showPricing() {
  const e = S.economy || {}; const packs = e.packs || []; const m = S.state?.mana;
  if (!S.payments) { try { S.payments = await api('/payments/status'); } catch { S.payments = { configured: false }; } }
  const live = S.payments.configured;
  const promises = [
    ['You are never blocked.', 'Out of Mana drops you to Ember — a leaner narrator, a quieter world. Free, unlimited, forever.'],
    ['Mana prices model calls, not turns.', 'Exploring, checking stealth, reading the map, moving relationships — all free. Only a model call costs.'],
    ['One host brings friends free.', `Up to ${e.free_friends_per_host || 5} friends pay nothing; several payers stack allowances up to ${e.max_allowance_stack || 5}x.`],
    ['Mana never expires.', 'Buy once, use it in a year. No subscription.'],
    ['Memory is never sold back to you.', 'The thing that makes this work is the product, not a tier.'],
  ];
  showModal(`${head('Mana', 'Pay for what you use. No subscription, no expiry, no locked door.')}
    <div class="modal-body">
      ${m ? `<div class="budget"><b>${m.balance}</b> Mana &middot; <b>${m.free_left}</b> of ${m.free_daily} free today
        ${m.mode === 'ember' ? '&middot; in <b>Ember</b>' : ''}</div>` : ''}
      <div class="price-grid" style="margin-top:16px">${packs.map((p) => `
        <div class="price ${p.featured ? 'featured' : ''}">${p.featured ? '<span class="price-flag">Best value</span>' : ''}
          <div class="price-name">${esc(p.name)}</div><div class="price-mana">${p.mana.toLocaleString()} Mana</div>
          <div class="price-usd">$${p.usd}<small> once</small></div><div class="price-note">${esc(p.note)}</div>
          <div class="price-per">&asymp; $${p.usd_per_action.toFixed(4)} an action</div>
          ${live
            ? `<div class="paypal-slot" id="pp-${esc(p.id)}" data-pack="${esc(p.id)}"></div>`
            : `<button class="btn btn-ghost" data-buy="${p.id}">Add Mana (dev)</button>`}
        </div>`).join('')}</div>
      <div class="promise-grid">${promises.map(([t, s]) => `<div class="promise">
        <svg viewBox="0 0 16 16" class="ico"><path d="M2.8 8.4 6 11.6l7.2-7.2"/></svg>
        <div><b>${esc(t)}</b> ${esc(s)}</div></div>`).join('')}</div>
      <p class="fineprint">${live
        ? 'Paid through PayPal. The order amount is fixed server-side from this price table — we never trust an amount the browser sends — and Mana is granted only after PayPal itself confirms the capture.'
        : 'PayPal is not configured on this deployment, so “Add Mana” is a dev-only instant grant. Set PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET to go live.'}
      See the full cost table under <b>/settings → Cost table</b>.</p></div>`);
  if (live) mountPaypalButtons(packs.map((p) => p.id));
}

let _paypalSdkPromise = null;

function loadPaypalSdk(clientId) {
  if (window.paypal) return Promise.resolve(window.paypal);
  if (_paypalSdkPromise) return _paypalSdkPromise;
  _paypalSdkPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&currency=USD&intent=capture`;
    s.onload = () => resolve(window.paypal);
    s.onerror = () => { _paypalSdkPromise = null; reject(new Error('PayPal SDK failed to load')); };
    document.head.appendChild(s);
  });
  return _paypalSdkPromise;
}

async function mountPaypalButtons(packIds) {
  if (!S.payments?.client_id) return;
  let paypal;
  try { paypal = await loadPaypalSdk(S.payments.client_id); }
  catch (e) { return toast('Could not reach PayPal.', 'err'); }
  for (const packId of packIds) {
    const slot = document.getElementById(`pp-${packId}`);
    if (!slot || !S.ptId) continue;
    paypal.Buttons({
      style: { layout: 'horizontal', height: 34, tagline: false, label: 'pay' },
      createOrder: async () => {
        const r = await api(`/playthroughs/${S.ptId}/purchase/paypal/create-order`, {
          method: 'POST', body: { pack_id: packId },
        });
        return r.order_id;
      },
      onApprove: async (data) => {
        try {
          const r = await api(`/playthroughs/${S.ptId}/purchase/paypal/capture?order_id=${encodeURIComponent(data.orderID)}`,
            { method: 'POST' });
          S.state = r.state; renderAll(); closeOverlays();
          toast(`${r.granted.toLocaleString()} Mana added. It never expires.`);
        } catch (err) { toast(`Payment captured but Mana grant failed: ${err.message}. Contact support with order ${data.orderID}.`, 'err'); }
      },
      onError: () => toast('PayPal could not complete that payment.', 'err'),
    }).render(`#pp-${packId}`);
  }
}

async function showEthics() {
  if (!S.ethics) S.ethics = await api('/ethics');
  showModal(`${head('Your data is yours', 'The part competitors cannot copy without changing their business model.')}
    <div class="modal-body">
      <span class="ethics-badge"><svg viewBox="0 0 16 16" class="ico"><path d="M8 1.5 13.5 4v4.2c0 3-2.3 5.4-5.5 6.3C4.8 13.6 2.5 11.2 2.5 8.2V4z"/><path d="M5.8 8.2 7.3 9.7l3-3.2"/></svg>
        No training on your stories &middot; Export any time &middot; No exit-guilting</span>
      <div style="margin-top:18px">${(S.ethics.promises || []).map((p) => `<div class="ethic">
        <svg viewBox="0 0 16 16" class="ico"><path d="M2.8 8.4 6 11.6l7.2-7.2"/></svg>
        <div><h4>${esc(p.title)}</h4><p>${esc(p.body)}</p></div></div>`).join('')}</div>
      <p class="fineprint">${esc(S.ethics.data_retention || '')}</p></div>`);
}

async function showStreak() {
  const s = S.streak || await api('/streak');
  const marks = [3, 7, 14, 30, 60, 100, 365];
  showModal(`${head('Campaign streak', 'A number you built. Nothing here will nag you to keep it.')}
    <div class="modal-body"><div class="streak-hero">
      <div class="streak-num">${s.current}</div>
      <div class="streak-word">${s.current === 1 ? 'day' : 'days'} running</div>
      <div class="streak-meta">
        <div><b>${s.longest}</b><span>longest ever</span></div>
        <div><b>${s.total_days}</b><span>days played</span></div>
        ${s.to_next ? `<div><b>${s.to_next}</b><span>to ${s.next_milestone} days</span></div>` : ''}</div>
      <div class="milestones">${marks.map((m) => `<span class="ms ${s.longest >= m ? 'hit' : ''}">${m} days</span>`).join('')}</div>
    </div>
    <div class="notice calm"><svg viewBox="0 0 16 16" class="ico"><path d="M8 1.5 13.5 4v4.2c0 3-2.3 5.4-5.5 6.3C4.8 13.6 2.5 11.2 2.5 8.2V4z"/></svg>
      <div><b>This is not a leash.</b> ${esc(s.note)} No notifications, no streak freezes for sale,
      and no character acts hurt when you take a week off.</div></div>
    <button class="btn btn-ghost btn-lg" data-share-streak>Share this run</button></div>`);
}

async function showShare() {
  if (!S.ptId) return;
  showModal(head('Share this moment', 'Rendering…'));
  const { text } = await api(`/playthroughs/${S.ptId}/card?player=${encodeURIComponent(S.playerId)}`);
  const src = `/api/playthroughs/${S.ptId}/card.svg?user_id=${encodeURIComponent(S.userId)}`
    + `&player=${encodeURIComponent(S.playerId)}&t=${Date.now()}`;
  showModal(`${head('Share this moment', 'Built from your actual state, and it carries the room code.')}
    <div class="modal-body"><div class="card-preview"><img src="${src}" alt="Story card"></div>
      <textarea class="share-text" id="shareText">${esc(text)}</textarea>
      <div class="share-row">
        <button class="btn btn-primary" data-share="copy">Copy post</button>
        <button class="btn btn-ghost" data-share="image">Open image</button>
        <button class="btn btn-ghost" data-share="native">Share&hellip;</button></div>
      <p class="fineprint">One self-contained SVG. No tracker, no font fetch, nothing phones home.</p></div>`);
}

function showHost() {
  const opts = [...S.worlds.filter((w) => w.kind === 'starter'), ...S.worlds.filter((w) => w.kind === 'mine')];
  showModal(`${head('Host a room', 'A six-character code. Anyone with it is playing inside a minute.')}
    <div class="modal-body">
      <label class="big-field"><span>Your name in the story</span>
        <input id="hostName" maxlength="24" value="${esc(localStorage.getItem('storyliver.name') || '')}" placeholder="Vale"></label>
      <label class="big-field"><span>World</span>
        <select id="hostWorld" style="width:100%;background:var(--panel-2);border:1px solid var(--line);border-radius:var(--r);padding:13px 15px;color:var(--vellum);font-size:15px">
          ${opts.map((w) => `<option value="${esc(w.id)}">${esc(w.name)}${w.kind === 'mine' ? ' (yours)' : ''}</option>`).join('')}
        </select></label>
      <div class="mode-grid" id="modeGrid">
        <button class="mode-opt on" data-mode="coop">
          <svg viewBox="0 0 16 16" class="ico"><circle cx="5.5" cy="5" r="2.2"/><circle cx="11" cy="6" r="1.8"/><path d="M1.6 13.5c.4-2.4 1.9-3.6 3.9-3.6s3.5 1.2 3.9 3.6M10 9.9c1.7.1 2.9 1.3 3.3 3.3"/></svg>
          <div><b>Co-op</b><span>One party, one world. Every character still remembers each of you separately.</span></div></button>
        <button class="mode-opt" data-mode="chaos">
          <svg viewBox="0 0 16 16" class="ico"><path d="M3 13 13 3M6 3H3v3M10 13h3v-3"/></svg>
          <div><b>Chaos</b><span>Competing goals. Contested moves are settled on state, then a roll. Splitting and betrayal are on.</span></div></button>
      </div>
      <div class="notice calm"><svg viewBox="0 0 16 16" class="ico"><path d="M8 1.5 4 8.6h3L6.5 14.5 12 7h-3z"/></svg>
        <div><b>Your wallet funds the room.</b> Up to ${(S.economy && S.economy.free_friends_per_host) || 5} friends play free.</div></div>
      <button class="btn btn-primary btn-lg" id="hostGo">Open the room</button></div>`);
}

async function createRoom() {
  const name = $('#hostName').value.trim() || 'Host';
  localStorage.setItem('storyliver.name', name);
  $('#hostGo').disabled = true;
  try {
    const r = await api('/sessions', { method: 'POST', body: {
      user_id: S.userId, world_id: $('#hostWorld').value,
      mode: $('.mode-opt.on')?.dataset.mode || 'coop', host_name: name } });
    S.session = r.session;
    closeOverlays();
    await openPlaythrough(r.session.playthrough_id, { sessionId: r.session.id, playerId: 'host', role: 'host' });
    showRoomCode(r.session.code);
  } catch (e) { toast(e.message, 'err'); const b = $('#hostGo'); if (b) b.disabled = false; }
}

function showRoomCode(code) {
  showModal(`${head('The room is open', 'Send this code. That is the whole invitation.')}
    <div class="modal-body"><div class="code-hero"><div class="code-big">${esc(code)}</div>
      <p class="code-note">Friends enter it on the front page and they are in — no account, nothing to pay.</p></div>
      <div class="share-row"><button class="btn btn-primary" data-copy-code="${esc(code)}">Copy the code</button>
        <button class="btn btn-ghost" data-close>Start playing</button></div></div>`);
}

async function showLibrary() {
  const saves = await api('/playthroughs');
  const list = saves.playthroughs || [];
  showModal(`${head('Your stories', `${list.length} in progress.`)}
    <div class="modal-body">${list.length ? `<div class="world-list">${list.map((p) => `
      <div class="world-row"><button class="wr-main" data-load="${p.id}" style="text-align:left;background:none">
        <div class="wr-name">${esc(p.world_name || p.title)} &mdash; day ${p.day}</div>
        <div class="wr-sub">turn ${p.current_turn} &middot; ${esc(p.location_name)} &middot; ${p.mana_balance} Mana${p.session_id ? ' &middot; in a room' : ''}</div>
      </button><div class="wr-actions"><button data-del="${p.id}">Delete</button></div></div>`).join('')}</div>`
      : '<div class="empty">Nothing yet.</div>'}</div>`);
}

async function showForge() {
  const worlds = await api('/forge/worlds');
  const suggestions = ['a frozen post-collapse Earth', 'a cyberpunk megacity in 2200',
    'a lighthouse at the end of the world', 'a generation ship 200 years in',
    'a plague town in 1348', 'a moon colony losing air'];
  showModal(`${head('World Forge', 'You are the world-builder. No author role — whoever builds it is just the host.')}
    <div class="modal-body">
      <div class="forge-tabs"><button class="on" data-forge-tab="bootstrap">Name a setting</button>
        <button data-forge-tab="mine">Your worlds</button><button data-forge-tab="scratch">From scratch</button></div>
      <div class="forge-pane on" data-forge-pane="bootstrap">
        <p class="forge-intro">Type any world. It builds places, people with persona anchors, nine enforced
        laws and seven fated events — then it is yours to edit.</p>
        <div class="mode-pick">
          <button class="mode-card on" data-build-mode="auto">
            <b>Decide for me</b><small>Look it up. If it is a real setting, continue it —
              if not, build it fresh.</small></button>
          <button class="mode-card" data-build-mode="canon">
            <b>Continue a real world</b><small>Read about it online and use its actual
              characters, places and factions.</small></button>
          <button class="mode-card" data-build-mode="original">
            <b>Build my own</b><small>Nothing is looked up. Pure invention, from
              your description alone.</small></button>
        </div>
        <label class="big-field"><span>The setting</span><input id="bootSetting" maxlength="160" placeholder="a frozen post-collapse Earth"></label>
        <div class="suggest">${suggestions.map((s) => `<button data-suggest="${esc(s)}">${esc(s)}</button>`).join('')}</div>
        <div id="researchPeek" class="research-peek"></div>
        <label class="big-field"><span>Tone (optional)</span><input id="bootTone" maxlength="160" placeholder="quiet, cold, and merciless"></label>
        <div class="notice"><svg viewBox="0 0 16 16" class="ico"><path d="M8 5.5v3.6M8 11.4v.1"/><circle cx="8" cy="8" r="6.4"/></svg>
          <div><b>If you name someone else's fiction</b>, you get an <b>inspired-by personal world</b> —
          original characters in that spirit, kept private, never publishable, and not affiliated with any
          rights holder. Worlds you want to share must be original.</div></div>
        <button class="btn btn-primary btn-lg" id="bootGo">Build the world</button></div>
      <div class="forge-pane" data-forge-pane="mine">
        ${(worlds.worlds || []).length ? `<div class="world-list">${worlds.worlds.map((w) => `
          <div class="world-row"><div class="wr-main"><div class="wr-name">${esc(w.name)}</div>
            <div class="wr-sub">${esc(w.tagline || w.origin)}</div></div>
            ${w.personal_only ? '<span class="badge personal">personal</span>' : ''}
            <div class="wr-actions"><button data-play-world="${w.id}">Play</button>
              <button data-edit-world="${w.id}">Edit</button><button data-export-world="${w.id}">Export</button>
              <button data-delete-world="${w.id}">Delete</button></div></div>`).join('')}</div>`
          : '<div class="empty">No worlds yet.</div>'}</div>
      <div class="forge-pane" data-forge-pane="scratch">
        <p class="forge-intro">Start from a valid skeleton and rewrite every part of it.</p>
        <label class="big-field"><span>World name</span><input id="scratchName" maxlength="60" placeholder="The Hollow"></label>
        <button class="btn btn-primary btn-lg" id="scratchGo">Open the editor</button></div>
    </div>`);
}

let researchTimer = null;

async function peekResearch() {
  const setting = $('#bootSetting')?.value.trim();
  const box = $('#researchPeek');
  if (!box) return;
  if (!setting || setting.length < 3 || S.buildMode === 'original') { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="rp-busy">Looking this up…</div>';
  try {
    const r = await api(`/forge/research?setting=${encodeURIComponent(setting)}`);
    if ($('#bootSetting')?.value.trim() !== setting) return;   // the player moved on
    if (!r.enabled) { box.innerHTML = ''; return; }
    if (!r.found) {
      // Saying so is the point: an invented setting and a real one should not
      // look identical right up until the world is built.
      box.innerHTML = `<div class="rp rp-none"><b>Nothing found for that name.</b>
        <span>It will be built from imagination — which is exactly right for an
        original setting.</span></div>`;
      return;
    }
    box.innerHTML = `<div class="rp rp-found">
      <b>Found it — ${esc(r.canonical_name || r.setting)}</b>
      <span>Your world will use the real names from this setting.</span>
      ${r.characters.length ? `<div class="rp-tags">${r.characters.slice(0, 8)
        .map((c) => `<i>${esc(c)}</i>`).join('')}</div>` : ''}
      ${r.places.length ? `<div class="rp-tags rp-dim">${r.places.slice(0, 5)
        .map((c) => `<i>${esc(c)}</i>`).join('')}</div>` : ''}
      <div class="rp-src">from ${r.sources.map((x) =>
        `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.source)}</a>`)
        .join(' and ')} · ${esc(r.sources[0]?.license || 'CC BY-SA')}</div>
    </div>`;
  } catch { box.innerHTML = ''; }
}

async function runBootstrap() {
  const setting = $('#bootSetting').value.trim();
  if (!setting) return toast('Name a setting first.', 'warn');
  const btn = $('#bootGo'); btn.disabled = true;
  btn.textContent = (S.buildMode === 'original') ? 'Building it…' : 'Reading up on it…';
  try {
    const r = await api('/forge/bootstrap', { method: 'POST', body: {
      user_id: S.userId, setting, tone: $('#bootTone').value.trim(),
      mode: S.buildMode || 'auto', save: true } });
    openEditor(r.world, (r.saved && r.saved.id) || null, r.notice);
  } catch (e) { toast(e.message, 'err'); btn.disabled = false; btn.textContent = 'Build the world'; }
}

const feText = (v) => esc(v == null ? '' : String(v));
const feLines = (arr) => esc((arr || []).join('\n'));

function openEditor(world, worldId, notice) {
  S.forge = { world: JSON.parse(JSON.stringify(world)), id: worldId };
  const w = S.forge.world;
  const locOpts = w.locations.map((l) => l.id);
  const section = (key, title, count, body) => `
    <div class="fe-section" data-fe="${key}"><button type="button" class="fe-head" data-fe-toggle="${key}">
      <span class="chev">&#9656;</span><h4>${title}</h4><span class="fe-n">${count}</span></button>
      <div class="fe-body">${body}</div></div>`;
  const sel = (name, value) => `<select data-f="${name}">${locOpts.map((l) =>
    `<option value="${esc(l)}"${l === value ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select>`;

  const places = w.locations.map((l, i) => `
    <div class="fe-item" data-kind="locations" data-i="${i}">
      <div class="fe-row"><label>Name</label><input data-f="name" value="${feText(l.name)}"></div>
      <div class="fe-row"><label>Description</label><textarea data-f="desc" rows="2">${feText(l.desc)}</textarea></div>
      <div class="fe-row"><label>Connects to</label><input data-f="connects" value="${feText((l.connects || []).join(', '))}"></div>
      <button class="fe-del" data-del-item>Remove</button></div>`).join('')
    + '<button class="fe-add" data-add="locations">+ Add a place</button>';
  const people = w.npcs.map((n, i) => `
    <div class="fe-item" data-kind="npcs" data-i="${i}">
      <div class="fe-row"><label>Name</label><input data-f="name" value="${feText(n.name)}"></div>
      <div class="fe-row"><label>Role</label><input data-f="role" value="${feText(n.role)}"></div>
      <div class="fe-row"><label>Voice</label><textarea data-f="anchors.voice" rows="2">${feText(n.anchors.voice)}</textarea></div>
      <div class="fe-row"><label>Constraints</label><textarea data-f="anchors.constraints" rows="2">${feLines(n.anchors.constraints)}</textarea></div>
      <div class="fe-row"><label>Wants</label><textarea data-f="anchors.goals" rows="2">${feLines(n.anchors.goals)}</textarea></div>
      <div class="fe-row"><label>Never</label><textarea data-f="anchors.taboos" rows="2">${feLines(n.anchors.taboos)}</textarea></div>
      <div class="fe-row"><label>Starts at</label>${sel('start_location', n.start_location)}</div>
      <button class="fe-del" data-del-item>Remove</button></div>`).join('')
    + '<button class="fe-add" data-add="npcs">+ Add a character</button>';
  const laws = w.rules.map((r, i) => `
    <div class="fe-item" data-kind="rules" data-i="${i}">
      <div class="fe-row"><label>The law</label><textarea data-f="text" rows="2">${feText(r.text)}</textarea></div>
      <div class="fe-row"><label>Catch words</label><input data-f="check.pattern" value="${feText(r.check && r.check.pattern)}" placeholder="regex — blank for semantic-only"></div>
      <div class="fe-row"><label>Refusal</label><input data-f="check.reason" value="${feText(r.check && r.check.reason)}"></div>
      <button class="fe-del" data-del-item>Remove</button></div>`).join('')
    + '<button class="fe-add" data-add="rules">+ Add a law</button>';
  const fate = w.fated_events.map((f, i) => `
    <div class="fe-item" data-kind="fated_events" data-i="${i}">
      <div class="fe-row"><label>Turn</label><input data-f="turn" type="number" min="1" value="${feText(f.turn)}"></div>
      <div class="fe-row"><label>Title</label><input data-f="title" value="${feText(f.title)}"></div>
      <div class="fe-row"><label>What happens</label><textarea data-f="desc" rows="2">${feText(f.desc)}</textarea></div>
      <div class="fe-row"><label>Where</label>${sel('location', f.location)}</div>
      <div class="fe-row"><label>Kills</label><select data-f="kills"><option value="">nobody</option>
        ${w.npcs.map((n) => `<option value="${esc(n.id)}"${n.id === f.kills ? ' selected' : ''}>${esc(n.name)}</option>`).join('')}</select></div>
      <button class="fe-del" data-del-item>Remove</button></div>`).join('')
    + '<button class="fe-add" data-add="fated_events">+ Add a fated event</button>';

  showModal(`${head('World Forge', `Editing &ldquo;${esc(w.name)}&rdquo;`)}
    <div class="modal-body">
      ${notice ? `<div class="notice"><svg viewBox="0 0 16 16" class="ico"><path d="M8 5.5v3.6M8 11.4v.1"/><circle cx="8" cy="8" r="6.4"/></svg><div>${esc(notice)}</div></div>` : ''}
      <div class="forge-editor">
        <div class="fe-section open"><div class="fe-body" style="display:block;padding-top:14px">
          <div class="fe-row"><label>Name</label><input id="wName" value="${feText(w.name)}"></div>
          <div class="fe-row" style="margin-top:8px"><label>Tagline</label><input id="wTagline" value="${feText(w.tagline)}"></div>
          <div class="fe-row" style="margin-top:8px"><label>Premise</label><textarea id="wPremise" rows="4">${feText(w.premise)}</textarea></div>
          <div class="fe-row" style="margin-top:8px"><label>Opening</label><textarea id="wOpening" rows="4">${feText(w.opening)}</textarea></div>
          <div class="fe-row" style="margin-top:8px"><label>Starts at</label>
            <select id="wStart">${locOpts.map((l) => `<option value="${esc(l)}"${l === w.start_location ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></div>
        </div></div>
        ${section('locations', 'Places', `${w.locations.length}`, places)}
        ${section('npcs', 'People', `${w.npcs.length}`, people)}
        ${section('rules', 'Laws the World Master enforces', `${w.rules.length} &middot; 9 min`, laws)}
        ${section('fated_events', 'Fate', `${w.fated_events.length} &middot; 7 required`, fate)}
      </div>
      <div class="forge-actions"><button class="btn btn-primary" id="forgeSave">Save world</button>
        <button class="btn btn-ghost" id="forgePlay">Save &amp; play</button></div></div>`);
}

function collectEditor() {
  const w = S.forge.world;
  w.name = $('#wName').value.trim() || w.name;
  w.tagline = $('#wTagline').value.trim();
  w.premise = $('#wPremise').value.trim();
  w.opening = $('#wOpening').value.trim();
  w.start_location = $('#wStart').value;
  const buckets = { locations: [], npcs: [], rules: [], fated_events: [] };
  $$('.fe-item').forEach((el) => {
    const kind = el.dataset.kind, i = Number(el.dataset.i);
    const src = JSON.parse(JSON.stringify((w[kind] || [])[i] || {}));
    $$('[data-f]', el).forEach((input) => {
      const path = input.dataset.f;
      let v = input.value;
      if (path === 'connects') v = v.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
      else if (path.startsWith('anchors.') && path !== 'anchors.voice') v = v.split('\n').map((s) => s.trim()).filter(Boolean);
      else if (path === 'turn') v = Number(v) || 1;
      const keys = path.split('.');
      let node = src;
      while (keys.length > 1) { const k = keys.shift(); node[k] = node[k] || {}; node = node[k]; }
      node[keys[0]] = v;
    });
    if (kind === 'rules' && !(src.check && src.check.pattern)) delete src.check;
    if (kind === 'fated_events' && !src.kills) src.kills = null;
    buckets[kind].push(src);
  });
  Object.assign(w, buckets);
  return w;
}

async function saveForge(thenPlay) {
  try {
    const saved = await api('/forge/worlds', { method: 'POST', body: {
      user_id: S.userId, world: collectEditor(), world_id: S.forge.id || null } });
    S.forge.id = saved.id;
    toast(`"${saved.name}" saved.`);
    S.worlds = await api('/worlds');
    if (thenPlay) {
      const { id } = await api('/playthroughs', { method: 'POST', body: { user_id: S.userId, world_id: saved.id } });
      closeOverlays(); await openPlaythrough(id);
    } else { closeOverlays(); showForge(); }
  } catch (e) { toast(e.message, 'err'); }
}

function showMenu() {
  if (!S.state) return;
  const I = {
    chart: '<path d="M2 13.5h12M4.5 11V6M8 11V3M11.5 11V8"/>',
    down: '<path d="M8 2v8m0 0 3-3M8 10 5 7M2.5 12.5h11"/>',
    book: '<path d="M2.5 3.5h5a2 2 0 0 1 2 2v7a1.6 1.6 0 0 0-1.6-1.6H2.5zM13.5 3.5h-5a2 2 0 0 0-2 2v7a1.6 1.6 0 0 1 1.6-1.6h5.4z"/>',
    scale: '<path d="M8 2v12M3 5h10M5 5 3 9.5h4zM11 5l-2 4.5h4z"/>',
    shield: '<path d="M8 1.5 13.5 4v4.2c0 3-2.3 5.4-5.5 6.3C4.8 13.6 2.5 11.2 2.5 8.2V4z"/>',
    flame: '<path d="M8 1.4c.6 2.6-1.4 3.4-1.4 5.2 0 1 .7 1.7 1.4 1.7s1.4-.6 1.4-1.6c1 .7 1.6 2 1.6 3.2A3 3 0 0 1 8 14.6 4.6 4.6 0 0 1 3.4 10c0-3.4 3-4.6 4.6-8.6z"/>',
    forge: '<path d="M8 1.6 14 5v6l-6 3.4L2 11V5z"/><path d="M8 8v6.4M8 8l6-3M8 8 2 5"/>',
    list: '<path d="M3 3v10M3 4.5h7M3 8h9M3 11.5h6"/>',
    gear: '<circle cx="8" cy="8" r="2.6"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4"/>',
    trash: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 9h5.6l.7-9"/>',
  };
  const item = (k, ico, t, s, danger) => `<button class="menu-item ${danger ? 'danger' : ''}" data-menu="${k}">
    <svg viewBox="0 0 16 16" class="ico">${I[ico]}</svg>
    <span class="mi-main"><span class="mi-t">${t}</span><span class="mi-s">${s}</span></span></button>`;
  const sessionsOnly = (k, ico, t, sub) => (S.sessionId ? item(k, ico, t, sub) : '');
  showModal(`${head('Story menu', `${esc(S.state.title)} &middot; turn ${S.state.turn}`)}
    <div class="modal-body"><div class="menu-list">
      ${item('timeline', 'list', 'Memory &amp; timeline', 'The append-only structured record')}
      ${item('safety', 'shield', 'Session Zero', 'Lines, veils, and the X-card')}
      ${item('card', 'book', 'Your character card', 'Name, voice, drive, flaw, anomaly')}
      ${item('streak', 'flame', 'Campaign streak', 'What you have built, with nothing nagging you')}
      ${item('cost', 'chart', 'Running cost', 'What this story has actually cost')}
      ${item('ethics', 'shield', 'Your data is yours', 'No training, no guilt hooks, export any time')}
      ${item('modes', 'scale', 'World modes', 'Canon, tone, stakes, pacing, difficulty')}
      ${item('arc', 'list', 'Timeline &amp; premise', 'Where you begin, and any alternate universe')}
      ${item('identity', 'book', 'Who they are', 'The card that keeps a character sounding like themselves')}
      ${item('boss', 'flame', 'Design a boss', 'A fight that has to be worked out, not out-damaged')}
      ${item('report', 'shield', 'Report this world', 'Worlds here are written by players')}
      ${item('explain', 'book', 'What is StoryLiver?', 'The five things that make this not a chatbot')}
      ${item('profile', 'gear', S.account ? 'Your profile' : 'Sign in', S.account
          ? 'Mana, runs, and the towns that remember you'
          : 'Keep your Mana and reputation across devices')}
      ${item('run', 'flame', `Run ${S.state.run_no || 1}`, 'Where this run stands, and what carries forward')}
      ${item('skip', 'down', 'Skip ahead', 'Training or travel, with real consequences')}
      ${sessionsOnly('party', 'shield', 'The table', 'Invite, remove, and what the world forgets')}
      ${item('settings', 'gear', 'How it looks', 'Theme, density, power mode')}
      ${item('export', 'down', 'Export everything', 'Full JSON — every layer, every memory')}
      ${item('forge', 'forge', 'World Forge', 'Build a world, or name a setting')}
      ${item('rules', 'scale', 'The rules of this world', 'What the World Master enforces')}
      ${item('switch', 'book', 'Your stories', 'Return to the threshold')}
      ${item('delete', 'trash', 'Delete this story', 'Permanent. There is no undo.', true)}
    </div></div>`);
}

async function showCost() {
  const u = await api(`/playthroughs/${S.ptId}/usage`);
  showModal(`${head('Running cost', 'Real model spend for this story, logged per call.')}
    <div class="modal-body"><div class="usage-grid">
      ${u.by_role.map((r) => `<div class="usage-row">
        <span>${esc(cap(r.role.replace('_', ' ')))} <span class="um">&middot; ${esc(r.model)}</span></span>
        <span class="um">${r.n} calls &middot; ${(r.tin + r.tout).toLocaleString()} tok</span>
        <span class="uv">$${(r.usd || 0).toFixed(5)}</span></div>`).join('') || '<div class="empty">No calls yet.</div>'}</div>
      <div class="budget ${u.within_budget ? '' : 'over'}"><b>$${u.usd_per_action.toFixed(5)}</b> per action across
        ${u.actions} actions (total $${u.total_usd.toFixed(5)}). Ceiling $${u.target_usd_per_action}.
        ${u.within_budget ? 'Comfortably inside budget.' : 'Over the ceiling.'}</div>
      <p class="fineprint">At most ${u.calls_per_turn_cap} model calls per turn, enforced in code. Every other
      system — the world tick, witnessing, reputation, stealth, combat maths, relationship deltas — is
      arithmetic and costs nothing.</p></div>`);
}

function showRules() {
  if (!S.state) return;
  showModal(`${head('The rules of this world', 'Checked before a single word is written.')}
    <div class="modal-body"><div class="mem-list">
      ${S.state.rules.map((r) => `<div class="mem"><div class="mem-meta"><span>${esc(r.id)}</span>
        ${r.check ? '<span>enforced in code</span>' : '<span>judged semantically</span>'}</div>${esc(r.text)}</div>`).join('')}
    </div><p class="fineprint">Break one and the world pushes back in character, cites the rule, and charges
    you nothing for the attempt.</p></div>`);
}

/* ==================================================================== WIRE */
function autosize(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 168) + 'px';
}

function startWhisper(kind, id, name) {
  S.whisperTo = { kind, id, name };
  $('#whisperTarget').textContent = name;
  $('#whisperBar').hidden = false;
  closeOverlays();
  $('#actionInput').placeholder = `Say it only to ${name}…`;
  $('#actionInput').focus();
}
function stopWhisper() {
  S.whisperTo = null;
  $('#whisperBar').hidden = true;
  $('#actionInput').placeholder = 'What do you do? Write it plainly — or press / for commands.';
}

function speakLast() {
  const nodes = $$('.entry-narration .prose, .entry-fate .fate-body, .entry-opening .prose');
  const last = nodes[nodes.length - 1];
  if (!last) return toast('Nothing to read yet.');
  if (!('speechSynthesis' in window)) return toast('This browser cannot read aloud.', 'warn');
  const btn = $('#voiceBtn');
  if (speechSynthesis.speaking) { speechSynthesis.cancel(); btn.classList.remove('on'); return; }
  const u = new SpeechSynthesisUtterance(last.textContent.trim());
  u.rate = 0.94; u.pitch = 0.95;
  u.onend = () => btn.classList.remove('on');
  btn.classList.add('on');
  speechSynthesis.speak(u);
}

function renderQuickRow() {
  const st = S.state; if (!st) return;
  const here = st.npcs.filter((n) => n.present && n.alive).slice(0, 2);
  const quick = [];
  for (const e of (st.exits || []).slice(0, 3)) {
    if (!e.blocked) quick.push([`Go to ${e.name}`, `I make my way to ${e.name}.`]);
  }
  for (const n of here) quick.push([`Talk to ${n.name.split(' ')[0]}`, `I ask ${n.name} what they make of all this.`]);
  quick.push(['Look around', 'I stop and take stock of the room.']);
  $('#quickRow').innerHTML = quick.slice(0, 5).map(([label, action]) =>
    `<button type="button" class="quick" data-quick="${esc(action)}">${esc(label)}</button>`).join('')
    + `<button type="button" class="quick" data-cmd-open>/ commands</button>`;
}

function wire() {
  $('#beginBtn').addEventListener('click', async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    try {
      const starter = S.worlds.find((w) => w.kind === 'starter') || { id: 'emberfall' };
      const { id } = await api('/playthroughs', { method: 'POST', body: { user_id: S.userId, world_id: starter.id } });
      await openPlaythrough(id);
    } catch (e) { toast(e.message, 'err'); b.disabled = false; }
  });

  $('#joinForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const code = $('#joinCode').value.trim().toUpperCase();
    const name = $('#joinName').value.trim();
    if (!code) return toast('Enter the room code.', 'warn');
    if (!name) return toast('Pick a name so the party knows who you are.', 'warn');
    localStorage.setItem('storyliver.name', name);
    $('#joinBtn').disabled = true;
    try {
      const r = await api('/sessions/join', { method: 'POST', body: {
        user_id: S.userId, code, name, role: $('#joinSpectate').checked ? 'spectator' : 'player' } });
      S.session = r.session;
      await openPlaythrough(r.session.playthrough_id, { sessionId: r.session.id, playerId: r.player_id, role: r.role });
      toast(r.role === 'spectator' ? 'You are watching this story.' : `You are in. Room ${code}.`);
    } catch (e) { toast(e.message, 'err'); } finally { $('#joinBtn').disabled = false; }
  });

  $('#joinCode').addEventListener('input', (e) => {
    e.target.value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 6);
  });
  $('#pricingLinkThreshold').addEventListener('click', showPricing);
  $('#ethicsLinkThreshold').addEventListener('click', () => showEthics().catch((e) => toast(e.message, 'err')));

  const ta = $('#actionInput');
  ta.addEventListener('input', () => {
    autosize(ta);
    $('#charCount').textContent = `${ta.value.length}/600`;
    const slash = ta.value.startsWith('/');
    $('#slashHints').hidden = !slash;
    if (slash) {
      const q = ta.value.slice(1).toLowerCase();
      $('#slashHints').innerHTML = COMMANDS.filter((c) => c.cmd.slice(1).startsWith(q)).slice(0, 6)
        .map((c) => `<button type="button" data-slash="${esc(c.cmd)}">${esc(c.cmd)}</button>`).join('');
    }
  });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#composer').requestSubmit(); }
    if (e.key === 'Escape' && S.whisperTo) stopWhisper();
  });
  $('#composer').addEventListener('submit', (e) => {
    e.preventDefault();
    const v = ta.value;
    if (!v.trim() || S.busy) return;
    ta.value = ''; autosize(ta); $('#charCount').textContent = '0/600'; $('#slashHints').hidden = true;
    submitAction(v);
  });
  $('#accountChip')?.addEventListener('click', () => showProfile());
  $('#premiumToggle').addEventListener('change', (e) => {
    // In a room the narrator is SHARED, so this is the host's decision for
    // everyone and it spends the host's wallet. A guest toggling it locally
    // would do nothing on the server, so say so rather than pretending.
    if (S.sessionId) {
      if (S.role !== 'host') {
        e.target.checked = !!S.state?.session?.premium_allowed;
        return toast('Deep Prose is the host’s call — the narrator is shared by the whole room.', 'warn');
      }
      return toggleRoomPremium(e.target.checked);
    }
    toast(e.target.checked ? 'Deep prose: the stronger narrator writes your next turn. 4 Mana.'
                           : 'Back to the standard narrator.');
  });
  $('#whisperCancel').addEventListener('click', stopWhisper);
  $('#voiceBtn').addEventListener('click', speakLast);

  $('#tableTabs').addEventListener('click', (e) => {
    const b = e.target.closest('[data-view]'); if (b) setView(b.dataset.view);
  });
  $('#souls').addEventListener('click', (e) => {
    const b = e.target.closest('[data-soul]'); if (b) showSoul(b.dataset.soul);
  });
  $('#pips').addEventListener('click', (e) => {
    const b = e.target.closest('[data-soul]'); if (b) showSoul(b.dataset.soul);
  });
  $('#party').addEventListener('click', (e) => {
    const b = e.target.closest('[data-whisper-player]');
    if (b) startWhisper('player', b.dataset.whisperPlayer, b.dataset.name);
  });
  $('#atlasMap').addEventListener('click', (e) => {
    const g = e.target.closest('[data-place]');
    if (g) { S.selectedPlace = g.dataset.place; renderAtlas(); }
  });
  $('#graphMap').addEventListener('click', (e) => {
    const g = e.target.closest('[data-gnode],[data-gfuture]');
    if (!g) return;
    const id = g.dataset.gnode ? Number(g.dataset.gnode) : g.dataset.gfuture;
    const n = (S.graph.nodes.find((x) => x.id === id)) || (S.graph.futures.find((x) => x.id === id));
    if (n) $('#graphDetail').innerHTML = `<span class="gd-turn">Turn ${n.turn} · ${esc(n.kind)}</span>
      <b>${esc(n.label)}</b>${esc(n.detail || '')}`;
  });

  $('#xcardBtn').addEventListener('click', doXCard);
  $('#manaBtn').addEventListener('click', showPricing);
  $('#shareBtn').addEventListener('click', () => showShare().catch((e) => toast(e.message, 'err')));
  $('#streakBtn').addEventListener('click', () => showStreak().catch((e) => toast(e.message, 'err')));
  $('#paletteBtn').addEventListener('click', () => openPalette('/'));
  $('#menuBtn').addEventListener('click', showMenu);
  $('#homeBtn').addEventListener('click', backToThreshold);
  $('#editCardBtn').addEventListener('click', () => showCard().catch((e) => toast(e.message, 'err')));
  $('#roomChip').addEventListener('click', () => showRoomCode($('#roomCode').textContent));

  $('#paletteInput').addEventListener('input', (e) => renderPalette(e.target.value));
  $('#paletteInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const first = $('.pal-item.on') || $('.pal-item');
      if (first) { closeOverlays(); runCommand(first.dataset.cmd); }
    }
  });

  $('#scrim').addEventListener('click', closeOverlays);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') return closeOverlays();
    const typing = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName);
    if (e.key === '/' && !typing && !$('#app').hidden) { e.preventDefault(); openPalette('/'); }
    if (e.key === 'Tab' && !typing && !$('#app').hidden) {
      e.preventDefault();
      const views = ['story', 'atlas', 'graph', 'known'];
      setView(views[(views.indexOf(S.view) + 1) % views.length]);
    }
  });
  document.addEventListener('click', (e) => { onGlobalClick(e).catch((err) => toast(err.message, 'err')); });

  $('#mobileTabs').addEventListener('click', (e) => {
    const b = e.target.closest('[data-pane]'); if (!b) return;
    $$('#mobileTabs button').forEach((x) => x.classList.toggle('on', x === b));
    const app = $('#app');
    app.classList.remove('pane-left', 'pane-right');
    if (b.dataset.pane !== 'read') app.classList.add(`pane-${b.dataset.pane}`);
  });
}

async function onGlobalClick(e) {
  const t = e.target;
  if (t.closest('[data-close]')) return closeOverlays();

  const quick = t.closest('[data-quick]');
  if (quick) return submitAction(quick.dataset.quick);
  if (t.closest('[data-cmd-open]')) return openPalette('/');
  const slash = t.closest('[data-slash]');
  if (slash) { $('#actionInput').value = slash.dataset.slash + ' '; $('#actionInput').focus(); return; }
  const pal = t.closest('[data-cmd]');
  if (pal) { closeOverlays(); return runCommand(pal.dataset.cmd); }

  const travel = t.closest('[data-travel]');
  if (travel) { setView('story'); return submitAction(`I make my way to ${travel.dataset.travel}.`); }

  const pref = t.closest('[data-pref]');
  if (pref) {
    const v = pref.dataset.val;
    S.prefs[pref.dataset.pref] = v === 'true' ? true : v === 'false' ? false : v;
    applyPrefs(); renderAll(); showSettings(); return;
  }
  if (t.closest('[data-show-budget]')) return showBudget();

  const openWhat = t.closest('[data-open]');
  if (openWhat) {
    const k = openWhat.dataset.open;
    if (k === 'host') return showHost();
    if (k === 'forge') return showForge();
    if (k === 'library') return showLibrary();
  }
  const load = t.closest('[data-load]');
  if (load) { closeOverlays(); return openPlaythrough(load.dataset.load); }
  const del = t.closest('[data-del]');
  if (del) {
    if (!confirm('Delete this story permanently?')) return;
    await api(`/playthroughs/${del.dataset.del}`, { method: 'DELETE' });
    t.closest('.world-row')?.remove();
    return toast('Story deleted.');
  }
  const buy = t.closest('[data-buy]');
  if (buy) {
    buy.disabled = true;
    try {
      const r = await api(`/playthroughs/${S.ptId}/purchase`, { method: 'POST', body: { pack_id: buy.dataset.buy } });
      S.state = r.state; renderAll(); closeOverlays();
      toast(`${r.granted.toLocaleString()} Mana added. It never expires.`);
    } catch (err) { toast(err.message, 'err'); buy.disabled = false; }
    return;
  }
  const wn = t.closest('[data-whisper-npc]');
  if (wn) return startWhisper('npc', wn.dataset.whisperNpc, wn.dataset.name);
  const copyCode = t.closest('[data-copy-code]');
  if (copyCode) {
    if (navigator.clipboard) await navigator.clipboard.writeText(copyCode.dataset.copyCode).catch(() => {});
    return toast('Code copied.');
  }

  /* combat */
  if (t.closest('[data-start-combat]')) return startCombat();
  const mv = t.closest('[data-move]');
  if (mv) { S.combatMove = mv.dataset.move; renderCombat(); return; }
  const ft = t.closest('[data-fighter]');
  if (ft) { S.combatTarget = ft.dataset.fighter; renderCombat(); return; }
  if (t.closest('[data-combat-submit]')) {
    if (!S.combatMove) return toast('Pick a move first.', 'warn');
    try {
      S.combat = await api(`/combat/${S.combat.id}/declare`, { method: 'POST', body: {
        player_id: S.playerId, move: S.combatMove, target: S.combatTarget, zone: null } });
      renderCombat(); toast('Declared. Nobody sees it until everyone has one.');
    } catch (err) { toast(err.message, 'err'); }
    return;
  }
  if (t.closest('[data-combat-resolve]')) {
    try {
      const out = await api(`/combat/${S.combat.id}/resolve?player=${encodeURIComponent(S.playerId)}`, { method: 'POST' });
      await loadCombat(); renderCombat();
      const { feed } = await api(`/playthroughs/${S.ptId}?player=${encodeURIComponent(S.playerId)}`);
      renderFeed(feed.filter((x) => x.id > S.lastFeedId));
      if (out.status === 'over') { toast('The fight is over.'); await refreshWorkspace(); setView('story'); }
    } catch (err) { toast(err.message, 'err'); }
    return;
  }

  /* betrayal */
  if (t.closest('[data-depart-go]')) {
    try {
      await api(`/playthroughs/${S.ptId}/depart`, { method: 'POST', body: {
        player_id: S.playerId, announced: $('#depSaid').value.trim(),
        truth: $('#depTruth').value.trim(), destination: '',
        private_turns: Number($('#depTurns').value) || 3 } });
      closeOverlays(); await refreshWorkspace(); setView('known');
      toast('You slip away. Your turns are yours alone now.');
    } catch (err) { toast(err.message, 'err'); }
    return;
  }
  if (t.closest('[data-return-go]')) {
    try {
      await api(`/playthroughs/${S.ptId}/return`, { method: 'POST', body: {
        player_id: S.playerId, account: $('#retAccount').value.trim(),
        truth: $('#retTruth').value.trim() } });
      closeOverlays(); toast('Sealed. It opens when everyone has written theirs.');
    } catch (err) { toast(err.message, 'err'); }
    return;
  }

  /* safety + cards */
  if (t.closest('[data-safety-save]')) {
    const lines = $('#safLines').value.split('\n').map((s) => s.trim()).filter(Boolean);
    const veils = $('#safVeils').value.split('\n').map((s) => s.trim()).filter(Boolean);
    S.safety = await api(`/playthroughs/${S.ptId}/safety`, { method: 'POST', body: { lines, veils } });
    closeOverlays(); return toast('Saved. Lines outrank everything, including canon.');
  }
  if (t.closest('[data-xcard]')) { closeOverlays(); return doXCard(); }
  if (t.closest('[data-card-roll]')) {
    const r = await api(`/playthroughs/${S.ptId}/card-roll?player=${encodeURIComponent(S.playerId)}`
      + `&name=${encodeURIComponent($('#cdName').value)}`);
    if (!$('#cdConcept').value) $('#cdConcept').value = r.concept;
    if (!$('#cdVoice').value) $('#cdVoice').value = r.aspects.voice;
    if (!$('#cdDrive').value) $('#cdDrive').value = r.aspects.drive;
    if (!$('#cdFlaw').value) $('#cdFlaw').value = r.aspects.flaw;
    return toast('Rolled the blanks. Free.');
  }
  const cardSave = t.closest('[data-card-save]');
  if (cardSave || t.closest('[data-card-autofill]')) {
    const body = {
      user_id: S.userId, player_id: S.playerId, name: $('#cdName').value.trim(),
      concept: $('#cdConcept').value.trim(), anomaly: $('#cdAnomaly').value.trim(),
      aspects: { voice: $('#cdVoice').value.trim(), drive: $('#cdDrive').value.trim(),
                 flaw: $('#cdFlaw').value.trim() },
      autofill: !!t.closest('[data-card-autofill]'),
      card_id: cardSave?.dataset.cardSave || null,
    };
    try {
      const card = await api(`/playthroughs/${S.ptId}/cards`, { method: 'POST', body });
      if (body.autofill) { closeOverlays(); await showCard(); return toast('Filled the blanks.'); }
      if (S.sessionId) await api(`/playthroughs/${S.ptId}/cards/${card.id}/submit`, { method: 'POST' });
      closeOverlays(); renderYouCard();
      return toast(S.sessionId ? 'Submitted for the table to approve.' : 'Saved.');
    } catch (err) { return toast(err.message, 'err'); }
  }

  /* forge */
  const tab = t.closest('[data-forge-tab]');
  if (tab) {
    $$('[data-forge-tab]').forEach((x) => x.classList.toggle('on', x === tab));
    $$('[data-forge-pane]').forEach((p) => p.classList.toggle('on', p.dataset.forgePane === tab.dataset.forgeTab));
    return;
  }
  const sug = t.closest('[data-suggest]');
  if (sug) { $('#bootSetting').value = sug.dataset.suggest; return; }
  if (t.closest('#bootGo')) return runBootstrap();
  if (t.closest('#scratchGo')) {
    const { world } = await api(`/forge/blank?name=${encodeURIComponent($('#scratchName').value.trim() || 'A new world')}`);
    return openEditor(world, null, null);
  }
  if (t.closest('#forgeSave')) return saveForge(false);
  if (t.closest('#forgePlay')) return saveForge(true);
  if (t.closest('#hostGo')) return createRoom();
  const modeOpt = t.closest('[data-mode]');
  if (modeOpt) { $$('.mode-opt').forEach((m) => m.classList.toggle('on', m === modeOpt)); return; }
  const feToggle = t.closest('[data-fe-toggle]');
  if (feToggle) { feToggle.closest('.fe-section').classList.toggle('open'); return; }
  const addItem = t.closest('[data-add]');
  if (addItem) {
    const kind = addItem.dataset.add, w = collectEditor();
    const hub = w.locations[0]?.id || null;
    const lastFate = w.fated_events[w.fated_events.length - 1];
    w[kind].push({
      locations: { id: `place_${w.locations.length}`, name: 'A new place', kind: 'place', desc: '', connects: hub ? [hub] : [] },
      npcs: { id: `npc_${w.npcs.length}`, name: 'Someone', role: 'villager',
              anchors: { voice: '', constraints: [], goals: [], taboos: [] }, start_location: hub,
              seed_memories: [], initial_relationship: { affinity: 0, trust: 0, fear: 0, obligation: 0 } },
      rules: { id: `R${w.rules.length + 1}_new`, text: '' },
      fated_events: { id: `F${w.fated_events.length + 1}_new`, turn: (lastFate ? lastFate.turn : 0) + 7,
                      title: 'Something happens', desc: '', location: hub, kills: null },
    }[kind]);
    return openEditor(w, S.forge.id, null);
  }
  const delItem = t.closest('[data-del-item]');
  if (delItem) {
    const item = delItem.closest('.fe-item');
    const w = collectEditor();
    w[item.dataset.kind].splice(Number(item.dataset.i), 1);
    return openEditor(w, S.forge.id, null);
  }
  const playWorld = t.closest('[data-play-world]');
  if (playWorld) {
    const { id } = await api('/playthroughs', { method: 'POST', body: { user_id: S.userId, world_id: playWorld.dataset.playWorld } });
    closeOverlays(); return openPlaythrough(id);
  }
  const editWorld = t.closest('[data-edit-world]');
  if (editWorld) {
    const d = await api(`/forge/worlds/${editWorld.dataset.editWorld}`);
    return openEditor(d.world, d.id, d.personal_only
      ? 'Inspired-by personal world: kept private, never published, not affiliated with any rights holder.' : null);
  }
  const expWorld = t.closest('[data-export-world]');
  if (expWorld) {
    window.open(`/api/forge/worlds/${expWorld.dataset.exportWorld}/export?user_id=${encodeURIComponent(S.userId)}`, '_blank');
    return toast('World exported. It is yours.');
  }
  const delWorld = t.closest('[data-delete-world]');
  if (delWorld) {
    if (!confirm('Delete this world? Stories already running on it keep playing.')) return;
    await api(`/forge/worlds/${delWorld.dataset.deleteWorld}`, { method: 'DELETE' });
    closeOverlays(); return showForge();
  }

  /* share */
  const share = t.closest('[data-share]');
  if (share) {
    const text = $('#shareText')?.value || '';
    const src = `${location.origin}/api/playthroughs/${S.ptId}/card.svg?user_id=${encodeURIComponent(S.userId)}&download=true`;
    if (share.dataset.share === 'copy') {
      if (navigator.clipboard) await navigator.clipboard.writeText(text).catch(() => {});
      return toast('Post copied.');
    }
    if (share.dataset.share === 'image') { window.open(src, '_blank'); return; }
    if (navigator.share) { try { await navigator.share({ text }); } catch { /* dismissed */ } return; }
    if (navigator.clipboard) await navigator.clipboard.writeText(text).catch(() => {});
    return toast('Sharing unavailable here — the post is on your clipboard.');
  }
  if (t.closest('[data-share-streak]')) { closeOverlays(); return showShare(); }

  const menu = t.closest('[data-menu]');
  if (!menu) return;
  const k = menu.dataset.menu;
  const routes = {
    timeline: showTimeline, cost: showCost, rules: showRules, ethics: showEthics,
    streak: showStreak, forge: showForge, safety: showSafety, card: showCard, settings: showSettings,
    modes: showModes, run: showRun, skip: showFastForward, party: showParty, report: showReport,
    arc: showTimelinePicker, identity: openIdentity, boss: showBossEditor,
    explain: () => showExplainer(true), profile: showProfile,
  };
  if (routes[k]) return routes[k]();
  if (k === 'export') {
    window.open(`/api/playthroughs/${S.ptId}/export?user_id=${encodeURIComponent(S.userId)}`, '_blank');
    closeOverlays(); return toast('Exported. Your world state is yours.');
  }
  if (k === 'switch') { closeOverlays(); return backToThreshold(); }
  if (k === 'delete') {
    if (!confirm('Delete this story permanently? There is no undo.')) return;
    await api(`/playthroughs/${S.ptId}`, { method: 'DELETE' });
    closeOverlays(); localStorage.removeItem('storyliver.last'); location.reload();
  }
}

async function backToThreshold() {
  closeOverlays();
  if (S.ws) { const w = S.ws; S.ws = null; S.sessionId = null; try { w.close(); } catch { /* gone */ } }
  S.session = null; S.combat = null;
  $('#app').hidden = true; $('#threshold').hidden = false;
  lastRenderedTurn = -1;
  localStorage.removeItem('storyliver.last');
  await initThreshold();
}

/* ==================================================================== BOOT */
(async function boot() {
  S.userId = userId();
  // An authenticated account replaces the generated client id everywhere, so
  // Mana, runs and a town's memory attach to the person, not the browser.
  await loadAccount();
  try { Object.assign(S.prefs, JSON.parse(localStorage.getItem('storyliver.prefs') || '{}')); } catch { /* defaults */ }
  document.documentElement.dataset.theme = S.prefs.theme;
  document.documentElement.dataset.density = S.prefs.density;
  wire();

  const url = new URL(location.href);
  const codeParam = (url.searchParams.get('room') || url.hash.replace('#', '')).toUpperCase();
  try {
    await initThreshold();
    if (codeParam && /^[A-Z0-9]{6}$/.test(codeParam)) {
      $('#joinCode').value = codeParam;
      $('#joinName').focus();
      return toast(`Room ${codeParam} — add your name and you are in.`);
    }
    const raw = localStorage.getItem('storyliver.last');
    if (raw) {
      // A story we can no longer open is not an error worth alarming anyone
      // about - it just means the threshold is the right place to land.
      try { const last = JSON.parse(raw); await openPlaythrough(last.id, last); }
      catch { localStorage.removeItem('storyliver.last'); }
    }
  } catch (e) { toast(`Could not reach the world: ${e.message}`, 'err'); }
})();


/* ===========================================================================
   World modes — the dials that re-tune the world.
   Progressive disclosure: this is one panel of chips, not a settings tree.
   ========================================================================= */

async function showModes() {
  if (!S.modeCatalogue) {
    try { S.modeCatalogue = await api('/modes/catalogue'); }
    catch { toast('Could not load modes.', 'err'); return; }
  }
  let current;
  try { current = await api(`/playthroughs/${S.ptId}/modes`); }
  catch { toast('Could not read this world’s modes.', 'err'); return; }
  S.modes = current.modes;

  const axes = S.modeCatalogue.axes;
  const order = ['canon', 'tone', 'stakes', 'pacing', 'combat', 'difficulty'];
  const label = {
    canon: 'Canon', tone: 'Tone', stakes: 'Stakes',
    pacing: 'Pacing', combat: 'Combat', difficulty: 'Difficulty',
  };

  showModal(`${head('World modes', 'These compose. Strict + Humor + Hardcore is a real, coherent world.')}
    <div class="modal-body">
      ${order.filter((a) => axes[a]).map((axis) => `
        <div class="mode-axis">
          <div class="mode-axis-name">${label[axis]}</div>
          <div class="mode-opts">
            ${axes[axis].options.map((o) => `
              <button class="mode-chip ${S.modes[axis] === o.id ? 'on' : ''}"
                      data-axis="${axis}" data-mode="${esc(o.id)}"
                      title="${esc(o.blurb || '')}">
                <b>${esc(o.name)}</b><small>${esc(o.blurb || '')}</small>
              </button>`).join('')}
          </div>
        </div>`).join('')}
      <div class="mode-axis">
        <div class="mode-axis-name">Language / voice</div>
        <input class="input" id="mode-lang" placeholder="e.g. Japanese-flavoured, Victorian, plainspoken"
               value="${esc(S.modes.language || '')}" />
      </div>
      <p class="fineprint">Cozy always outranks Hardcore — if a table asked not to lose anyone,
        that promise wins. Modes change what the world <em>does</em>; they never change what a turn costs.</p>
    </div>`);
}

async function setMode(axis, value) {
  try {
    const r = await api(`/playthroughs/${S.ptId}/modes`, {
      method: 'POST', body: { modes: { [axis]: value } },
    });
    S.modes = r.modes;
    toast(`${axis}: ${r.labels[axis]}`);
    showModes();
    renderAll();
  } catch (e) { toast(e.message, 'err'); }
}

/* ===========================================================================
   The run banner — roguelike framing. A run has a shape; a chat thread doesn't.
   ========================================================================= */

async function showRun() {
  let r;
  try { r = await api(`/playthroughs/${S.ptId}/run`); }
  catch { toast('Could not read the run.', 'err'); return; }

  showModal(`${head(`Run ${r.run_no}`, r.permadeath
      ? 'Hardcore. If you fall, the run ends — and a fraction carries forward.'
      : 'Death has resolutions here. You will not be dead-ended.')}
    <div class="modal-body">
      <div class="run-stats">
        <div class="run-stat"><b>${r.run_no}</b><small>this run</small></div>
        <div class="run-stat"><b>${r.runs_completed}</b><small>runs finished</small></div>
        <div class="run-stat"><b>${r.deepest_turn}</b><small>deepest turn</small></div>
        <div class="run-stat"><b>${r.lore_known}</b><small>facts carried</small></div>
      </div>
      ${r.last_echo ? `<p class="run-echo">${esc(r.last_echo)}</p>` : ''}
      ${r.unlocks?.length ? `<div class="run-unlocks">${r.unlocks.map((u) =>
        `<span class="pill">${esc(u)}</span>`).join('')}</div>` : ''}
      <p class="fineprint">What carries between runs is <b>knowledge</b>, never power —
        lore you uncovered and places you found. Run five starts less lost, not stronger.
        That is the difference between a roguelike and a save file.</p>
      <div class="row" style="margin-top:14px">
        <button class="btn btn-ghost" data-run-end="retired">Retire this run</button>
      </div>
    </div>`);
}

async function endRun(reason) {
  if (!confirm(`End this run (${reason})? Meta-progression is settled now.`)) return;
  try {
    const r = await api(`/playthroughs/${S.ptId}/run/end`, {
      method: 'POST', body: { reason },
    });
    closeOverlays();
    toast(`Run ended. ${r.carried.lore_total} facts carried forward.`);
    renderAll();
  } catch (e) { toast(e.message, 'err'); }
}

/* ===========================================================================
   Death — offered as choices, never as a wall.
   ========================================================================= */

function showDeathResolutions(payload) {
  const rs = payload.resolutions || [];
  showModal(`${head(payload.knockout ? 'Down, not dead' : `${esc(payload.who)} has died`,
      payload.knockout
        ? `This world does not take people. ${esc(payload.outcome || '')}.`
        : 'Death changed the world. Now choose what it changes for you.')}
    <div class="modal-body">
      ${payload.consequences ? `<div class="death-fallout">
        ${payload.consequences.vacuum ? `<div><b>${esc(payload.consequences.vacuum)}</b> stands empty.</div>` : ''}
        ${payload.consequences.mourned_by?.length
          ? `<div>${payload.consequences.mourned_by.length} character(s) are grieving.</div>` : ''}
        ${payload.consequences.killer_cost?.length
          ? `<div>The killer has lost standing with ${payload.consequences.killer_cost.length}.</div>` : ''}
      </div>` : ''}
      <div class="death-grid">${rs.map((r) => `
        <button class="death-opt ${payload.preset === r.id ? 'preset' : ''}"
                data-death="${esc(r.id)}" data-who="${esc(payload.who)}">
          <b>${esc(r.name)}</b><small>${esc(r.blurb)}</small>
          <span class="death-caps">${r.can_act ? 'can act' : 'no agency'} ·
            ${r.can_whisper ? 'can whisper' : 'silent'}</span>
        </button>`).join('')}</div>
      ${payload.preset ? '<p class="fineprint">Highlighted is the choice you set in advance on your card.</p>' : ''}
    </div>`);
}

async function resolveDeath(choice, who) {
  try {
    const r = await api(`/playthroughs/${S.ptId}/death/resolve`, {
      method: 'POST', body: { choice, who },
    });
    S.state = r.state; closeOverlays(); renderAll();
    toast(r.note || r.name);
    if (r.needs_new_card) showCards?.();
  } catch (e) { toast(e.message, 'err'); }
}

/* ===========================================================================
   Fast-forward — a skip that costs something, previewed before it commits.
   ========================================================================= */

async function showFastForward() {
  if (!S.ffCatalogue) {
    try { S.ffCatalogue = await api('/fastforward/catalogue'); }
    catch { toast('Could not load skip kinds.', 'err'); return; }
  }
  const present = (S.state?.npcs || []).filter((n) => n.present && n.alive);
  showModal(`${head('Skip ahead', 'Time passes — and it applies the same consequences playing it would.')}
    <div class="modal-body">
      <div class="ff-kinds">${S.ffCatalogue.kinds.map((k, i) => `
        <button class="ff-kind ${i === 0 ? 'on' : ''}" data-ff-kind="${esc(k.id)}">
          <b>${esc(k.name)}</b><small>${esc(k.blurb)}</small></button>`).join('')}</div>
      <label class="field"><span>How long</span>
        <input class="input" type="number" id="ff-turns" min="1"
               max="${S.ffCatalogue.max_turns}" value="8" /></label>
      ${present.length ? `<div class="field"><span>Alongside</span>
        <div class="ff-with">${present.map((n) => `
          <button class="chip" data-ff-with="${esc(n.id)}">${esc(n.name)}</button>`).join('')}</div>
        </div>` : ''}
      <div id="ff-preview" class="ff-preview"></div>
      <div class="row" style="margin-top:14px">
        <button class="btn btn-ghost" id="ff-plan">Preview</button>
        <button class="btn btn-primary" id="ff-go">Skip ahead</button>
      </div>
      <p class="fineprint">Relationships move. Skills improve. The world ticks, rumours arrive,
        hunters get closer. The narrator then reports what already happened — it never decides it.</p>
    </div>`);
  S.ffKind = S.ffCatalogue.kinds[0].id; S.ffWith = [];
}

function ffBody() {
  return {
    kind: S.ffKind || 'training',
    turns: parseInt(document.getElementById('ff-turns')?.value || '8', 10),
    with_whom: S.ffWith || [],
  };
}

async function ffPreview() {
  try {
    const p = await api(`/playthroughs/${S.ptId}/fastforward/plan`, {
      method: 'POST', body: ffBody(),
    });
    const el = document.getElementById('ff-preview');
    if (el) el.innerHTML = `<div class="ff-plan"><b>${p.turns} turns of ${esc(p.name)}</b>
      <div>+${p.skill_gain} skill${p.with?.length ? ` · alongside ${p.with.length}` : ''}</div>
      <small>${esc(p.note)}</small></div>`;
  } catch (e) { toast(e.message, 'err'); }
}

async function ffApply() {
  try {
    const r = await api(`/playthroughs/${S.ptId}/fastforward`, {
      method: 'POST', body: ffBody(),
    });
    S.state = r.state; closeOverlays(); renderAll();
    const moved = (r.changes.relationships || [])
      .filter((x) => Math.abs(x.trust) >= 1).length;
    toast(`${r.changes.to_turn - r.changes.from_turn} turns passed · +${r.changes.skill.gain} skill${moved ? ` · ${moved} relationship(s) shifted` : ''}`);
  } catch (e) { toast(e.message, 'err'); }
}

/* ===========================================================================
   Party lifecycle — invite needs everyone, kick needs everyone else.
   ========================================================================= */

async function showParty() {
  if (!S.sessionId) { toast('Solo play has no table to manage.'); return; }
  let motions = { motions: [] };
  try { motions = await api(`/sessions/${S.sessionId}/party/motions`); } catch {}
  const players = S.session?.players || [];

  showModal(`${head('The table', 'Inviting needs everyone. Removing needs everyone else.')}
    <div class="modal-body">
      <div class="party-list">${players.map((p) => `
        <div class="party-row">
          <div class="seat-avatar">${p.avatar_url
            ? `<img src="${esc(p.avatar_url)}" alt="" />`
            : `<span>${esc((p.name || '?')[0])}</span>`}</div>
          <div class="party-who"><b>${esc(p.name)}</b>
            <small>${p.is_host ? 'host · holds the wallet' : p.role}${
              p.life_state === 'dead' ? ` · ${esc(p.resolution || 'gone')}` : ''}</small></div>
          ${p.is_host || p.player_id === S.playerId ? ''
            : `<button class="btn btn-ghost sm" data-kick="${esc(p.player_id)}">Propose removal</button>`}
        </div>`).join('')}</div>

      ${motions.motions.length ? `<div class="motions">${motions.motions.map((m) => `
        <div class="motion">
          <div><b>${m.kind === 'kick' ? 'Remove' : 'Invite'} ${esc(m.target_name || m.target_id)}</b>
            <small>${Object.keys(m.approvals).length} of ${m.needed.length} agreed</small></div>
          ${m.needed.includes(S.playerId) && !(S.playerId in m.approvals) ? `
            <div class="row">
              <button class="btn btn-ghost sm" data-vote="${m.id}" data-approve="0">Object</button>
              <button class="btn btn-primary sm" data-vote="${m.id}" data-approve="1">Agree</button>
            </div>` : '<span class="pill">voted</span>'}
        </div>`).join('')}</div>` : ''}

      <div id="party-archives"></div>

      <p class="fineprint">When someone leaves, every memory the world holds of them is
        erased from every character — except what other players actually witnessed, which stays,
        because that is their memory too. Nothing is destroyed: a re-invite restores it all.</p>
    </div>`);
  loadArchives();
}

async function loadArchives() {
  if (!S.sessionId) return;
  let out = { archives: [] };
  try { out = await api(`/sessions/${S.sessionId}/party/archives`); } catch { return; }
  const el = document.getElementById('party-archives');
  if (!el || !out.archives.length) return;
  el.innerHTML = `<div class="pf-head" style="margin-top:14px">People who left</div>
    ${out.archives.map((a) => `<div class="party-row">
      <div class="party-who"><b>${esc(a.player_id)}</b>
        <small>${esc(a.cease)} cease · ${esc((a.created_at || '').slice(0, 10))}</small></div>
      <button class="btn btn-ghost sm" data-restore="${a.id}">Bring them back</button>
    </div>`).join('')}`;
}

async function restorePlayer(archiveId) {
  if (!confirm('Restore this player and everything the world remembered about them?')) return;
  try {
    const r = await api(`/sessions/${S.sessionId}/party/archives/${archiveId}/restore`,
                        { method: 'POST' });
    toast(`${r.player_id} is back, and so is their history.`);
    showParty();
  } catch (e) { toast(e.message, 'err'); }
}

async function proposeKick(targetId) {
  const target = (S.session?.players || []).find((p) => p.player_id === targetId);
  try {
    await api(`/sessions/${S.sessionId}/party/propose`, {
      method: 'POST',
      body: { kind: 'kick', proposed_by: S.playerId, target_id: targetId,
              target_name: target?.name || '' },
    });
    toast('Proposed. Every other player must agree.');
    showParty();
  } catch (e) { toast(e.message, 'err'); }
}

async function voteMotion(motionId, approve) {
  try {
    const m = await api(`/sessions/${S.sessionId}/party/motions/${motionId}/vote`, {
      method: 'POST', body: { player_id: S.playerId, approve },
    });
    toast(m.status === 'passed' ? 'Carried.'
      : m.status === 'rejected' ? 'Blocked — one objection is enough.' : 'Recorded.');
    showParty();
  } catch (e) { toast(e.message, 'err'); }
}

/* ===========================================================================
   Report a world — the takedown route, reachable from inside the product.
   ========================================================================= */

async function showReport() {
  let p;
  try { p = await api('/policy'); } catch { toast('Could not load policy.', 'err'); return; }
  showModal(`${head('Report this world', 'Worlds here are written by players. Tell us if one is wrong.')}
    <div class="modal-body">
      <div class="report-reasons">${Object.entries(p.reasons).map(([id, blurb], i) => `
        <button class="report-reason ${i === 0 ? 'on' : ''}" data-reason="${esc(id)}">
          <b>${esc(id.replace(/_/g, ' '))}</b><small>${esc(blurb)}</small></button>`).join('')}</div>
      <label class="field"><span>Anything else we should know</span>
        <textarea class="input" id="report-detail" rows="3"></textarea></label>
      <div class="row" style="margin-top:12px">
        <button class="btn btn-primary" id="report-send">Send report</button>
      </div>
      <p class="fineprint">${esc(p.ownership)} ${esc(p.official)} ${esc(p.monetisation)}</p>
    </div>`);
  S.reportReason = Object.keys(p.reasons)[0];
}

async function sendReport() {
  try {
    const r = await api('/reports', {
      method: 'POST',
      body: { reason: S.reportReason, world_id: S.state?.world?.id || '',
              playthrough_id: S.ptId,
              detail: document.getElementById('report-detail')?.value || '' },
    });
    closeOverlays();
    toast(r.note);
  } catch (e) { toast(e.message, 'err'); }
}


/* --- delegation for everything added above ------------------------------- */
document.addEventListener('click', async (ev) => {
  const t = ev.target;
  const pick = (sel) => t.closest(sel);

  const mode = pick('[data-axis][data-mode]');
  if (mode) return setMode(mode.dataset.axis, mode.dataset.mode);

  const runEnd = pick('[data-run-end]');
  if (runEnd) return endRun(runEnd.dataset.runEnd);

  const dth = pick('[data-death]');
  if (dth) return resolveDeath(dth.dataset.death, dth.dataset.who);

  const ffk = pick('[data-ff-kind]');
  if (ffk) {
    S.ffKind = ffk.dataset.ffKind;
    document.querySelectorAll('[data-ff-kind]').forEach((b) => b.classList.toggle('on', b === ffk));
    return;
  }
  const ffw = pick('[data-ff-with]');
  if (ffw) {
    const id = ffw.dataset.ffWith;
    S.ffWith = S.ffWith || [];
    const at = S.ffWith.indexOf(id);
    if (at >= 0) S.ffWith.splice(at, 1); else S.ffWith.push(id);
    ffw.classList.toggle('on', at < 0);
    return;
  }
  if (t.closest('#ff-plan')) return ffPreview();
  if (t.closest('#ff-go')) return ffApply();

  const kick = pick('[data-kick]');
  if (kick) return proposeKick(kick.dataset.kick);

  const vote = pick('[data-vote]');
  if (vote) return voteMotion(vote.dataset.vote, vote.dataset.approve === '1');

  const restore = pick('[data-restore]');
  if (restore) return restorePlayer(restore.dataset.restore);

  const reason = pick('[data-reason]');
  if (reason) {
    S.reportReason = reason.dataset.reason;
    document.querySelectorAll('[data-reason]').forEach((b) => b.classList.toggle('on', b === reason));
    return;
  }
  if (t.closest('#report-send')) return sendReport();
});

document.addEventListener('input', (ev) => {
  if (ev.target.id === 'mode-lang') {
    clearTimeout(S._langT);
    S._langT = setTimeout(() => setMode('language', ev.target.value), 700);
  }
});


/* ===========================================================================
   FIRST RUN — the tutorialised opening.

   Research on RPG-chat retention is consistent about the failure mode: a blank
   input box after a wall of setup text. The player does not know what they are
   allowed to type, so they type nothing and leave. So the first session opens
   with three decisions that TEACH by being made — tone, stakes, and where the
   table's lines are — and then hands over pre-filled actions rather than a
   cursor. Under a minute, and every control it touches is one the player will
   use again later in the same place.
   ========================================================================= */

const ONBOARD_KEY = 'storyliver.onboarded';

function needsOnboarding() {
  try { return !localStorage.getItem(ONBOARD_KEY); } catch { return false; }
}

function markOnboarded() {
  try { localStorage.setItem(ONBOARD_KEY, '1'); } catch { /* private mode */ }
}

const ONBOARD_STEPS = [
  {
    key: 'tone',
    title: 'What kind of story is this?',
    sub: 'This changes how the world talks to you. You can change it any time.',
    axis: 'tone',
    opts: [
      ['neutral', 'As written', 'The world in its own voice.'],
      ['dark', 'Dark', 'Grim, close, and heavier when it costs you.'],
      ['humor', 'Funny', 'The world is absurd and knows it.'],
      ['chill', 'Cozy', 'Low threat. Nobody you care about dies.'],
    ],
  },
  {
    key: 'stakes',
    title: 'How much should losing hurt?',
    sub: 'Cozy always wins over Hardcore — if you chose Cozy above, nobody dies regardless.',
    axis: 'stakes',
    opts: [
      ['normal', 'Death has answers', 'If you fall, you choose what happens next.'],
      ['hardcore', 'Hardcore', 'Permanent. What you learned carries to the next run.'],
    ],
  },
];

async function startOnboarding() {
  S.onboardStep = 0;
  S.onboardPicks = {};
  renderOnboardStep();
}

function renderOnboardStep() {
  const i = S.onboardStep;
  if (i >= ONBOARD_STEPS.length) return renderOnboardLines();
  const step = ONBOARD_STEPS[i];
  showModal(`${head(step.title, step.sub)}
    <div class="modal-body">
      <div class="onb-dots">${ONBOARD_STEPS.map((_, n) =>
        `<span class="${n === i ? 'on' : ''}"></span>`).join('')}<span></span></div>
      <div class="onb-opts">${step.opts.map(([id, name, blurb]) => `
        <button class="onb-opt" data-onb="${esc(id)}" data-onb-axis="${esc(step.axis)}">
          <b>${esc(name)}</b><small>${esc(blurb)}</small></button>`).join('')}</div>
      <button class="btn btn-ghost onb-skip" data-onb-skip>Skip — use the defaults</button>
    </div>`);
}

function renderOnboardLines() {
  showModal(`${head('Anything this story should never contain?',
      'Your table’s lines. The world will refuse them outright — not soften them.')}
    <div class="modal-body">
      <div class="onb-dots">${ONBOARD_STEPS.map(() => '<span></span>').join('')}<span class="on"></span></div>
      <div class="onb-lines">${['Harm to children', 'Sexual content', 'Animal cruelty',
        'Suicide &amp; self-harm', 'Graphic torture'].map((l) => `
        <button class="chip" data-line="${esc(l.replace('&amp;', '&'))}">${l}</button>`).join('')}</div>
      <label class="field" style="margin-top:12px"><span>Anything else</span>
        <input class="input" id="onb-line-custom" placeholder="Type a line and press Enter" /></label>
      <p class="fineprint">Anyone at the table can add a line or play the X-card at any moment,
        with no reason required. Lines outrank every other rule in the engine, including canon.</p>
      <div class="row" style="margin-top:14px">
        <button class="btn btn-primary" data-onb-done>Begin</button>
      </div>
    </div>`);
  S.onboardLines = S.onboardLines || [];
}

async function finishOnboarding() {
  markOnboarded();
  try {
    if (Object.keys(S.onboardPicks || {}).length) {
      await api(`/playthroughs/${S.ptId}/modes`, { method: 'POST', body: { modes: S.onboardPicks } });
    }
    if ((S.onboardLines || []).length) {
      await api(`/playthroughs/${S.ptId}/safety`, { method: 'POST', body: { lines: S.onboardLines } });
    }
    // This endpoint returns {state, feed} - taking the wrapper instead of the
    // state is what made renderAll() throw on a fresh onboarded world.
    const fresh = await api(`/playthroughs/${S.ptId}?player=${encodeURIComponent(S.playerId)}`);
    if (fresh?.state) S.state = fresh.state;
  } catch (e) { toast(e.message, 'err'); }
  closeOverlays();
  renderAll();
  toast('Your world is set. Change any of it from the story menu.');
}

/* ===========================================================================
   ENDING — a story that reaches its last fated event gets a close, not silence.
   ========================================================================= */

function showEnding(ending) {
  if (!ending) return;
  const carried = ending.run?.carried || {};
  showModal(`${head('The story closes', esc(ending.reason === 'victory'
      ? 'Fate has run out. What happened, happened.'
      : 'This run is over.'))}
    <div class="modal-body">
      <div class="run-stats">
        <div class="run-stat"><b>${S.state?.turn ?? 0}</b><small>turns lived</small></div>
        <div class="run-stat"><b>${carried.lore_total ?? 0}</b><small>facts carried</small></div>
        <div class="run-stat"><b>${carried.runs_completed ?? 1}</b><small>runs finished</small></div>
        <div class="run-stat"><b>${carried.deepest_turn ?? 0}</b><small>deepest turn</small></div>
      </div>
      ${carried.echo ? `<p class="run-echo">${esc(carried.echo)}</p>` : ''}
      ${carried.unlocks?.length ? `<div class="run-unlocks">${carried.unlocks.map((u) =>
        `<span class="pill">${esc(u)}</span>`).join('')}</div>` : ''}
      <p class="fineprint">What you learned about this world stays with you. Start again and
        you begin knowing it — less lost, not stronger.</p>
      <div class="row" style="margin-top:14px">
        <button class="btn btn-ghost" data-menu="export">Export this story</button>
        <button class="btn btn-primary" data-new-run>Begin a new run</button>
      </div>
    </div>`);
}

async function beginNewRun() {
  try {
    const w = S.state?.world?.id || 'emberfall';
    const { id } = await api('/playthroughs', { method: 'POST', body: { user_id: S.userId, world_id: w } });
    closeOverlays();
    await openPlaythrough(id);
    toast('A new run. The world remembers what you learned.');
  } catch (e) { toast(e.message, 'err'); }
}

/* ===========================================================================
   Host controls — Deep Prose is a room decision, so it lives with the room.
   ========================================================================= */

async function toggleRoomPremium(on) {
  try {
    const r = await api(`/sessions/${S.sessionId}/premium`, {
      method: 'POST', body: { allowed: on },
    });
    if (S.state?.session) S.state.session.premium_allowed = r.premium_allowed;
    toast(r.premium_allowed
      ? 'Deep Prose is on for the whole room. It costs 4 Mana a turn, from your wallet.'
      : 'Deep Prose off. Everyone is on the standard narrator.');
    renderAll();
  } catch (e) { toast(e.message, 'err'); }
}

/* ===========================================================================
   Character identity — the card that holds a persona steady.
   ========================================================================= */

const IDENTITY_FIELDS = [
  ['voice', 'Voice', 'How they speak — rhythm, formality, dialect', 'text'],
  ['catchphrases', 'Signature lines', 'One per line', 'list'],
  ['mannerisms', 'Mannerisms', 'Physical tics, gestures', 'list'],
  ['values', 'Values', 'What they would never compromise', 'list'],
  ['flaws', 'Flaws', 'What trips them up', 'list'],
  ['goals', 'Wants', 'What they are chasing', 'list'],
  ['taboos', 'Never', 'What they will never do or say', 'list'],
  ['secrets', 'Secrets', 'Known, never volunteered', 'list'],
  ['backstory', 'Behind them', 'The short version', 'text'],
];

async function showIdentity(cardId) {
  if (!cardId) { toast('Create a character card first.'); return showCard?.(); }
  let beats = S.personaBeats;
  if (!beats) {
    try { beats = S.personaBeats = (await api('/persona/beats')).beats; }
    catch { beats = []; }
  }
  const id = S.identityDraft || {};
  showModal(`${head('Who they are', 'Re-sent to the model on every single call — never summarised, so it cannot drift.')}
    <div class="modal-body">
      <label class="field"><span>Their art (you upload it — nothing here is generated)</span>
        <div class="row">
          <div class="seat-avatar lg" id="idn-avatar">${S.identityAvatar
            ? `<img src="${esc(S.identityAvatar)}" alt="" />` : '<span>+</span>'}</div>
          <input type="file" id="idn-file" accept="image/png,image/jpeg,image/gif,image/webp" hidden />
          <button class="btn btn-ghost" id="idn-pick">Choose an image</button>
        </div></label>

      ${IDENTITY_FIELDS.map(([k, label, hint, kind]) => `
        <label class="field"><span>${label} <small class="hint">${hint}</small></span>
          ${kind === 'list'
            ? `<textarea class="input" data-idn="${k}" rows="2">${esc((id[k] || []).join('\n'))}</textarea>`
            : `<input class="input" data-idn="${k}" value="${esc(id[k] || '')}" />`}
        </label>`).join('')}

      <div class="field"><span>Canon lines <small class="hint">delivered only when that
        moment actually arrives — not every turn</small></span>
        <div id="idn-lines">${(id.famous_lines || []).map((l, i) => `
          <div class="idn-line" data-i="${i}">
            <select class="input" data-idn-beat="${i}">
              <option value="">any moment</option>
              ${beats.map((b) => `<option value="${esc(b)}"${l.beat === b ? ' selected' : ''}>${esc(b.replace(/_/g, ' '))}</option>`).join('')}
            </select>
            <input class="input" data-idn-line="${i}" value="${esc(l.line || '')}" />
          </div>`).join('')}</div>
        <button class="btn btn-ghost sm" id="idn-add-line">Add a canon line</button>
      </div>

      <label class="field"><span>If this character dies <small class="hint">decided now, so
        nobody has to choose in the ten seconds after losing them</small></span>
        <select class="input" id="idn-ondeath">
          <option value="">ask me then</option>
          <option value="ghost">Spectator</option><option value="heir">Heir</option>
          <option value="spirit">Spirit guide</option><option value="legacy">Legacy</option>
        </select></label>

      <div id="idn-meter" class="idn-meter"></div>
      <div class="row" style="margin-top:14px">
        <button class="btn btn-primary" id="idn-save" data-card="${esc(cardId)}">Save</button>
      </div>
      <p class="fineprint">Every field you fill is one more thing that holds them steady when
        the story runs long. Empty fields are where a character starts sounding generic.</p>
    </div>`);
  updateIdentityMeter();
}

function collectIdentity() {
  const out = { famous_lines: [] };
  document.querySelectorAll('[data-idn]').forEach((el) => {
    const k = el.dataset.idn;
    const field = IDENTITY_FIELDS.find((f) => f[0] === k);
    out[k] = field && field[3] === 'list'
      ? el.value.split('\n').map((x) => x.trim()).filter(Boolean)
      : el.value.trim();
  });
  document.querySelectorAll('[data-idn-line]').forEach((el) => {
    const i = el.dataset.idnLine;
    const beat = document.querySelector(`[data-idn-beat="${i}"]`)?.value || '';
    if (el.value.trim()) out.famous_lines.push({ beat, line: el.value.trim() });
  });
  return out;
}

function updateIdentityMeter() {
  const d = collectIdentity();
  const total = IDENTITY_FIELDS.length + 1;
  const filled = IDENTITY_FIELDS.filter(([k]) => (Array.isArray(d[k]) ? d[k].length : d[k])).length
    + (d.famous_lines.length ? 1 : 0);
  const pct = Math.round((filled / total) * 100);
  const el = document.getElementById('idn-meter');
  if (el) el.innerHTML = `<div class="idn-bar"><i style="width:${pct}%"></i></div>
    <small>${filled} of ${total} filled — ${pct < 40 ? 'still fairly generic'
      : pct < 75 ? 'taking shape' : 'this will hold under a long story'}</small>`;
}

async function saveIdentity(cardId) {
  try {
    const r = await api(`/cards/${cardId}/identity`, {
      method: 'POST',
      body: { identity: collectIdentity(), avatar_url: S.identityAvatar || '',
              on_death: document.getElementById('idn-ondeath')?.value || '' },
    });
    S.identityDraft = r.identity;
    closeOverlays();
    toast('Saved. They will sound like themselves.');
  } catch (e) { toast(e.message, 'err'); }
}

async function uploadImage(file) {
  if (!file) return null;
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(`/api/upload?user_id=${encodeURIComponent(S.userId)}`,
                        { method: 'POST', body: fd });
  if (!r.ok) { toast((await r.json().catch(() => ({}))).detail || 'Upload failed.', 'err'); return null; }
  return (await r.json()).url;
}

/* ===========================================================================
   Timeline entry point + AU premise.
   ========================================================================= */

async function showTimelinePicker() {
  const worldId = S.state?.world?.id;
  let data = { arcs: [] };
  try { data = await api(`/worlds/${worldId}/timeline`); } catch {}
  let pos = {};
  try { pos = await api(`/playthroughs/${S.ptId}/timeline`); } catch {}

  showModal(`${head('Where does your story start?', 'Canon is a starting position, not a cage.')}
    <div class="modal-body">
      ${data.arcs.length ? `<div class="arc-list">${data.arcs.map((a, i) => `
        <button class="arc ${pos.arc?.id === a.id ? 'on' : ''}" data-arc="${esc(a.id)}">
          <span class="arc-n">${i + 1}</span>
          <span class="arc-main"><b>${esc(a.name)}</b><small>${esc(a.summary || '')}</small></span>
        </button>`).join('')}</div>`
      : `<div class="empty">This world has no timeline yet. Worlds built from a named
           setting get one automatically; a hand-forged world can have arcs added in the Forge.</div>`}

      <label class="field" style="margin-top:16px">
        <span>Alternate universe <small class="hint">any premise you like — declared non-canon</small></span>
        <textarea class="input" id="au-premise" rows="2"
          placeholder="e.g. everyone survives &middot; modern high-school &middot; the villain won">${esc(pos.au || '')}</textarea>
      </label>
      <div class="row"><button class="btn btn-primary" id="au-save">Set the premise</button></div>
      <p class="fineprint">Starting at a later arc seeds the world as it stands then — who has
        already died, where people are, what standings exist. An AU keeps the characters
        and changes the situation.</p>
    </div>`);
}

async function chooseArc(arcId) {
  if (!confirm('Restart this story at that point? The world reseeds to how it stands there.')) return;
  try {
    const r = await api(`/playthroughs/${S.ptId}/timeline/entry`, {
      method: 'POST', body: { arc_id: arcId },
    });
    S.state = r.state; closeOverlays(); renderAll();
    toast(`Now beginning at "${r.name}".`);
  } catch (e) { toast(e.message, 'err'); }
}

async function saveAu() {
  try {
    const r = await api(`/playthroughs/${S.ptId}/au`, {
      method: 'POST', body: { premise: document.getElementById('au-premise')?.value || '' },
    });
    closeOverlays();
    toast(r.premise ? 'Premise set. The world forks from here.' : 'Back to canon.');
  } catch (e) { toast(e.message, 'err'); }
}


/* --- delegation for onboarding, ending, identity, timeline ---------------- */
document.addEventListener('click', async (ev) => {
  const t = ev.target;
  const pick = (sel) => t.closest(sel);

  const onb = pick('[data-onb]');
  if (onb) {
    S.onboardPicks[onb.dataset.onbAxis] = onb.dataset.onb;
    S.onboardStep += 1;
    return renderOnboardStep();
  }
  if (pick('[data-onb-skip]')) { markOnboarded(); closeOverlays(); return renderAll(); }
  if (pick('[data-onb-done]')) return finishOnboarding();

  const line = pick('[data-line]');
  if (line) {
    S.onboardLines = S.onboardLines || [];
    const v = line.dataset.line;
    const at = S.onboardLines.indexOf(v);
    if (at >= 0) S.onboardLines.splice(at, 1); else S.onboardLines.push(v);
    line.classList.toggle('on', at < 0);
    return;
  }

  if (pick('[data-new-run]')) return beginNewRun();

  const arc = pick('[data-arc]');
  if (arc) return chooseArc(arc.dataset.arc);
  if (pick('#au-save')) return saveAu();

  const bm = pick('[data-build-mode]');
  if (bm) {
    S.buildMode = bm.dataset.buildMode;
    document.querySelectorAll('[data-build-mode]').forEach((b) =>
      b.classList.toggle('on', b === bm));
    // "Build my own" looks nothing up, so the preview would be misleading.
    const box = document.getElementById('researchPeek');
    if (S.buildMode === 'original') { if (box) box.innerHTML = ''; }
    else peekResearch();
    return;
  }
  if (pick('#idn-pick')) return document.getElementById('idn-file')?.click();
  if (pick('#idn-add-line')) {
    const wrap = document.getElementById('idn-lines');
    const i = wrap.children.length;
    const beats = S.personaBeats || [];
    const div = document.createElement('div');
    div.className = 'idn-line';
    div.innerHTML = `<select class="input" data-idn-beat="${i}"><option value="">any moment</option>
      ${beats.map((b) => `<option value="${b}">${b.replace(/_/g, ' ')}</option>`).join('')}</select>
      <input class="input" data-idn-line="${i}" placeholder="What they say when that moment comes" />`;
    wrap.appendChild(div);
    return;
  }
  const save = pick('#idn-save');
  if (save) return saveIdentity(save.dataset.card);

  const prem = pick('[data-room-premium]');
  if (prem) return toggleRoomPremium(prem.dataset.roomPremium === '1');
});

document.addEventListener('change', async (ev) => {
  if (ev.target.id === 'idn-file') {
    const url = await uploadImage(ev.target.files?.[0]);
    if (url) {
      S.identityAvatar = url;
      const slot = document.getElementById('idn-avatar');
      if (slot) slot.innerHTML = `<img src="${url}" alt="" />`;
      toast('Your art, uploaded. Nothing here was generated.');
    }
  }
});

document.addEventListener('input', (ev) => {
  if (ev.target.matches('[data-idn], [data-idn-line]')) updateIdentityMeter();
  if (ev.target.id === 'bootSetting') {
    // Debounced: a lookup per keystroke would be rude to the wikis and useless
    // to the player, who is still mid-word.
    clearTimeout(researchTimer);
    researchTimer = setTimeout(peekResearch, 600);
  }
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && ev.target.id === 'onb-line-custom') {
    const v = ev.target.value.trim();
    if (v) { S.onboardLines = S.onboardLines || []; S.onboardLines.push(v); ev.target.value = ''; toast(`Line added: ${v}`); }
    ev.preventDefault();
  }
});

/* --- death resolutions, reachable from wherever the death happened ------- */
async function offerResolutions(payload) {
  if (payload) return showDeathResolutions(payload);
  try {
    const r = await api(`/playthroughs/${S.ptId}/death/offered`);
    showDeathResolutions({
      who: S.state?.protagonist || 'Your character',
      death: true, resolutions: r.resolutions, consequences: null, preset: '',
    });
  } catch (e) { toast(e.message, 'err'); }
}


async function openIdentity() {
  if (!S.cardId) {
    try {
      const cards = await api(`/playthroughs/${S.ptId}/cards`);
      const mine = (cards.cards || []).find((c) => c.player_id === S.playerId);
      S.cardId = mine?.id || null;
      if (mine?.identity) S.identityDraft = mine.identity;
      if (mine?.avatar_url) S.identityAvatar = mine.avatar_url;
    } catch { /* fall through to the card editor */ }
  }
  if (!S.cardId) { toast('Make a character card first — then you can give them a voice.'); return showCard(); }
  return showIdentity(S.cardId);
}


/* ===========================================================================
   Boss editor — a boss is a set of RULES, not a bigger number.

   The design point the blueprint makes is that a boss should be beaten by
   understanding it, not by out-damaging it. So the editor asks for the things
   that make that true: a condition that has to exist before damage lands, a
   weak point that pays off when you hit it, and phases that change the answer
   partway through. Hit points are the least interesting field here, which is
   why they are last.
   ========================================================================= */

const BOSS_CONDITIONS = [
  ['', 'Always vulnerable', 'An ordinary hard fight.'],
  ['grounded', 'Only when grounded', 'Bring it down before anything lands.'],
  ['exposed', 'Only when exposed', 'Someone has to draw it out first.'],
  ['burning', 'Only when burning', 'Set it alight, then hit it.'],
  ['bound', 'Only when bound', 'Hold it still — then it can be hurt.'],
];

const BOSS_WEAK = [
  ['', 'No weak point', ''],
  ['throat', 'The throat', ''],
  ['off_hand', 'The off hand', ''],
  ['eyes', 'The eyes', ''],
  ['heart', 'The heart', ''],
];

function showBossEditor() {
  const b = S.bossDraft || { phases: [] };
  showModal(`${head('Design a boss', 'A boss is beaten by working it out — not by hitting it longer.')}
    <div class="modal-body">
      <label class="field"><span>Name</span>
        <input class="input" id="boss-name" value="${esc(b.name || '')}" placeholder="The thing in the dark" /></label>

      <label class="field"><span>Nothing hurts it unless…
        <small class="hint">the condition that has to be true before damage lands at all</small></span>
        <select class="input" id="boss-cond">${BOSS_CONDITIONS.map(([v, n, hint]) =>
          `<option value="${v}"${b.only_hurt_by === v ? ' selected' : ''}>${n}${hint ? ' — ' + hint : ''}</option>`).join('')}</select></label>

      <label class="field"><span>Weak point <small class="hint">hitting it pays off far more than anything else</small></span>
        <select class="input" id="boss-weak">${BOSS_WEAK.map(([v, n]) =>
          `<option value="${v}"${b.weak_point === v ? ' selected' : ''}>${n}</option>`).join('')}</select></label>

      <div class="field"><span>Phases <small class="hint">the fight changes its own rules as it goes</small></span>
        <div id="boss-phases">${(b.phases || []).map((p, i) => `
          <div class="boss-phase-row" data-i="${i}">
            <input class="input" data-bp-name="${i}" value="${esc(p.name || '')}" placeholder="What it does now" />
            <select class="input" data-bp-cond="${i}">${BOSS_CONDITIONS.map(([v, n]) =>
              `<option value="${v}"${p.only_hurt_by === v ? ' selected' : ''}>${n}</option>`).join('')}</select>
            <select class="input" data-bp-weak="${i}">${BOSS_WEAK.map(([v, n]) =>
              `<option value="${v}"${p.weak_point === v ? ' selected' : ''}>${n}</option>`).join('')}</select>
            <input class="input" type="number" data-bp-above="${i}" min="0" max="100"
                   value="${Math.round((p.above ?? 0) * 100)}" title="Above this % health" />
          </div>`).join('')}</div>
        <button class="btn btn-ghost sm" id="boss-add-phase">Add a phase</button>
      </div>

      <label class="field"><span>Health <small class="hint">the least interesting dial — the rules above are the fight</small></span>
        <input class="input" type="number" id="boss-hp" min="10" max="400" value="${b.hp || 60}" /></label>

      <div class="row" style="margin-top:14px">
        <button class="btn btn-ghost" id="boss-preview">Preview the rules</button>
        <button class="btn btn-primary" id="boss-fight">Start the fight</button>
      </div>
      <div id="boss-out" class="ff-preview"></div>
      <p class="fineprint">When a boss is untouchable, the game says <em>why</em> instead of
        silently doing nothing — so a player can work out what to change.</p>
    </div>`);
}

function collectBoss() {
  const phases = [];
  document.querySelectorAll('[data-bp-name]').forEach((el) => {
    const i = el.dataset.bpName;
    phases.push({
      name: el.value.trim() || `Phase ${Number(i) + 1}`,
      only_hurt_by: document.querySelector(`[data-bp-cond="${i}"]`)?.value || '',
      weak_point: document.querySelector(`[data-bp-weak="${i}"]`)?.value || '',
      above: (Number(document.querySelector(`[data-bp-above="${i}"]`)?.value) || 0) / 100,
    });
  });
  phases.sort((a, b) => b.above - a.above);
  return {
    name: document.getElementById('boss-name')?.value.trim() || 'The thing in the dark',
    only_hurt_by: document.getElementById('boss-cond')?.value || '',
    weak_point: document.getElementById('boss-weak')?.value || '',
    hp: Number(document.getElementById('boss-hp')?.value) || 60,
    phases,
  };
}

function previewBoss() {
  const b = collectBoss();
  const lines = [];
  lines.push(b.only_hurt_by
    ? `Nothing hurts <b>${esc(b.name)}</b> unless it is <b>${esc(b.only_hurt_by)}</b>.`
    : `<b>${esc(esc(b.name))}</b> can be hurt at any time.`);
  if (b.weak_point) lines.push(`Hitting the <b>${esc(b.weak_point)}</b> is what pays off.`);
  b.phases.forEach((p) => lines.push(
    `Above ${Math.round(p.above * 100)}% health — <b>${esc(p.name)}</b>` +
    (p.only_hurt_by ? `, only hurt when ${esc(p.only_hurt_by)}` : '') +
    (p.weak_point ? `, weak at the ${esc(p.weak_point)}` : '') + '.'));
  const el = document.getElementById('boss-out');
  if (el) el.innerHTML = `<div class="ff-plan">${lines.join('<br>')}</div>`;
}

async function startBossFight() {
  const boss = collectBoss();
  S.bossDraft = boss;
  try {
    const r = await api(`/playthroughs/${S.ptId}/combat?player=${encodeURIComponent(S.playerId)}`, {
      method: 'POST',
      body: { boss, enemies: [], place_id: S.state?.location },
    });
    S.combat = r; closeOverlays(); setView('combat');
    await loadCombat(); renderCombat();
    toast(`${boss.name} is here.`);
  } catch (e) { toast(e.message, 'err'); }
}

/* --- boss editor delegation --------------------------------------------- */
document.addEventListener('click', (ev) => {
  const t = ev.target;
  if (t.closest('#boss-add-phase')) {
    const wrap = document.getElementById('boss-phases');
    const i = wrap.querySelectorAll('.boss-phase-row').length;
    const div = document.createElement('div');
    div.className = 'boss-phase-row';
    div.innerHTML = `<input class="input" data-bp-name="${i}" placeholder="What it does now" />
      <select class="input" data-bp-cond="${i}">${BOSS_CONDITIONS.map(([v, n]) => `<option value="${v}">${n}</option>`).join('')}</select>
      <select class="input" data-bp-weak="${i}">${BOSS_WEAK.map(([v, n]) => `<option value="${v}">${n}</option>`).join('')}</select>
      <input class="input" type="number" data-bp-above="${i}" min="0" max="100" value="0" />`;
    wrap.appendChild(div);
    return;
  }
  if (t.closest('#boss-preview')) return previewBoss();
  if (t.closest('#boss-fight')) return startBossFight();
});


/* ===========================================================================
   WORKSTREAM A — the awareness HUD.

   The rule this follows is SIGNAL FIRST, NUMBERS ON DEMAND. Every line is a
   sentence a person would say, not a stat. The number is there when the number
   is the point — an ETA is, a standing score is not — and everything else is
   one hover away.

   All of it is CSS/SVG. Nothing here is generated imagery.
   ========================================================================= */

const SIGILS = {
  eye:    '<circle cx="12" cy="12" r="3.2"/><path d="M1.8 12S5.8 5 12 5s10.2 7 10.2 7-4 7-10.2 7S1.8 12 1.8 12z"/>',
  wave:   '<path d="M2 9c2.5-3 5-3 7.5 0S15 12 17.5 9 22 6 22 6M2 15c2.5-3 5-3 7.5 0s5.5 3 8 0 4.5-3 4.5-3"/>',
  banner: '<path d="M6 3v18M6 4h12l-2.5 4L18 12H6"/>',
  blade:  '<path d="M14.5 2.5 7 10l-2 7 7-2 7.5-7.5zM7 10l4 4"/>',
  calm:   '<circle cx="12" cy="12" r="9"/><path d="M8 13.5s1.5 1.5 4 1.5 4-1.5 4-1.5"/>',
  scales: '<path d="M12 3v18M4 7h16M7 7l-3 6h6zM17 7l-3 6h6z"/>',
};

const TONE_CLASS = { danger: 'sig-danger', warn: 'sig-warn', good: 'sig-good', calm: 'sig-calm' };

async function loadAwareness() {
  if (!S.ptId) return null;
  try {
    S.awareness = await api(`/playthroughs/${S.ptId}/awareness?player=${encodeURIComponent(S.playerId)}`);
    if (!S.tooltips) S.tooltips = await api('/tooltips');
  } catch { S.awareness = null; }
  return S.awareness;
}

function tip(key) {
  const t = S.tooltips?.[key];
  if (!t) return '';
  return ` data-tip="${esc(t.what)}" data-tip-why="${esc(t.why)}"`;
}

function renderAwareness() {
  const host = $('#awarenessPanel');
  if (!host) return;
  const a = S.awareness;
  if (!a) { host.innerHTML = ''; return; }

  const pulse = (a.pulse || []).map((s) => `
    <div class="sig ${TONE_CLASS[s.tone] || ''}"${tip(s.kind)}>
      <svg viewBox="0 0 24 24" class="sig-ico">${SIGILS[s.sigil] || SIGILS.calm}</svg>
      <div class="sig-body">
        <div class="sig-text">${esc(s.text)}</div>
        ${s.detail ? `<div class="sig-detail">${esc(s.detail)}</div>` : ''}
      </div>
      ${s.eta !== undefined ? `<div class="sig-eta">${s.eta}<small>turns</small></div>` : ''}
    </div>`).join('');

  // "Unknown" is the internal band name and reads as an error next to a faction
  // that simply has not met you. Every band gets a phrase a person would say.
  const BAND_LABEL = {
    hunted: 'wants you gone', hated: 'would turn you in', disliked: 'wary of you',
    unknown: 'no opinion yet', tolerated: 'will hear you out',
    trusted: 'trusts you', beloved: 'stands with you',
  };
  const factions = (a.factions || []).map((f) => `
    <div class="fac-chip band-${esc(f.band)}"${tip('standing')} title="${esc(f.blurb)}">
      <span class="fac-name">${esc(f.name)}</span>
      <span class="fac-band">${esc(BAND_LABEL[f.band] || f.band)}</span>
      ${f.fear ? `<span class="fac-fear">${esc(f.fear)}</span>` : ''}
      ${S.prefs.power ? `<span class="fac-num">${f.standing}</span>` : ''}
    </div>`).join('');

  // A face card has to answer "how does this person feel about me, and do they
  // know anything?" at a glance. Two rows of identical unlabelled pips answered
  // neither - the bars are labelled, fear only appears when there IS fear, and
  // the disposition sits on the name line where the eye already is.
  const faces = (a.faces || []).filter((f) => f.alive).map((f) => `
    <div class="face"${tip('faces')}>
      <div class="face-top">
        <span class="face-name">${esc(f.name)}</span>
        <span class="face-disp">${esc(f.disposition)}</span>
      </div>
      <div class="face-bar">
        <em>trust</em>${pips(f.trust_pips, 'trust')}
      </div>
      ${f.fear_pips > 0 ? `<div class="face-bar">
        <em>fear</em>${pips(f.fear_pips, 'fear')}
      </div>` : ''}
      ${f.knows_about_you ? `<div class="face-knows">knows ${f.knows_about_you}
        thing${f.knows_about_you === 1 ? '' : 's'} about you</div>` : ''}
    </div>`).join('');

  host.innerHTML = `
    <div class="aw-head">
      <span${tip('pulse')}>What the world notices</span>
      <button class="mini-btn" id="awSettings" title="Choose what this panel shows">
        <svg viewBox="0 0 16 16" class="ico"><circle cx="8" cy="8" r="2.4"/><path d="M8 1.6v2M8 12.4v2M1.6 8h2M12.4 8h2"/></svg>
      </button>
    </div>
    ${a.shows.pulse ? `<div class="sig-list">${pulse}</div>` : ''}
    ${a.shows.factions && factions ? `<div class="fac-strip"${tip('factions')}>${factions}</div>` : ''}
    ${a.shows.faces && faces ? `<div class="face-row">${faces}</div>` : ''}
    ${a.shows.board && a.board ? renderBoard(a.board) : ''}
    ${renderAuthorityStrip()}`;
}

function pips(n, kind) {
  return [0, 1, 2, 3, 4].map((i) =>
    `<i class="pip pip-${kind} ${i < n ? 'on' : ''}"></i>`).join('');
}

function renderBoard(b) {
  return `<div class="int-board"${tip('board')}>
    <div class="ib-head">Who knows what about you</div>
    ${(b.knows || []).length ? (b.knows || []).map((k) => `
      <div class="ib-row">
        <div class="ib-who">${esc(k.name)} <small>${k.count}</small></div>
        <div class="ib-facts">${k.facts.map((f) => `
          <div class="ib-fact src-${esc(f.source)}">
            <span>${esc(f.summary)}</span>
            <em>${esc(f.source)} · T${f.turn} · ${Math.round(f.confidence * 100)}%</em>
          </div>`).join('')}</div>
      </div>`).join('') : '<div class="empty">Nobody has anything on you.</div>'}
    ${b.unwitnessed ? `<div class="ib-quiet">${b.unwitnessed} thing${b.unwitnessed === 1 ? '' : 's'} you did that nobody ever learned about.</div>` : ''}
  </div>`;
}

/* --- the authority strip (Workstream C, surfaced) ------------------------ */

function renderAuthorityStrip() {
  const au = S.authority;
  if (!au || !au.authorities?.length) return '';
  return `<div class="auth-strip"${tip('bounty')}>
    ${au.authorities.map((a) => `
      <div class="auth-row tier-${esc(a.tier)}">
        <svg viewBox="0 0 24 24" class="sig-ico">${SIGILS.scales}</svg>
        <div class="auth-body">
          <div class="auth-name">${esc(a.name)}</div>
          <div class="auth-tier">${esc(a.response.label)}${a.officer
            ? ` — ${esc(a.officer.name)}` : ''}</div>
          <div class="auth-blurb">${esc(a.response.blurb)}</div>
        </div>
        ${a.tier !== 'clean' ? `<button class="btn btn-ghost sm" data-settle="${esc(a.faction)}">Settle</button>` : ''}
      </div>`).join('')}
  </div>`;
}

async function loadAuthority() {
  if (!S.ptId) return;
  try {
    S.authority = await api(`/playthroughs/${S.ptId}/authority?player=${encodeURIComponent(S.playerId)}`);
  } catch { S.authority = null; }
}

async function showSettle(factionId) {
  const a = (S.authority?.authorities || []).find((x) => x.faction === factionId);
  if (!a) return;
  showModal(`${head(`${a.name}`, a.response.blurb)}
    <div class="modal-body">
      <div class="settle-state tier-${esc(a.tier)}">
        <b>${esc(a.response.label)}</b>
        <span>${a.crimes} recorded${a.officer ? ` · handled by ${esc(a.officer.name)}` : ''}</span>
      </div>
      <div class="settle-opts">
        <button class="settle-opt" data-settle-how="pay" data-faction="${esc(factionId)}">
          <b>Pay it off — ${a.fine_mana} Mana</b>
          <small>Immediate. The matter closes and anyone sent after you is called back.
            They will remember that you paid.</small></button>
        <button class="settle-opt" data-settle-how="atone" data-faction="${esc(factionId)}">
          <b>Make it right — free</b>
          <small>Slower, and it moves how they actually feel about you rather than
            just clearing the ledger.</small></button>
      </div>
      <p class="fineprint">Standing drifts back toward neutral on its own if you stop
        giving them reasons — institutions forget, just slowly.</p>
    </div>`);
}

async function doSettle(factionId, how) {
  try {
    const r = await api(`/playthroughs/${S.ptId}/authority/settle?player=${encodeURIComponent(S.playerId)}`, {
      method: 'POST', body: { faction_id: factionId, how },
    });
    S.state = r.state; closeOverlays();
    await Promise.all([loadAuthority(), loadAwareness()]);
    renderAll();
    toast(`${r.note} ${r.hunts_called_off ? 'Whoever was coming has been called back.' : ''}`);
  } catch (e) { toast(e.message, 'err'); }
}

async function showAwarenessSettings() {
  const a = S.awareness || { shows: {} };
  const rows = [
    ['pulse', 'The pulse', 'Short signals: who saw you, what is travelling, who is coming.'],
    ['faces', 'Relationship faces', 'Trust and fear, per character, toward you.'],
    ['factions', 'Faction standing', 'Every group that has an opinion.'],
    ['board', 'Intelligence board', 'Who knows what, and how they found out.'],
  ];
  showModal(`${head('What the panel shows', `Your world is set to ${esc(a.level)} — these are its defaults, and you can override any of them.`)}
    <div class="modal-body">
      ${rows.map(([k, name, why]) => `
        <label class="hud-toggle">
          <input type="checkbox" data-hud="${k}" ${a.shows[k] ? 'checked' : ''} />
          <span><b>${name}</b><small>${why}</small></span>
        </label>`).join('')}
      <p class="fineprint">Cozy shows only the pulse, Hardcore shows everything — but this
        overrides the mode either way. Nothing here changes what the world DOES,
        only what you are shown.</p>
    </div>`);
}

async function saveHudPrefs() {
  const shows = {};
  document.querySelectorAll('[data-hud]').forEach((el) => { shows[el.dataset.hud] = el.checked; });
  try {
    await api(`/playthroughs/${S.ptId}/awareness/prefs?player=${encodeURIComponent(S.playerId)}`, {
      method: 'POST', body: { shows },
    });
    await loadAwareness(); renderAwareness();
  } catch (e) { toast(e.message, 'err'); }
}

/* ===========================================================================
   WORKSTREAM B — "What is StoryLiver?"

   Five bullets, one mechanic each, and one interactive beat that DEMONSTRATES
   the difference between doing something in public and doing it quietly.
   Showing that once is worth more than any amount of explaining it.
   ========================================================================= */

const EXPLAIN_KEY = 'storyliver.explained';

const EXPLAIN_BULLETS = [
  ['eye', 'The world is awake.',
   'Characters see, hear and gossip. What you do unseen stays unseen — until someone sees it.'],
  ['banner', 'Reputation is per-group, not a score.',
   'Each faction only knows what its own people learned. A village that heard nothing thinks nothing.'],
  ['blade', 'Consequences arrive on foot.',
   'When someone is sent after you, they travel. You get a real window, and you can use it.'],
  ['calm', 'Death is not the end.',
   'If you fall you choose what happens next — a ghost, an heir, a legacy. Only Hardcore is final.'],
  ['scales', 'It is a roguelike, not a chat.',
   'Each entry into a world is a run. It ends, and what you learned carries to the next one.'],
];

function needsExplainer() {
  try { return !localStorage.getItem(EXPLAIN_KEY); } catch { return false; }
}

function showExplainer(replay) {
  showModal(`${head('What is StoryLiver?', 'Five things that make this different from a chatbot.')}
    <div class="modal-body">
      ${tableSvg()}
      <div class="ex-list">${EXPLAIN_BULLETS.map(([sig, t, d]) => `
        <div class="ex-row">
          <svg viewBox="0 0 24 24" class="sig-ico">${SIGILS[sig]}</svg>
          <div><b>${esc(t)}</b><small>${esc(d)}</small></div>
        </div>`).join('')}</div>

      <div class="ex-try">
        <div class="ex-try-head">Try it — the same act, two ways</div>
        <div class="ex-try-opts">
          <button class="ex-try-opt" data-try="public">
            <b>Say it in the taproom</b><small>with people in the room</small></button>
          <button class="ex-try-opt" data-try="quiet">
            <b>Whisper it</b><small>to one person, alone</small></button>
        </div>
        <div id="ex-try-out" class="ex-try-out"></div>
      </div>

      <div class="row" style="margin-top:14px">
        <button class="btn btn-primary" data-explain-done>${replay ? 'Close' : 'Start playing'}</button>
      </div>
    </div>`);
}

function tableSvg() {
  // The table metaphor: the party gathered between runs. Pure SVG, no imagery.
  return `<svg viewBox="0 0 320 120" class="table-svg" aria-label="The table">
    <ellipse cx="160" cy="72" rx="86" ry="26" class="tbl-top"/>
    <ellipse cx="160" cy="66" rx="86" ry="26" class="tbl-face"/>
    ${[52, 96, 160, 224, 268].map((x, i) => `
      <g class="tbl-seat" style="--d:${i * 90}ms">
        <circle cx="${x}" cy="${i % 2 ? 34 : 30}" r="11"/>
        <path d="M${x - 13} ${(i % 2 ? 34 : 30) + 20} q13 -14 26 0" />
      </g>`).join('')}
    <g class="tbl-flame"><path d="M160 52c1.6 6-3.6 8.4-3.6 13a3.6 3.6 0 0 0 7.2 0c0-4.6-5.2-7-3.6-13z"/></g>
  </svg>`;
}

function tryBeat(which) {
  const out = document.getElementById('ex-try-out');
  if (!out) return;
  const html = which === 'public'
    ? `<div class="try-result try-seen">
         <b>Three people heard you.</b>
         <div>Nessa Quill now knows. So does whoever she tells — word starts walking
           toward the Green, and it arrives about two turns from now, a little less
           certain than when it left.</div>
         <div class="try-tail">The Warden's Office keeps a note. Standing drops.</div>
       </div>`
    : `<div class="try-result try-unseen">
         <b>One person heard you. Nobody else.</b>
         <div>No rumour starts, because nothing left the room. The faction standing
           does not move — a group that heard nothing thinks nothing.</div>
         <div class="try-tail">The person you told, though, now has something on you.</div>
       </div>`;
  out.innerHTML = html;
  document.querySelectorAll('[data-try]').forEach((b) =>
    b.classList.toggle('on', b.dataset.try === which));
}

function finishExplainer() {
  try { localStorage.setItem(EXPLAIN_KEY, '1'); } catch { /* private mode */ }
  closeOverlays();
  // Hand straight into world setup so a first-timer meets one continuous flow
  // rather than two modals with a gap between them.
  if (needsOnboarding() && S.state?.turn === 0) startOnboarding();
}

/* ===========================================================================
   WORKSTREAM D — accounts, on the surface.
   ========================================================================= */

async function loadAccount() {
  try {
    const r = await api('/auth/me');
    S.account = r.account; S.guest = r.guest;
    // An authenticated account IS the user id from then on, so Mana, runs and
    // a town's memory all attach to the person rather than to the browser.
    if (r.account) S.userId = r.account.id;
  } catch { S.account = null; S.guest = true; }
  return S.account;
}

function showAuth(mode = 'login') {
  const isLogin = mode === 'login';
  showModal(`${head(isLogin ? 'Sign in' : 'Make an account',
      isLogin ? 'Your Mana, your runs and every town that remembers you.'
              : 'You can keep playing without one — an account is what makes it follow you.')}
    <div class="modal-body">
      <label class="field"><span>Email</span>
        <input class="input" id="au-email" type="email" autocomplete="email" /></label>
      ${isLogin ? '' : `<label class="field"><span>Name</span>
        <input class="input" id="au-name" maxlength="40" placeholder="What the table calls you" /></label>`}
      <label class="field"><span>Password <small class="hint">at least 10 characters — length beats symbols</small></span>
        <input class="input" id="au-pass" type="password"
               autocomplete="${isLogin ? 'current-password' : 'new-password'}" /></label>
      <div id="au-err" class="au-err"></div>
      <div class="row" style="margin-top:12px">
        <button class="btn btn-primary" id="au-go">${isLogin ? 'Sign in' : 'Create account'}</button>
        <button class="btn btn-ghost" data-auth-switch="${isLogin ? 'register' : 'login'}">
          ${isLogin ? 'I need an account' : 'I already have one'}</button>
      </div>
      <div id="au-carry" class="au-carry"></div>
      <p class="fineprint">Your password is stored as an Argon2id hash and cannot be read
        back by anyone, including us. Your session lives in a cookie JavaScript cannot
        touch. Playing as a guest works fine — you just cannot buy Mana or carry a
        reputation between devices.</p>
    </div>`);
  setTimeout(() => document.getElementById('au-email')?.focus(), 60);
  showWhatCarriesOver();
}

async function showWhatCarriesOver() {
  // A specific promise ("2 stories and 140 Mana") is worth far more than
  // "keep your progress", and it is only shown when it is actually true.
  if (S.account) return;
  try {
    const c = await api(`/auth/claimable?user_id=${encodeURIComponent(S.userId)}`);
    if (!c.any) return;
    const bits = [];
    if (c.stories) bits.push(`${c.stories} ${c.stories === 1 ? 'story' : 'stories'}`);
    if (c.mana) bits.push(`${c.mana} Mana`);
    if (c.worlds) bits.push(`${c.worlds} ${c.worlds === 1 ? 'world' : 'worlds'}`);
    const el = document.getElementById('au-carry');
    if (el && bits.length) {
      el.textContent = `${bits.join(' and ')} will come with you.`;
    }
  } catch { /* the offer is a nicety, never a blocker */ }
}

async function doAuth(mode) {
  const email = document.getElementById('au-email')?.value.trim();
  const password = document.getElementById('au-pass')?.value || '';
  const display_name = document.getElementById('au-name')?.value.trim() || '';
  const err = document.getElementById('au-err');
  try {
    // Carry whatever this browser was playing under onto the new account -
    // signing up must never cost you the story you signed up to keep.
    const guest_id = S.guest === false ? '' : S.userId;
    const body = mode === 'login'
      ? { email, password, guest_id }
      : { email, password, display_name, guest_id };
    const r = await api(`/auth/${mode}`, { method: 'POST', body });
    S.account = r.account; S.guest = false; S.userId = r.account.id;
    closeOverlays(); renderAll();
    const moved = r.claimed?.claimed
      ? ' Your stories came with you.' : '';
    toast(`Welcome${r.account.display_name ? ', ' + r.account.display_name : ''}.${moved}`);
  } catch (e) {
    if (err) err.textContent = e.message;
  }
}

async function showProfile() {
  if (!S.account) return showAuth('login');
  let p;
  try { p = await api('/profile'); } catch (e) { return toast(e.message, 'err'); }
  showModal(`${head(esc(p.account.display_name || 'Your profile'), esc(p.account.email))}
    <div class="modal-body">
      <div class="row" style="align-items:center;gap:14px">
        <div class="seat-avatar lg" id="pf-avatar">${p.account.avatar_url
          ? `<img src="${esc(p.account.avatar_url)}" alt="" />`
          : `<span>${esc((p.account.display_name || '?')[0])}</span>`}</div>
        <input type="file" id="pf-file" accept="image/png,image/jpeg,image/gif,image/webp" hidden />
        <button class="btn btn-ghost" id="pf-pick">Upload your art</button>
      </div>
      <label class="field"><span>Name</span>
        <input class="input" id="pf-name" maxlength="40" value="${esc(p.account.display_name)}" /></label>
      <label class="field"><span>About you</span>
        <textarea class="input" id="pf-bio" rows="2" maxlength="400">${esc(p.account.bio || '')}</textarea></label>

      <div class="run-stats" style="margin-top:14px">
        <div class="run-stat"><b>${p.mana.balance}</b><small>Mana</small></div>
        <div class="run-stat"><b>${p.runs.length}</b><small>runs</small></div>
        <div class="run-stat"><b>${p.streak?.current ?? 0}</b><small>day streak</small></div>
        <div class="run-stat"><b>${p.cards.length}</b><small>characters</small></div>
      </div>

      ${p.runs?.length ? `<div class="pf-towns">
        <div class="pf-head">Your runs</div>
        ${p.runs.slice(0, 6).map((r) => `<div class="pf-town">
          <span>${esc(r.world_id || 'a world')} · run ${r.run_no}</span>
          <span class="pf-crimes">${r.turns} turns</span>
          <span class="pf-standing ${r.ended_reason?.includes('died') ? 'bad' : 'good'}">
            ${esc((r.ended_reason || r.status).replace('ended:', ''))}</span>
        </div>`).join('')}</div>` : ''}

      ${p.towns?.length ? `<div class="pf-towns">
        <div class="pf-head">Towns that remember you</div>
        ${p.towns.map((t) => `<div class="pf-town">
          <span>${esc(t.world_id)} · ${esc(t.faction_id)}</span>
          <span class="pf-standing ${t.standing < 0 ? 'bad' : 'good'}">${Math.round(t.standing)}</span>
          ${t.crimes ? `<span class="pf-crimes">${t.crimes} on record</span>` : ''}
        </div>`).join('')}</div>` : ''}

      ${p.sessions?.length > 1 ? `<p class="fineprint">${p.sessions.length} devices signed in.</p>` : ''}

      <div class="row" style="margin-top:14px">
        <button class="btn btn-primary" id="pf-save">Save</button>
        <button class="btn btn-ghost" id="pf-logout">Sign out</button>
      </div>
    </div>`);
}

async function saveProfile() {
  try {
    const r = await api('/profile', { method: 'PUT', body: {
      display_name: document.getElementById('pf-name')?.value || '',
      bio: document.getElementById('pf-bio')?.value || '',
      avatar_url: S.profileAvatar || undefined,
    } });
    S.account = r.account; closeOverlays(); renderAll();
    toast('Saved.');
  } catch (e) { toast(e.message, 'err'); }
}

async function doLogout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch { /* already gone */ }
  S.account = null; S.guest = true;
  closeOverlays(); renderAll();
  toast('Signed out. You can keep playing as a guest.');
}

/* --- delegation ---------------------------------------------------------- */
document.addEventListener('click', async (ev) => {
  const t = ev.target;
  const pick = (s) => t.closest(s);

  if (pick('#awSettings')) return showAwarenessSettings();
  const settle = pick('[data-settle]');
  if (settle) return showSettle(settle.dataset.settle);
  const how = pick('[data-settle-how]');
  if (how) return doSettle(how.dataset.faction, how.dataset.settleHow);

  const tryB = pick('[data-try]');
  if (tryB) return tryBeat(tryB.dataset.try);
  if (pick('[data-explain-done]')) return finishExplainer();

  const sw = pick('[data-auth-switch]');
  if (sw) return showAuth(sw.dataset.authSwitch);
  if (pick('#au-go')) {
    return doAuth(document.getElementById('au-name') ? 'register' : 'login');
  }
  if (pick('#pf-pick')) return document.getElementById('pf-file')?.click();
  if (pick('#pf-save')) return saveProfile();
  if (pick('#pf-logout')) return doLogout();
});

document.addEventListener('change', async (ev) => {
  if (ev.target.matches('[data-hud]')) return saveHudPrefs();
  if (ev.target.id === 'pf-file') {
    const url = await uploadImage(ev.target.files?.[0]);
    if (url) {
      S.profileAvatar = url;
      const slot = document.getElementById('pf-avatar');
      if (slot) slot.innerHTML = `<img src="${url}" alt="" />`;
      toast('Your art. Nothing here was generated.');
    }
  }
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && (ev.target.id === 'au-pass' || ev.target.id === 'au-email')) {
    ev.preventDefault();
    doAuth(document.getElementById('au-name') ? 'register' : 'login');
  }
});
