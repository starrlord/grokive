// Grokive Prompt Studio — options page logic.
// All network goes through the background page (message protocol). This script
// only talks to background via ext.runtime.sendMessage and never fetches the
// Grokive server directly (CORS-safe by design).

const ext = (typeof browser !== 'undefined') ? browser : chrome;

// Defaults must match the shared contract so a missing key never breaks the UI.
const DEFAULTS = {
  baseUrl: 'http://localhost:8080',
  username: '',
  password: '',
  saveFolder: 'Firefox',
  sourceFolder: '__all',
  dialogueLevel: 'normal',
  injectOnGrok: true
};

// --- DOM refs ---------------------------------------------------------------
const form = document.getElementById('gks-form');
const els = {
  baseUrl: document.getElementById('baseUrl'),
  username: document.getElementById('username'),
  password: document.getElementById('password'),
  saveFolder: document.getElementById('saveFolder'),
  dialogueLevel: document.getElementById('dialogueLevel'),
  injectOnGrok: document.getElementById('injectOnGrok'),
  testBtn: document.getElementById('testBtn'),
  testResult: document.getElementById('testResult'),
  saveBtn: document.getElementById('saveBtn'),
  saveStatus: document.getElementById('saveStatus')
};

// --- helpers ----------------------------------------------------------------

// Send a message to the background and normalize transport errors into the
// standard { ok, data?, error? } envelope.
async function send(msg) {
  try {
    const res = await ext.runtime.sendMessage(msg);
    if (!res || typeof res !== 'object') {
      return { ok: false, error: 'No response from the extension background.' };
    }
    return res;
  } catch (e) {
    return { ok: false, error: (e && e.message) ? e.message : 'Extension messaging failed.' };
  }
}

function trimSlash(url) {
  return String(url || '').trim().replace(/\/+$/, '');
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

// --- load -------------------------------------------------------------------

async function loadSettings() {
  const res = await send({ type: 'getSettings' });
  // Fill from background-provided settings, falling back to defaults per key.
  const s = Object.assign({}, DEFAULTS, (res && res.ok && res.data) ? res.data : {});
  els.baseUrl.value = s.baseUrl;
  els.username.value = s.username;
  els.password.value = s.password;
  els.saveFolder.value = s.saveFolder;
  els.dialogueLevel.value = ['normal', 'dirtier', 'filthier'].includes(s.dialogueLevel)
    ? s.dialogueLevel : 'normal';
  els.injectOnGrok.checked = !!s.injectOnGrok;
}

// --- save -------------------------------------------------------------------

function showSaveStatus(text, kind) {
  els.saveStatus.textContent = text;
  els.saveStatus.className = 'gks-save-status show ' + (kind || '');
  if (kind === 'ok') {
    setTimeout(() => { els.saveStatus.className = 'gks-save-status'; }, 2400);
  }
}

async function saveSettings(e) {
  e.preventDefault();
  const settings = {
    baseUrl: trimSlash(els.baseUrl.value) || DEFAULTS.baseUrl,
    username: els.username.value.trim(),
    password: els.password.value,
    saveFolder: els.saveFolder.value.trim() || DEFAULTS.saveFolder,
    dialogueLevel: els.dialogueLevel.value,
    injectOnGrok: els.injectOnGrok.checked
  };

  // Reflect the trimmed URL/folder back into the inputs.
  els.baseUrl.value = settings.baseUrl;
  els.saveFolder.value = settings.saveFolder;

  els.saveBtn.disabled = true;
  showSaveStatus('Saving…', '');
  const res = await send({ type: 'setSettings', settings });
  els.saveBtn.disabled = false;

  if (res.ok) {
    showSaveStatus('Saved ✓', 'ok');
  } else {
    showSaveStatus(res.error || 'Could not save settings.', 'err');
  }
}

// --- test connection --------------------------------------------------------

function row(label, valueHtml) {
  return `<li><span class="k">${esc(label)}</span><span class="v">${valueHtml}</span></li>`;
}
function yesNo(v, goodWhenTrue) {
  const good = goodWhenTrue ? !!v : !v;
  return `<span class="v ${good ? 'good' : 'bad'}">${v ? 'Yes' : 'No'}</span>`;
}

function renderTest(state, html) {
  els.testResult.hidden = false;
  els.testResult.className = 'gks-test ' + state;
  els.testResult.innerHTML = html;
}

async function testConnection() {
  // Persist the current URL first so the background uses what's on screen.
  const baseUrl = trimSlash(els.baseUrl.value) || DEFAULTS.baseUrl;
  els.baseUrl.value = baseUrl;
  await send({
    type: 'setSettings',
    settings: {
      baseUrl,
      username: els.username.value.trim(),
      password: els.password.value
    }
  });

  els.testBtn.disabled = true;
  renderTest('is-loading', '<span class="gks-test-msg">Testing connection…</span>');

  const res = await send({ type: 'status' });
  els.testBtn.disabled = false;

  if (!res.ok) {
    renderTest('is-err',
      `<div class="gks-test-head"><span class="gks-test-dot"></span>Connection failed</div>` +
      `<div class="gks-test-msg">${esc(res.error || 'Unknown error.')}</div>`);
    return;
  }

  const d = res.data || {};

  if (!d.connected) {
    renderTest('is-err',
      `<div class="gks-test-head"><span class="gks-test-dot"></span>Offline</div>` +
      `<div class="gks-test-msg">Can't reach Grokive at ${esc(d.baseUrl || baseUrl)}. Is the server running?</div>`);
    return;
  }

  // Connected. Determine overall tone: warn if auth is required but not authed.
  const needsLogin = d.authRequired && !d.authed;
  const state = needsLogin ? 'is-warn' : 'is-ok';
  const headline = needsLogin ? 'Connected — login required' : 'Connected';

  let items = '';
  items += row('Server', `<span class="v">${esc(d.baseUrl || baseUrl)}</span>`);
  items += row('Auth required', yesNo(d.authRequired, false));
  if (d.authRequired) {
    items += row('Authenticated',
      d.authed
        ? `<span class="v good">Yes</span>`
        : `<span class="v idle">No — set username &amp; password</span>`);
  }
  items += row('AI (LLM) ready', yesNo(d.llmReady, true));
  if (d.model) items += row('Model', `<span class="v">${esc(d.model)}</span>`);
  items += row('Embeddings ready', yesNo(d.embedReady, true));
  if (d.saveFolder) items += row('Save folder', `<span class="v">${esc(d.saveFolder)}</span>`);

  renderTest(state,
    `<div class="gks-test-head"><span class="gks-test-dot"></span>${esc(headline)}</div>` +
    `<ul>${items}</ul>` +
    (d.llmReady ? '' : `<div class="gks-test-msg" style="margin-top:8px">AI features (Enhance / Variations) are off until the server's LLM is configured.</div>`));
}

// --- wire up ----------------------------------------------------------------

form.addEventListener('submit', saveSettings);
els.testBtn.addEventListener('click', testConnection);

loadSettings();
