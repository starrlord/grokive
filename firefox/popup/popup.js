/* Grokive Prompt Studio — popup logic.
 *
 * ALL server interaction goes through the background page via
 * ext.runtime.sendMessage({ type, ... }). The popup NEVER fetches the
 * Grokive server directly (CORS + the contract forbids it).
 *
 * Background responses are always shaped: { ok: boolean, data?, error? }.
 *
 * Message types this popup sends:
 *   - getSettings
 *   - setSettings
 *   - status
 *   - getResponses
 *   - searchPrompts  { query, folder, offset, limit }
 *   - getPrompt      { id }
 *   - copyPrompt     { id, text }
 *   - randomPrompt   { folder }
 *   - enhance        { prompt, dialogueLevel, dialogueOnly }
 *   - generate       { prompt, mode, n }
 *   - savePrompt     { text, folder?, starred? }
 *   - copyToClipboard { text }
 */

const ext = (typeof browser !== 'undefined') ? browser : chrome;

/* Settings defaults — mirror the shared contract so a missing key never
   breaks the popup even if background somehow returns a partial object. */
const DEFAULT_SETTINGS = {
  baseUrl: 'http://localhost:8080',
  username: '',
  password: '',
  saveFolder: 'Firefox',
  sourceFolder: '__all',
  dialogueLevel: 'normal',
  injectOnGrok: true,
};

/* ---------- Element refs ---------- */
const el = {};
function bindElements() {
  const ids = [
    'statusWrap', 'statusDot', 'statusLabel', 'gearBtn', 'toast',
    'sourceFolder', 'randomBtn', 'anotherBtn',
    'searchQuery', 'searchClear', 'searchInfo', 'searchScoped', 'resultsList', 'searchMoreBtn',
    'promptText', 'charCount', 'poolInfo',
    'copyBtn', 'undoBtn', 'dialogueSeg', 'dialogueOnly',
    'enhanceBtn', 'variationsBtn', 'saveBtn', 'saveBtnFolder', 'starBtn', 'variationsList',
    'newPromptText', 'saveNewBtn', 'saveNewBtnLabel',
  ];
  for (const id of ids) el[id] = document.getElementById(id);
}

/* ---------- State ---------- */
let settings = { ...DEFAULT_SETTINGS };
let llmReady = false;
let preEnhanceText = null;   // for Undo
let toastTimer = null;

/* Search state. `searchSeq` guards against out-of-order replies: a slow response
   for an older keystroke must never overwrite a newer one's results. */
const SEARCH_PAGE = 25;      // rows per request; "Show more" appends the next page
const SEARCH_DEBOUNCE_MS = 160;
let searchTimer = null;
let searchSeq = 0;
let searchOffset = 0;
let searchTotal = 0;
let searchTerms = [];
let libraryCount = 0;

/* ---------- Messaging helper ---------- */
async function send(type, args = {}) {
  try {
    const res = await ext.runtime.sendMessage({ type, ...args });
    if (!res) return { ok: false, error: 'No response from background.' };
    return res;
  } catch (e) {
    return { ok: false, error: (e && e.message) ? e.message : 'Extension messaging failed.' };
  }
}

/* ---------- Toast ---------- */
function toast(message, kind = 'info', sticky = false) {
  if (!el.toast) return;
  el.toast.textContent = message;
  el.toast.className = 'gks-toast gks-toast--' + kind;
  el.toast.hidden = false;
  if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  if (!sticky) {
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, 3200);
  }
}
function clearToast() {
  if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  if (el.toast) el.toast.hidden = true;
}

/* ---------- Busy helper ---------- */
function setBusy(button, busy, busyLabel) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.innerHTML;
    button.disabled = true;
    button.classList.add('gks-busy');
    button.innerHTML = '<span class="gks-spin"></span>' + (busyLabel ? ' ' + busyLabel : '');
  } else {
    button.classList.remove('gks-busy');
    if (button.dataset.label != null) {
      button.innerHTML = button.dataset.label;
      delete button.dataset.label;
    }
    button.disabled = false;
  }
}

/* ---------- Char count ---------- */
function updateCharCount() {
  if (!el.promptText || !el.charCount) return;
  const n = el.promptText.value.length;
  el.charCount.textContent = n + (n === 1 ? ' char' : ' chars');
}

/* ---------- Status dot ---------- */
function applyStatus(data) {
  // data: { connected, baseUrl, authRequired, authed, llmReady, embedReady, model, saveFolder }
  let cls = 'gks-dot--offline';
  let label = 'Offline';

  if (!data || !data.connected) {
    cls = 'gks-dot--offline';
    label = 'Offline';
  } else if (data.authRequired && !data.authed) {
    cls = 'gks-dot--amber';
    label = 'Needs login';
  } else {
    cls = 'gks-dot--online';
    label = 'Connected';
  }

  el.statusDot.className = 'gks-dot ' + cls;
  el.statusLabel.textContent = label;

  // Build a descriptive tooltip.
  if (data && data.connected) {
    const bits = [];
    bits.push('Server: ' + (data.baseUrl || settings.baseUrl));
    bits.push(data.authRequired ? (data.authed ? 'Authenticated' : 'Login required') : 'No auth');
    bits.push(data.llmReady ? ('AI ready' + (data.model ? ' (' + data.model + ')' : '')) : 'AI not configured');
    el.statusWrap.title = bits.join(' · ');
  } else {
    el.statusWrap.title = 'Cannot reach ' + (settings.baseUrl || DEFAULT_SETTINGS.baseUrl);
  }

  llmReady = !!(data && data.llmReady);
  applyLlmGating();

  if (data && data.saveFolder) {
    settings.saveFolder = data.saveFolder;
    reflectSaveFolder();
  }
}

function applyLlmGating() {
  const tip = 'AI not configured on the server';
  for (const btn of [el.enhanceBtn, el.variationsBtn]) {
    if (!btn) continue;
    btn.disabled = !llmReady;
    btn.title = llmReady ? '' : tip;
  }
}

/* ---------- Save-folder labels ---------- */
function reflectSaveFolder() {
  const f = settings.saveFolder || DEFAULT_SETTINGS.saveFolder;
  if (el.saveBtnFolder) el.saveBtnFolder.textContent = 'Save';
  if (el.saveBtn) el.saveBtn.title = 'Save to ' + f;
  if (el.saveNewBtnLabel) el.saveNewBtnLabel.textContent = 'Save to ' + f;
}

/* ---------- Folder <select> ---------- */
function populateFolders(data) {
  // data: { responses, folders:[{name,count}], unfiled, total }
  const sel = el.sourceFolder;
  const folders = (data && Array.isArray(data.folders)) ? data.folders : [];
  const unfiled = (data && typeof data.unfiled === 'number') ? data.unfiled : 0;
  const total = (data && typeof data.total === 'number') ? data.total : 0;

  sel.innerHTML = '';

  const allOpt = document.createElement('option');
  allOpt.value = '__all';
  allOpt.textContent = 'All' + (total ? ' (' + total + ')' : '');
  sel.appendChild(allOpt);

  const starred = (data && typeof data.starred === 'number') ? data.starred : 0;
  const starOpt = document.createElement('option');
  starOpt.value = '__starred';
  starOpt.textContent = '★ Starred' + (starred ? ' (' + starred + ')' : '');
  sel.appendChild(starOpt);

  if (unfiled > 0) {
    const u = document.createElement('option');
    u.value = '__unfiled';
    u.textContent = 'Unfiled (' + unfiled + ')';
    sel.appendChild(u);
  }

  for (const f of folders) {
    if (!f || !f.name) continue;
    const o = document.createElement('option');
    o.value = f.name;
    o.textContent = f.name + ' (' + (f.count || 0) + ')';
    sel.appendChild(o);
  }

  // Restore previously-picked folder if still present.
  const want = settings.sourceFolder || '__all';
  const has = Array.from(sel.options).some((o) => o.value === want);
  sel.value = has ? want : '__all';
  if (!has && want !== '__all') {
    settings.sourceFolder = '__all';
    // Persist the corrected value silently.
    send('setSettings', { settings: { ...settings, sourceFolder: '__all' } });
  }

  sel.disabled = false;
}

/* ---------- Field setter ---------- */
function setPrompt(text) {
  el.promptText.value = text || '';
  updateCharCount();
}

/* ---------- Library search ---------- */
/* The background owns the cached library and does the matching, so a keystroke
   never refetches it and only ONE page of rows crosses the message channel. */

// '__all' unless the user ticked "This folder only", in which case the search is
// scoped to whatever the Source folder select is on (including ★ Starred/Unfiled).
function searchScope() {
  if (el.searchScoped && el.searchScoped.checked) {
    return el.sourceFolder.value || settings.sourceFolder || '__all';
  }
  return '__all';
}

// Paint `text` into `node`, wrapping matched terms in <mark>. Assembled from DOM
// nodes — never innerHTML — so prompt text can't inject markup into the popup.
function highlightInto(node, text, terms) {
  node.textContent = '';
  if (!terms || !terms.length) { node.textContent = text; return; }

  const lower = text.toLowerCase();
  const ranges = [];
  for (const t of terms) {
    let i = lower.indexOf(t);
    while (i >= 0 && ranges.length < 300) {   // cap: a 1-char term in a long prompt
      ranges.push([i, i + t.length]);
      i = lower.indexOf(t, i + t.length);
    }
  }
  if (!ranges.length) { node.textContent = text; return; }

  // Merge overlapping hits so two terms matching the same span emit one <mark>.
  ranges.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }

  const frag = document.createDocumentFragment();
  let pos = 0;
  for (const [a, b] of merged) {
    if (a > pos) frag.appendChild(document.createTextNode(text.slice(pos, a)));
    const m = document.createElement('mark');
    m.textContent = text.slice(a, b);
    frag.appendChild(m);
    pos = b;
  }
  if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
  node.appendChild(frag);
}

// Rows are preview-capped by the background (prompts may be up to 100K chars), so
// anything that needs the real thing resolves it by id first.
async function fullTextOf(r) {
  if (!r.truncated) return r.text;
  const got = await send('getPrompt', { id: r.id });
  if (got.ok && got.data && got.data.prompt) return got.data.prompt.text;
  return r.text;   // deleted since the search ran — the preview is better than nothing
}

async function loadResultIntoEditor(r) {
  const text = await fullTextOf(r);
  setPrompt(text);
  preEnhanceText = null;
  el.undoBtn.hidden = true;
  hideVariations();
  el.poolInfo.textContent = '';
  toast('Loaded into the editor.', 'info');
  try { el.promptText.scrollIntoView({ block: 'nearest' }); } catch (e) { /* nicety only */ }
}

async function copyResult(r, btn) {
  setBusy(btn, true);
  const res = await send('copyPrompt', { id: r.id, text: r.text });
  setBusy(btn, false);
  if (res.ok) toast('Copied to clipboard ✓', 'ok');
  else toast(res.error || 'Copy failed.', 'error');
}

async function toggleResultStar(r, btn) {
  const next = !r.starred;
  setBusy(btn, true);
  const res = await send('starPrompt', { id: r.id, starred: next });
  setBusy(btn, false);
  if (!res.ok) { toast(res.error || 'Star failed.', 'error'); return; }
  r.starred = next;
  btn.classList.toggle('is-on', next);
  btn.title = next ? 'Starred — click to unstar' : 'Star this prompt';
  toast(next ? 'Starred ★' : 'Unstarred', 'ok');
}

function buildResultRow(r) {
  const li = document.createElement('li');
  li.className = 'gks-result';

  // The full prompt, clamped to a few lines; click anywhere in the text to expand.
  const body = document.createElement('div');
  body.className = 'gks-result-text';
  body.title = 'Click to expand / collapse';
  highlightInto(body, r.text + (r.truncated ? '…' : ''), searchTerms);
  body.addEventListener('click', () => li.classList.toggle('is-open'));

  const foot = document.createElement('div');
  foot.className = 'gks-result-foot';

  const meta = document.createElement('span');
  meta.className = 'gks-result-meta';
  const bits = [r.folder || 'Unfiled'];
  if (r.truncated) bits.push(r.length.toLocaleString() + ' chars');
  meta.textContent = bits.join(' · ');
  if (r.tags && r.tags.length) meta.title = 'Tags: ' + r.tags.join(', ');

  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'gks-mini';
  copyBtn.textContent = '📋 Copy';
  copyBtn.title = 'Copy the full prompt to the clipboard';
  copyBtn.addEventListener('click', () => copyResult(r, copyBtn));

  const loadBtn = document.createElement('button');
  loadBtn.type = 'button';
  loadBtn.className = 'gks-mini';
  loadBtn.textContent = 'Load';
  loadBtn.title = 'Load into the editor above';
  loadBtn.addEventListener('click', () => loadResultIntoEditor(r));

  const starBtn = document.createElement('button');
  starBtn.type = 'button';
  starBtn.className = 'gks-mini gks-mini--star' + (r.starred ? ' is-on' : '');
  starBtn.textContent = '★';
  starBtn.title = r.starred ? 'Starred — click to unstar' : 'Star this prompt';
  starBtn.addEventListener('click', () => toggleResultStar(r, starBtn));

  foot.appendChild(meta);
  foot.appendChild(copyBtn);
  foot.appendChild(loadBtn);
  foot.appendChild(starBtn);

  li.appendChild(body);
  li.appendChild(foot);
  return li;
}

function clearResults() {
  if (!el.resultsList) return;
  el.resultsList.innerHTML = '';
  el.resultsList.hidden = true;
  el.searchMoreBtn.hidden = true;
  searchOffset = 0;
  searchTotal = 0;
  searchTerms = [];
}

function setSearchInfo(text) {
  if (el.searchInfo) el.searchInfo.textContent = text || '';
}

// Idle line: how big the library is, so an empty box still tells you something.
function idleSearchInfo() {
  setSearchInfo(libraryCount ? (libraryCount.toLocaleString() + ' prompts — type to search') : '');
}

async function runSearch(append) {
  const query = (el.searchQuery.value || '').trim();
  if (el.searchClear) el.searchClear.hidden = !query;

  if (!query) {
    clearResults();
    idleSearchInfo();
    return;
  }

  const seq = ++searchSeq;
  const offset = append ? searchOffset : 0;
  if (!append) setSearchInfo('Searching…');
  if (append) setBusy(el.searchMoreBtn, true, 'Loading…');

  const res = await send('searchPrompts', {
    query: query,
    folder: searchScope(),
    offset: offset,
    limit: SEARCH_PAGE,
  });

  // Clear the busy state BEFORE the staleness guard, or a keystroke landing mid-
  // request would leave "Show more" stuck as a disabled spinner.
  if (append) setBusy(el.searchMoreBtn, false);
  if (seq !== searchSeq) return;   // a newer keystroke already owns the list

  if (!res.ok) {
    clearResults();
    setSearchInfo('');
    toast(res.error || 'Search failed.', 'error');
    return;
  }

  const data = res.data || {};
  const rows = Array.isArray(data.results) ? data.results : [];
  searchTerms = Array.isArray(data.terms) ? data.terms : [];
  searchTotal = typeof data.total === 'number' ? data.total : rows.length;
  if (typeof data.library === 'number') libraryCount = data.library;

  if (!append) el.resultsList.innerHTML = '';
  for (const r of rows) el.resultsList.appendChild(buildResultRow(r));
  searchOffset = offset + rows.length;

  el.resultsList.hidden = searchOffset === 0;
  el.searchMoreBtn.hidden = searchOffset >= searchTotal;

  if (!searchTotal) {
    setSearchInfo('No matches');
  } else {
    setSearchInfo(searchOffset.toLocaleString() + ' of ' + searchTotal.toLocaleString());
  }
  if (!append) el.resultsList.scrollTop = 0;
}

function scheduleSearch() {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { searchTimer = null; runSearch(false); }, SEARCH_DEBOUNCE_MS);
}

function onSearchInput() {
  if (el.searchClear) el.searchClear.hidden = !(el.searchQuery.value || '').trim();
  scheduleSearch();
}

function clearSearch() {
  el.searchQuery.value = '';
  if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
  searchSeq++;                 // abandon any in-flight reply
  clearResults();
  idleSearchInfo();
  if (el.searchClear) el.searchClear.hidden = true;
  el.searchQuery.focus();
}

/* ---------- Actions ---------- */
async function doRandom() {
  clearToast();
  hideVariations();
  const folder = el.sourceFolder.value || settings.sourceFolder || '__all';
  setBusy(el.randomBtn, true, 'Rolling…');
  el.anotherBtn.disabled = true;
  const res = await send('randomPrompt', { folder });
  setBusy(el.randomBtn, false);

  if (!res.ok) {
    toast(res.error || 'Could not get a prompt.', 'error');
    el.anotherBtn.disabled = false;
    return;
  }
  const p = res.data && res.data.prompt;
  setPrompt(p ? p.text : '');
  preEnhanceText = null;
  el.undoBtn.hidden = true;
  el.anotherBtn.disabled = false;

  const count = res.data && typeof res.data.count === 'number' ? res.data.count : 0;
  el.poolInfo.textContent = count ? ('1 of ' + count) : '';
}

async function doCopy() {
  const text = el.promptText.value.trim();
  if (!text) { toast('Nothing to copy.', 'info'); return; }
  const res = await send('copyToClipboard', { text });
  if (res.ok) toast('Copied to clipboard ✓', 'ok');
  else toast(res.error || 'Copy failed.', 'error');
}

async function doEnhance() {
  if (!llmReady) { toast('AI not configured on the server.', 'error'); return; }
  const text = el.promptText.value.trim();
  if (!text) { toast('Write or roll a prompt first.', 'info'); return; }

  clearToast();
  preEnhanceText = el.promptText.value;   // snapshot for Undo
  setBusy(el.enhanceBtn, true, 'Enhancing…');
  const res = await send('enhance', {
    prompt: text,
    dialogueLevel: settings.dialogueLevel || 'normal',
    dialogueOnly: !!el.dialogueOnly.checked,
  });
  setBusy(el.enhanceBtn, false);

  if (!res.ok) {
    preEnhanceText = null;
    toast(res.error || 'Enhance failed.', 'error');
    return;
  }
  const enhanced = res.data && res.data.prompt;
  if (!enhanced) {
    preEnhanceText = null;
    toast('Server returned an empty result.', 'error');
    return;
  }
  setPrompt(enhanced);
  el.undoBtn.hidden = false;
  toast('Enhanced ✓', 'ok');
}

function doUndo() {
  if (preEnhanceText == null) return;
  setPrompt(preEnhanceText);
  preEnhanceText = null;
  el.undoBtn.hidden = true;
  toast('Reverted to original.', 'info');
}

async function doVariations() {
  if (!llmReady) { toast('AI not configured on the server.', 'error'); return; }
  const text = el.promptText.value.trim();
  if (!text) { toast('Write or roll a prompt first.', 'info'); return; }

  clearToast();
  hideVariations();
  setBusy(el.variationsBtn, true, '…');
  const res = await send('generate', { prompt: text, mode: 'variations', n: 4 });
  setBusy(el.variationsBtn, false);

  if (!res.ok) {
    toast(res.error || 'Could not generate variations.', 'error');
    return;
  }
  const list = (res.data && Array.isArray(res.data.variations)) ? res.data.variations : [];
  if (!list.length) {
    toast('No variations returned.', 'info');
    return;
  }
  showVariations(list);
}

function showVariations(list) {
  const ul = el.variationsList;
  ul.innerHTML = '';
  for (const v of list) {
    const li = document.createElement('li');
    li.textContent = v;
    li.title = 'Click to load into the editor';
    li.addEventListener('click', () => {
      setPrompt(v);
      preEnhanceText = null;
      el.undoBtn.hidden = true;
      hideVariations();
      toast('Loaded variation into editor.', 'info');
    });
    ul.appendChild(li);
  }
  ul.hidden = false;
}
function hideVariations() {
  if (!el.variationsList) return;
  el.variationsList.hidden = true;
  el.variationsList.innerHTML = '';
}

async function doSaveCurrent() {
  const text = el.promptText.value.trim();
  if (!text) { toast('Nothing to save.', 'info'); return; }
  setBusy(el.saveBtn, true, 'Saving…');
  const res = await send('savePrompt', { text });   // folder defaults to saveFolder in background
  setBusy(el.saveBtn, false);

  if (!res.ok) {
    toast(res.error || 'Save failed.', 'error');
    return;
  }
  const folder = (res.data && res.data.folder) || settings.saveFolder || 'Firefox';
  if (res.data && res.data.added === false) {
    toast('Already in ' + folder + ' (duplicate skipped).', 'info');
  } else {
    toast('Saved to ' + folder + ' ✓', 'ok');
    // Refresh folder counts in the background-backed select.
    refreshFolders();
  }
}

async function doStarCurrent() {
  const text = el.promptText.value.trim();
  if (!text) { toast('Nothing to star.', 'info'); return; }
  const folder = settings.saveFolder || DEFAULT_SETTINGS.saveFolder;
  setBusy(el.starBtn, true, 'Starring…');
  const res = await send('savePrompt', { text, folder, starred: true });
  setBusy(el.starBtn, false);

  if (!res.ok) {
    toast(res.error || 'Save failed.', 'error');
    return;
  }
  toast('Saved & starred ★', 'ok');
  // Refresh folder counts in the background-backed select.
  refreshFolders();
}

async function doSaveNew() {
  const text = el.newPromptText.value.trim();
  if (!text) { toast('Write a prompt to save.', 'info'); return; }
  setBusy(el.saveNewBtn, true, 'Saving…');
  const res = await send('savePrompt', { text });
  setBusy(el.saveNewBtn, false);

  if (!res.ok) {
    toast(res.error || 'Save failed.', 'error');
    return;
  }
  const folder = (res.data && res.data.folder) || settings.saveFolder || 'Firefox';
  if (res.data && res.data.added === false) {
    toast('Already in ' + folder + ' (duplicate skipped).', 'info');
  } else {
    el.newPromptText.value = '';
    toast('Saved to ' + folder + ' ✓', 'ok');
    refreshFolders();
  }
}

/* ---------- Dialogue segmented control ---------- */
function applyDialogueSeg() {
  if (!el.dialogueSeg) return;
  const level = settings.dialogueLevel || 'normal';
  const btns = el.dialogueSeg.querySelectorAll('.gks-seg-btn');
  btns.forEach((b) => {
    b.classList.toggle('is-active', b.dataset.level === level);
    b.setAttribute('aria-pressed', b.dataset.level === level ? 'true' : 'false');
  });
}

function onSegClick(e) {
  const btn = e.target.closest('.gks-seg-btn');
  if (!btn) return;
  const level = btn.dataset.level;
  if (!level || level === settings.dialogueLevel) { applyDialogueSeg(); return; }
  settings.dialogueLevel = level;
  applyDialogueSeg();
  send('setSettings', { settings: { ...settings } });   // persist
}

/* ---------- Folder/source persistence ---------- */
function onSourceChange() {
  settings.sourceFolder = el.sourceFolder.value || '__all';
  send('setSettings', { settings: { ...settings } });
  // A scoped search follows the select, so re-run it against the new folder.
  if (el.searchScoped && el.searchScoped.checked && el.searchQuery.value.trim()) runSearch(false);
}

async function refreshFolders() {
  const res = await send('getResponses');
  if (res.ok && res.data) {
    populateFolders(res.data);
    if (typeof res.data.total === 'number') libraryCount = res.data.total;
    if (!(el.searchQuery && el.searchQuery.value.trim())) idleSearchInfo();
  }
  // Soft-fail: keep whatever is already in the select.
}

/* ---------- Init ---------- */
async function refreshStatus() {
  const res = await send('status');
  if (res.ok) {
    applyStatus(res.data);
  } else {
    applyStatus({ connected: false });
    toast(res.error || ('Can\'t reach Grokive at ' + settings.baseUrl + '. Is the server running?'), 'error', true);
  }
}

async function init() {
  bindElements();

  // Wire events first so the UI is responsive even while loading. Optional-chain
  // every binding so a single renamed/missing element ID degrades that one control
  // instead of throwing and aborting all of init() (settings/status/folders below).
  el.gearBtn?.addEventListener('click', () => {
    if (ext.runtime.openOptionsPage) ext.runtime.openOptionsPage();
  });
  el.promptText?.addEventListener('input', updateCharCount);
  el.randomBtn?.addEventListener('click', doRandom);
  el.anotherBtn?.addEventListener('click', doRandom);
  el.copyBtn?.addEventListener('click', doCopy);
  el.undoBtn?.addEventListener('click', doUndo);
  el.enhanceBtn?.addEventListener('click', doEnhance);
  el.variationsBtn?.addEventListener('click', doVariations);
  el.saveBtn?.addEventListener('click', doSaveCurrent);
  el.starBtn?.addEventListener('click', doStarCurrent);
  el.saveNewBtn?.addEventListener('click', doSaveNew);
  el.dialogueSeg?.addEventListener('click', onSegClick);
  el.sourceFolder?.addEventListener('change', onSourceChange);

  el.searchQuery?.addEventListener('input', onSearchInput);
  el.searchQuery?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); clearSearch(); }
    // Enter searches immediately instead of waiting out the debounce.
    if (e.key === 'Enter') {
      e.preventDefault();
      if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
      runSearch(false);
    }
  });
  el.searchClear?.addEventListener('click', clearSearch);
  el.searchScoped?.addEventListener('change', () => {
    if (el.searchQuery.value.trim()) runSearch(false);
  });
  el.searchMoreBtn?.addEventListener('click', () => runSearch(true));

  updateCharCount();

  // Load settings (fill defaults defensively).
  const sres = await send('getSettings');
  if (sres.ok && sres.data) {
    settings = { ...DEFAULT_SETTINGS, ...sres.data };
  } else {
    settings = { ...DEFAULT_SETTINGS };
  }
  applyDialogueSeg();
  reflectSaveFolder();

  // Gate AI buttons off until status confirms readiness.
  llmReady = false;
  applyLlmGating();

  // Fetch status + folders in parallel.
  await Promise.all([refreshStatus(), refreshFolders()]);
}

document.addEventListener('DOMContentLoaded', init);
