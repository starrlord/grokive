// Grokive Prompt Studio — content script for grok.com
//
// Injects a small collapsible floating toolbar (bottom-right) onto grok.com with
// three actions: Random (pull a random saved prompt), Enhance (rewrite the field
// text via the server's AI), and Save (store the field text into the saveFolder).
// It also shows a live Grok Imagine quota readout (image + 480p/720p video
// generations left + time until reset), read straight from grok.com's own quota
// endpoint, scoped to /imagine pages.
//
// Hard rules from the shared build contract:
//  - Vanilla JS only, MV2, WebExtension promise API via the `ext` shim.
//  - Content scripts are subject to CORS, so we NEVER fetch the Grokive server
//    directly. ALL network goes through background messages, whose responses are
//    always shaped { ok:boolean, data?:any, error?:string }.
//  - Re-read settings before each action (the user may change them in Options).
//  - Never throw; if the prompt field can't be found, degrade to the clipboard.
//  - Respect settings.injectOnGrok: if false, do nothing at all.

(function () {
  'use strict';

  const ext = (typeof browser !== 'undefined') ? browser : chrome;

  // Avoid double-injection if the script runs more than once on a page.
  if (window.__gksInjected) return;
  window.__gksInjected = true;

  // ---- Settings (single source of truth = ext.storage.local "settings") -------
  // Mirror the contract defaults so a missing key never breaks us.
  const DEFAULT_SETTINGS = {
    baseUrl: 'http://localhost:8080',
    username: '',
    password: '',
    saveFolder: 'Firefox',
    sourceFolder: '__all',
    dialogueLevel: 'normal',
    injectOnGrok: true
  };

  function getSettings() {
    return new Promise((resolve) => {
      try {
        ext.storage.local.get('settings').then((res) => {
          const s = (res && res.settings) ? res.settings : {};
          resolve(Object.assign({}, DEFAULT_SETTINGS, s));
        }).catch(() => resolve(Object.assign({}, DEFAULT_SETTINGS)));
      } catch (e) {
        resolve(Object.assign({}, DEFAULT_SETTINGS));
      }
    });
  }

  // ---- Background messaging ----------------------------------------------------
  // Every reply is { ok, data?, error? }. Normalise rejections into that shape so
  // callers never have to try/catch.
  function send(msg) {
    return new Promise((resolve) => {
      try {
        ext.runtime.sendMessage(msg).then((res) => {
          if (res && typeof res.ok === 'boolean') resolve(res);
          else resolve({ ok: false, error: 'No response from extension background.' });
        }).catch((err) => {
          resolve({ ok: false, error: (err && err.message) ? err.message : 'Background message failed.' });
        });
      } catch (e) {
        resolve({ ok: false, error: (e && e.message) ? e.message : 'Background message failed.' });
      }
    });
  }

  // ---- Prompt field discovery + get/set ---------------------------------------
  // Resilient to grok.com DOM changes. Preference order:
  //   1. The currently-focused <textarea> or [contenteditable].
  //   2. The largest visible <textarea>.
  //   3. Any visible [contenteditable="true"].
  function isVisible(el) {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
    return true;
  }

  function isEditableContentEl(el) {
    if (!el) return false;
    // isContentEditable covers inherited contenteditable too.
    return el.isContentEditable === true || el.getAttribute('contenteditable') === 'true';
  }

  function findPromptField() {
    try {
      // 1. Focused element, if it's an editable target (ignore our own toolbar).
      const active = document.activeElement;
      if (active && !active.closest('.gks-toolbar')) {
        if (active.tagName === 'TEXTAREA' && isVisible(active)) return active;
        if (isEditableContentEl(active) && isVisible(active)) return active;
      }

      // 2. Largest visible <textarea>.
      let best = null;
      let bestArea = 0;
      const areas = document.querySelectorAll('textarea');
      for (const ta of areas) {
        if (ta.closest('.gks-toolbar')) continue;
        if (!isVisible(ta)) continue;
        const r = ta.getBoundingClientRect();
        const a = r.width * r.height;
        if (a > bestArea) { bestArea = a; best = ta; }
      }
      if (best) return best;

      // 3. Largest visible contenteditable.
      let bestCE = null;
      let bestCEArea = 0;
      const editables = document.querySelectorAll('[contenteditable="true"]');
      for (const ce of editables) {
        if (ce.closest('.gks-toolbar')) continue;
        if (!isVisible(ce)) continue;
        const r = ce.getBoundingClientRect();
        const a = r.width * r.height;
        if (a > bestCEArea) { bestCEArea = a; bestCE = ce; }
      }
      if (bestCE) return bestCE;
    } catch (e) {
      // fall through to null
    }
    return null;
  }

  function getFieldText(el) {
    if (!el) return '';
    if (el.tagName === 'TEXTAREA') return el.value || '';
    // contenteditable
    return (el.innerText !== undefined ? el.innerText : el.textContent) || '';
  }

  // Set text and fire an 'input' event so the page's framework (React/etc.) reacts.
  function setFieldText(el, text) {
    if (!el) return false;
    try {
      if (el.tagName === 'TEXTAREA') {
        // Use the native value setter so React's tracked value invalidates and
        // the framework picks up the change.
        const proto = Object.getPrototypeOf(el);
        const desc = Object.getOwnPropertyDescriptor(proto, 'value') ||
                     Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
        if (desc && desc.set) desc.set.call(el, text);
        else el.value = text;
      } else {
        // contenteditable: many grok-style rich editors (Lexical/ProseMirror/Slate)
        // own their model and silently revert a raw `textContent =` mutation. Drive
        // the native input pipeline instead — focus, select-all, then insertText —
        // which those editors honor. Fall back to textContent if execCommand is gone.
        try {
          el.focus();
          const sel = window.getSelection();
          if (sel) {
            const range = document.createRange();
            range.selectNodeContents(el);
            sel.removeAllRanges();
            sel.addRange(range);
          }
          const inserted = document.execCommand && document.execCommand('insertText', false, text);
          if (!inserted) el.textContent = text;
        } catch (e) {
          el.textContent = text;
        }
      }
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    } catch (e) {
      return false;
    }
  }

  // ---- Toast feedback ----------------------------------------------------------
  let toastTimer = null;
  function toast(message, kind) {
    try {
      let host = document.querySelector('.gks-toast');
      if (!host) {
        host = document.createElement('div');
        host.className = 'gks-toast';
        document.body.appendChild(host);
      }
      host.textContent = message;
      host.classList.remove('gks-toast-err', 'gks-toast-ok');
      if (kind === 'error') host.classList.add('gks-toast-err');
      else if (kind === 'ok') host.classList.add('gks-toast-ok');
      // Force reflow so re-triggering the animation works.
      void host.offsetWidth;
      host.classList.add('gks-toast-show');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        host.classList.remove('gks-toast-show');
      }, kind === 'error' ? 4200 : 2600);
    } catch (e) { /* never throw on UI feedback */ }
  }

  function truncate(s, n) {
    if (!s) return '';
    s = String(s).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  // ---- Actions -----------------------------------------------------------------
  function setBusy(btn, busy) {
    if (!btn) return;
    if (busy) { btn.classList.add('gks-busy'); btn.disabled = true; }
    else { btn.classList.remove('gks-busy'); btn.disabled = false; }
  }

  async function doRandom(btn) {
    const settings = await getSettings();
    setBusy(btn, true);
    const res = await send({ type: 'randomPrompt', folder: settings.sourceFolder });
    setBusy(btn, false);
    if (!res.ok) { toast(res.error || 'Could not fetch a prompt.', 'error'); return; }

    const prompt = res.data && res.data.prompt;
    const text = prompt && prompt.text ? prompt.text : '';
    if (!text) { toast('No prompt text returned.', 'error'); return; }

    const field = findPromptField();
    if (field && setFieldText(field, text)) {
      try { field.focus(); } catch (e) { /* noop */ }
      toast('Random prompt inserted', 'ok');
    } else {
      // Degrade to clipboard via background (content scripts can't reliably copy).
      const copy = await send({ type: 'copyToClipboard', text });
      if (copy.ok) toast('No input found — copied to clipboard', 'ok');
      else toast('No input found and copy failed.', 'error');
    }
  }

  async function doEnhance(btn) {
    const field = findPromptField();
    if (!field) { toast('No prompt field found to enhance.', 'error'); return; }
    const current = getFieldText(field).trim();
    if (!current) { toast('Field is empty — nothing to enhance.', 'error'); return; }

    const settings = await getSettings();
    setBusy(btn, true);
    const res = await send({
      type: 'enhance',
      prompt: current,
      dialogueLevel: settings.dialogueLevel,
      dialogueOnly: false
    });
    setBusy(btn, false);
    if (!res.ok) { toast(res.error || 'Enhance failed.', 'error'); return; }

    const enhanced = res.data && res.data.prompt ? res.data.prompt : '';
    if (!enhanced) { toast('AI returned no text.', 'error'); return; }
    if (setFieldText(field, enhanced)) toast('Enhanced ✨', 'ok');
    else {
      const copy = await send({ type: 'copyToClipboard', text: enhanced });
      toast(copy.ok ? 'Enhanced — copied to clipboard' : 'Could not update the field.', copy.ok ? 'ok' : 'error');
    }
  }

  async function doSave(btn) {
    const field = findPromptField();
    if (!field) { toast('No prompt field found to save.', 'error'); return; }
    const current = getFieldText(field).trim();
    if (!current) { toast('Field is empty — nothing to save.', 'error'); return; }

    setBusy(btn, true);
    // folder omitted -> background defaults to settings.saveFolder.
    const res = await send({ type: 'savePrompt', text: current });
    setBusy(btn, false);
    if (!res.ok) { toast(res.error || 'Save failed.', 'error'); return; }

    const folder = (res.data && res.data.folder) ? res.data.folder : 'Firefox';
    const added = res.data && res.data.added;
    toast(added ? ('Saved to ' + folder + ' ✓') : ('Already in ' + folder), 'ok');
  }

  async function doStar(btn) {
    const field = findPromptField();
    if (!field) { toast('No prompt field found to save.', 'error'); return; }
    const current = getFieldText(field).trim();
    if (!current) { toast('Field is empty — nothing to save.', 'error'); return; }

    setBusy(btn, true);
    // folder omitted -> background defaults to settings.saveFolder; starred upgrades
    // an existing entry too (background dedupes by text and bumps the star flag).
    const res = await send({ type: 'savePrompt', text: current, starred: true });
    setBusy(btn, false);
    if (!res.ok) { toast(res.error || 'Save failed.', 'error'); return; }

    const folder = (res.data && res.data.folder) ? res.data.folder : 'Firefox';
    const added = res.data && res.data.added;
    toast(added ? ('Saved & starred ★ to ' + folder) : ('Starred ★ in ' + folder), 'ok');
  }

  // ---- Imagine quota (image + video) ------------------------------------------
  // Grok Imagine exposes per-bucket quota at POST /rest/media/imagine/quota_info
  // (empty JSON body) — the same endpoint the official UI's credit-quota store
  // polls. The toolbar lives ON grok.com, so this is a SAME-ORIGIN fetch and the
  // page's session cookie rides along with credentials:'include' — no API key.
  // The response carries five buckets: image, imagePro, imageEdit, video (480p),
  // video720p (720p). Quirk: when you're not near the cap the server OMITS
  // remainingQueries/nextAvailableAt and just returns { available:true } (treat as
  // unlimited); the live count + reset time only appear as you approach/hit the cap.
  const QUOTA_URL = 'https://grok.com/rest/media/imagine/quota_info';
  const QUOTA_POLL_MS = 60000;            // network refresh cadence
  const RESET_PAD_MS = 30 * 60 * 1000;    // grok.com pads nextAvailableAt by 30 min

  // The five quota buckets in display order. `key` is the response field AND the
  // data-tier attribute; `group` drives a small divider between image and video.
  const QUOTA_TIERS = [
    { key: 'image', label: 'Img', group: 'img' },
    { key: 'imagePro', label: 'Pro', group: 'img' },
    { key: 'imageEdit', label: 'Edit', group: 'img' },
    { key: 'video', label: '480p', group: 'vid' },
    { key: 'video720p', label: '720p', group: 'vid' }
  ];

  let quotaEl = null;
  let quotaPollTimer = null;   // 60s network poll (only while on an Imagine page)
  let quotaTickTimer = null;   // 1s ticker: watches SPA nav + re-renders countdowns
  let quotaRefreshing = false; // guard against overlapping fetches
  let quotaPath = '';          // last seen pathname, to detect client-side nav
  // Map of bucket key -> tier view model { unlimited, remaining, resetAt } (or null).
  let quotaState = {};

  // Turn one quota bucket into a tier view model. `available:true` with no
  // remainingQueries means "plenty left" (the server only sends a number as you
  // near the cap); available:false means exhausted (0).
  function mapBucket(bucket) {
    if (!bucket || typeof bucket !== 'object') return null;
    const hasCount = typeof bucket.remainingQueries === 'number';
    let resetAt = null;
    if (bucket.nextAvailableAt != null) {
      const t = new Date(bucket.nextAvailableAt).getTime();   // ISO-8601 or epoch-ms
      if (Number.isFinite(t)) resetAt = t + RESET_PAD_MS;
    }
    return {
      unlimited: !hasCount && bucket.available !== false,
      remaining: hasCount ? bucket.remainingQueries : (bucket.available === false ? 0 : null),
      resetAt: resetAt
    };
  }

  async function fetchImagineQuota() {
    try {
      const res = await fetch(QUOTA_URL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
      if (!res || !res.ok) return null;
      const data = await res.json();
      return (data && typeof data === 'object') ? data : null;
    } catch (e) {
      return null;   // never throw — the readout just stays hidden
    }
  }

  function fmtCountdown(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const p = (n) => String(n).padStart(2, '0');
    return h > 0 ? (h + ':' + p(m) + ':' + p(sec)) : (m + ':' + p(sec));
  }

  function renderTier(statEl, tier) {
    if (!statEl) return;
    const valEl = statEl.querySelector('.gks-q-val');
    const resetEl = statEl.querySelector('.gks-q-reset');
    const resetValEl = statEl.querySelector('.gks-q-reset-val');
    statEl.classList.remove('gks-q-ok', 'gks-q-low', 'gks-q-out');
    if (!tier) { valEl.textContent = '—'; resetEl.style.display = 'none'; return; }
    if (tier.unlimited) {
      valEl.textContent = '∞';
      statEl.classList.add('gks-q-ok');
    } else if (typeof tier.remaining === 'number') {
      valEl.textContent = String(tier.remaining);
      statEl.classList.add(tier.remaining <= 0 ? 'gks-q-out' : (tier.remaining <= 3 ? 'gks-q-low' : 'gks-q-ok'));
    } else {
      valEl.textContent = '—';
    }
    if (tier.resetAt) {
      resetValEl.textContent = fmtCountdown(tier.resetAt - Date.now());
      resetEl.style.display = '';
    } else {
      resetEl.style.display = 'none';
    }
  }

  function renderQuota() {
    if (!quotaEl) return;
    let known = false;
    let worst = 'ok';          // ok < low < out — follows the most-urgent tier
    let minRemaining = null;   // smallest concrete count across metered tiers
    let allUnlimited = true;
    for (const t of QUOTA_TIERS) {
      const tier = quotaState[t.key] || null;
      if (tier) known = true;
      renderTier(quotaEl.querySelector('[data-tier="' + t.key + '"]'), tier);
      if (!tier || tier.unlimited) continue;
      if (typeof tier.remaining === 'number') {
        allUnlimited = false;
        if (minRemaining == null || tier.remaining < minRemaining) minRemaining = tier.remaining;
        if (tier.remaining <= 0) worst = 'out';
        else if (tier.remaining <= 3 && worst !== 'out') worst = 'low';
      }
    }
    // Badge: colour follows the worst tier; the value is ∞ while everything is
    // unlimited, otherwise the most-urgent remaining count (so the number you
    // actually care about is visible without opening the popover).
    const badge = quotaEl.querySelector('.gks-quota-badge');
    if (badge) {
      badge.classList.remove('gks-q-ok', 'gks-q-low', 'gks-q-out');
      badge.classList.add(worst === 'out' ? 'gks-q-out' : (worst === 'low' ? 'gks-q-low' : 'gks-q-ok'));
      const bVal = badge.querySelector('.gks-qb-val');
      if (bVal) bVal.textContent = allUnlimited ? '∞' : String(minRemaining);
      badge.title = buildQuotaTitle();
    }
    // Reveal only once we actually have data; a failed/blocked fetch (e.g. signed
    // out) leaves every tier null and the block stays hidden rather than showing —.
    quotaEl.style.display = known ? '' : 'none';
  }

  // One-line "Img ∞ · Pro ∞ · Edit ∞ · 480p 12 · 720p ∞" summary for the badge
  // tooltip, so the full picture is available on hover even without the popover.
  function buildQuotaTitle() {
    const parts = [];
    for (const t of QUOTA_TIERS) {
      const tier = quotaState[t.key];
      let v = '—';
      if (tier) {
        if (tier.unlimited) v = '∞';
        else if (typeof tier.remaining === 'number') v = String(tier.remaining);
      }
      parts.push(t.label + ' ' + v);
    }
    return 'Grok Imagine quota — ' + parts.join(' · ');
  }

  // 1s tick: re-render only the countdown text from the stored resetAt; when a
  // window rolls over, pull fresh numbers.
  function updateCountdowns() {
    if (!quotaEl) return;
    let expired = false;
    for (const t of QUOTA_TIERS) {
      const tier = quotaState[t.key];
      if (!tier || !tier.resetAt) continue;
      const left = tier.resetAt - Date.now();
      const stat = quotaEl.querySelector('[data-tier="' + t.key + '"]');
      const rv = stat && stat.querySelector('.gks-q-reset-val');
      if (rv) rv.textContent = fmtCountdown(left);
      if (left <= 0) expired = true;
    }
    if (expired) refreshQuota();
  }

  async function refreshQuota() {
    if (quotaRefreshing) return;
    quotaRefreshing = true;
    const data = await fetchImagineQuota();
    quotaRefreshing = false;
    if (data) {
      const next = {};
      for (const t of QUOTA_TIERS) next[t.key] = mapBucket(data[t.key]);
      quotaState = next;
    }
    renderQuota();
  }

  function buildQuota() {
    const wrap = document.createElement('div');
    wrap.className = 'gks-quota';
    wrap.style.display = 'none';   // hidden until the first successful fetch

    // The always-visible badge: ⚡ glyph + the most-urgent remaining count.
    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'gks-quota-badge';
    badge.setAttribute('aria-haspopup', 'true');
    badge.setAttribute('aria-expanded', 'false');
    badge.title = 'Grok Imagine quota — hover for image & video generations left';
    const bIcon = document.createElement('span');
    bIcon.className = 'gks-qb-icon';
    bIcon.textContent = '⚡';
    const bVal = document.createElement('span');
    bVal.className = 'gks-qb-val';
    bVal.textContent = '…';
    badge.appendChild(bIcon);
    badge.appendChild(bVal);

    // The popover: one row per tier (label left, value + reset right), with a
    // horizontal rule between the image and video groups.
    const pop = document.createElement('div');
    pop.className = 'gks-quota-pop';
    pop.setAttribute('role', 'tooltip');
    let prevGroup = null;
    for (const t of QUOTA_TIERS) {
      if (prevGroup && t.group !== prevGroup) {
        const sep = document.createElement('span');
        sep.className = 'gks-q-divider';
        pop.appendChild(sep);
      }
      prevGroup = t.group;
      const stat = document.createElement('span');
      stat.className = 'gks-q-stat';
      stat.setAttribute('data-tier', t.key);
      const lab = document.createElement('span');
      lab.className = 'gks-q-label';
      lab.textContent = t.label;
      const right = document.createElement('span');
      right.className = 'gks-q-right';
      const val = document.createElement('span');
      val.className = 'gks-q-val';
      val.textContent = '…';
      const reset = document.createElement('span');
      reset.className = 'gks-q-reset';
      reset.style.display = 'none';
      const rv = document.createElement('span');
      rv.className = 'gks-q-reset-val';
      reset.appendChild(document.createTextNode('⏱'));
      reset.appendChild(rv);
      right.appendChild(val);
      right.appendChild(reset);
      stat.appendChild(lab);
      stat.appendChild(right);
      pop.appendChild(stat);
    }

    // Open/close. Hover is handled in CSS (:hover), but a tap on touch devices
    // has no hover — toggle a class on click. Re-clamping isn't needed since the
    // popover opens upward from a bottom-anchored bar.
    badge.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      wrap.classList.toggle('gks-pop-open');
      badge.setAttribute('aria-expanded', wrap.classList.contains('gks-pop-open') ? 'true' : 'false');
    });
    // Outside-tap close is handled by ONE top-level listener (registered in init,
    // alongside the References outside-click handler) keyed off the live `quotaEl`,
    // so it isn't re-added — and leaked — every time the toolbar is remounted.

    wrap.appendChild(badge);
    wrap.appendChild(pop);
    quotaEl = wrap;
    return wrap;
  }

  // Only the Imagine app meters video generations, so scope the readout to
  // /imagine* — on chat or other grok.com pages it stays hidden. grok.com is an
  // SPA, so a one-time path check isn't enough; the persistent 1s ticker also
  // watches for client-side navigation (Firefox Xray wrappers block reliably
  // monkeypatching the page's history.pushState from a content script, so polling
  // the path is the robust, framework-agnostic approach).
  function isImaginePath() {
    try { return /^\/imagine(\/|$)/.test(location.pathname); }
    catch (e) { return false; }
  }

  // Start/stop the 60s network poll based on whether we're on an Imagine page.
  function syncQuota() {
    if (!quotaEl) return;
    if (isImaginePath()) {
      if (!quotaPollTimer) {
        refreshQuota();
        quotaPollTimer = setInterval(refreshQuota, QUOTA_POLL_MS);
      }
    } else if (quotaPollTimer) {
      clearInterval(quotaPollTimer);
      quotaPollTimer = null;
      quotaState = {};   // keys are the QUOTA_TIERS bucket names; empty hides every tier
      renderQuota();     // hides the block once off Imagine
    }
  }

  function quotaTick() {
    if (location.pathname !== quotaPath) {   // client-side navigation
      quotaPath = location.pathname;
      syncQuota();
    }
    if (quotaPollTimer) updateCountdowns();  // live reset countdown while active
  }

  function startQuota() {
    quotaPath = location.pathname;
    if (!quotaTickTimer) quotaTickTimer = setInterval(quotaTick, 1000);
    syncQuota();
  }

  function stopQuota() {
    if (quotaPollTimer) { clearInterval(quotaPollTimer); quotaPollTimer = null; }
    if (quotaTickTimer) { clearInterval(quotaTickTimer); quotaTickTimer = null; }
    quotaEl = null;
  }

  // ---- Toolbar construction ----------------------------------------------------
  let toolbarEl = null;

  // ---- References panel (browse collections → copy an image) ------------------
  // Lists your saved Grokive collections, drills into one to show its image
  // thumbnails, and copies a chosen image to the clipboard as PNG so you can paste
  // it straight into Grok Imagine as a reference image (verified: the Imagine
  // input accepts a pasted clipboard image). Thumbnails and full images are fetched
  // through the background (fetchImageData) — the content script can't reach the
  // Grokive server directly (CORS) and a cross-site <img> would drop the Lax
  // session cookie — then cached as data: URLs for the page session.
  let refsPanelEl = null;        // panel root (lazily built, kept across opens)
  let refsBodyEl = null;         // scroll container + IntersectionObserver root
  let refsTitleEl = null;
  let refsBackEl = null;
  let refsStatusEl = null;
  let refsView = 'collections';  // 'collections' | 'images'
  let refsCurrentCollection = null;
  let refsObserver = null;       // lazy-loads thumbnails as they scroll into view
  const refsImageCache = new Map();    // media/thumbnail path -> data: URL (page session)
  const refsImageInflight = new Map(); // path -> in-flight fetch promise (dedup hover+click)
  const REFS_CACHE_CAP = 300;          // bound the cache so a long session can't grow forever

  function cacheRefImage(path, dataUrl) {
    refsImageCache.set(path, dataUrl);
    if (refsImageCache.size > REFS_CACHE_CAP) {
      const oldest = refsImageCache.keys().next().value;   // FIFO eviction
      if (oldest !== undefined) refsImageCache.delete(oldest);
    }
  }

  // Fetch (or reuse) an image's bytes as a data: URL through the background. The
  // in-flight map means hover-prefetch and the click never duplicate the same
  // request; resolved bytes are cached (capped) for the page session.
  function getRefImageData(path) {
    if (!path) return Promise.reject(new Error('Missing image path.'));
    if (refsImageCache.has(path)) return Promise.resolve(refsImageCache.get(path));
    if (refsImageInflight.has(path)) return refsImageInflight.get(path);
    const p = send({ type: 'fetchImageData', href: path }).then((res) => {
      if (res && res.ok && res.data && res.data.dataUrl) {
        cacheRefImage(path, res.data.dataUrl);
        return res.data.dataUrl;
      }
      throw new Error((res && res.error) || 'Could not load the image.');
    }).finally(() => refsImageInflight.delete(path));
    refsImageInflight.set(path, p);
    return p;
  }

  function refsIsOpen() {
    return !!(refsPanelEl && refsPanelEl.classList.contains('gks-refs-open'));
  }

  function setRefsStatus(message, kind) {
    if (!refsStatusEl) return;
    refsStatusEl.textContent = message || '';
    refsStatusEl.classList.toggle('gks-refs-status-err', kind === 'error');
    refsStatusEl.style.display = message ? '' : 'none';
  }

  // A fresh observer per render: the scroll root is stable but old observed nodes
  // are gone after innerHTML clears, and disconnecting avoids leaking them.
  function resetRefsObserver() {
    if (refsObserver) { try { refsObserver.disconnect(); } catch (e) { /* noop */ } }
    // Capture the instance so the callback always unobserves on its OWN observer,
    // never a later one assigned to refsObserver after a re-render.
    const obs = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        obs.unobserve(img);
        loadRefThumb(img);
      }
    }, { root: refsBodyEl, rootMargin: '150px' });
    refsObserver = obs;
    return obs;
  }

  async function loadRefThumb(img) {
    const path = img.getAttribute('data-src');
    if (!path) return;
    try {
      const dataUrl = await getRefImageData(path);
      if (img.isConnected) {   // a re-render may have replaced the element
        img.src = dataUrl;
        img.classList.add('gks-refs-loaded');
      }
    } catch (e) {
      if (img.isConnected) img.classList.add('gks-refs-broken');
    }
  }

  // data: URL -> PNG Blob via canvas. Normalises jpg/webp to image/png (the one
  // raster type every clipboard reliably accepts); a data: URL is same-origin so
  // the canvas isn't tainted and toBlob() works.
  function dataUrlToPngBlob(dataUrl) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        try {
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth || img.width;
          canvas.height = img.naturalHeight || img.height;
          if (!canvas.width || !canvas.height) { reject(new Error('Image had no dimensions.')); return; }
          canvas.getContext('2d').drawImage(img, 0, 0);
          canvas.toBlob((blob) => {
            if (blob) resolve(blob);
            else reject(new Error('Could not encode the image as PNG.'));
          }, 'image/png');
        } catch (e) { reject(e); }
      };
      img.onerror = () => reject(new Error('Could not decode the image.'));
      img.src = dataUrl;
    });
  }

  // Warm the cache with the FULL image on hover so the click-time copy is instant.
  // Pure latency optimisation — the clipboard write itself works regardless (the
  // clipboardWrite permission means it needs no transient activation). No-op on touch.
  function prefetchRefImage(href) {
    if (!href) return;
    getRefImageData(href).catch(() => { /* a real failure surfaces on click */ });
  }

  async function copyRefImage(item, tile) {
    if (!item || !item.href) { toast('No image to copy.', 'error'); return; }
    if (!navigator.clipboard || typeof window.ClipboardItem === 'undefined') {
      toast('This browser can’t copy images to the clipboard.', 'error');
      return;
    }
    setBusy(tile, true);
    try {
      const dataUrl = await getRefImageData(item.href);
      const pngBlob = await dataUrlToPngBlob(dataUrl);
      await navigator.clipboard.write([new window.ClipboardItem({ 'image/png': pngBlob })]);
      toast('Image copied — paste it into Grok ✓', 'ok');
    } catch (e) {
      toast((e && e.message) ? e.message : 'Copy failed.', 'error');
    } finally {
      setBusy(tile, false);
    }
  }

  // ---- Press-and-hold preview --------------------------------------------------
  // Hold a thumbnail to peek a big version; release to dismiss it (without copying)
  // so you can move to another tile or deliberately click Copy. Quick click = copy.
  let refsPreviewEl = null;
  let refsPreviewImg = null;
  let refsPressTimer = null;       // long-press timer (null once it fires/cancels)
  let refsPreviewActive = false;   // a preview is currently shown
  let refsPreviewToken = 0;        // guards the async full-image swap against a re-press
  let refsSuppressClick = false;   // swallow the click that trails a hold
  const REFS_LONGPRESS_MS = 350;

  function buildRefsPreview() {
    const ov = document.createElement('div');
    ov.className = 'gks-refs-preview';
    const img = document.createElement('img');
    img.className = 'gks-refs-preview-img';
    img.alt = '';
    img.draggable = false;
    ov.appendChild(img);
    document.body.appendChild(ov);
    refsPreviewEl = ov;
    refsPreviewImg = img;
    return ov;
  }

  function showRefPreview(item) {
    if (!item) return;
    if (!refsPreviewEl) buildRefsPreview();
    const token = ++refsPreviewToken;
    refsPreviewActive = true;
    // Instant feedback: show the already-loaded thumbnail scaled up, then upgrade
    // to the full image when it resolves (usually already cached via hover-prefetch).
    const thumbUrl = refsImageCache.get(item.thumb);
    if (thumbUrl) { refsPreviewImg.src = thumbUrl; refsPreviewImg.style.visibility = 'visible'; }
    else { refsPreviewImg.removeAttribute('src'); refsPreviewImg.style.visibility = 'hidden'; }
    refsPreviewEl.classList.add('gks-refs-preview-open');
    getRefImageData(item.href).then((full) => {
      if (token === refsPreviewToken && refsPreviewActive) {
        refsPreviewImg.src = full;
        refsPreviewImg.style.visibility = 'visible';
      }
    }).catch(() => { /* keep the thumb / dim backdrop until release */ });
  }

  function hideRefPreview() {
    if (refsPreviewEl) refsPreviewEl.classList.remove('gks-refs-preview-open');
    refsPreviewActive = false;
    refsPreviewToken++;   // invalidate any in-flight full-image swap
  }

  function startRefPress(item, ev) {
    if (ev && ev.button != null && ev.button !== 0) return;   // primary button / touch only
    if (refsPressTimer) clearTimeout(refsPressTimer);
    refsPressTimer = setTimeout(() => {
      refsPressTimer = null;
      showRefPreview(item);
    }, REFS_LONGPRESS_MS);
  }

  // Pointer left the tile before the hold fired — cancel the pending preview. An
  // already-shown preview stays until release (handled by the document listeners).
  function cancelPendingRefPress() {
    if (refsPressTimer) { clearTimeout(refsPressTimer); refsPressTimer = null; }
  }

  function endRefPress() {
    if (refsPressTimer) { clearTimeout(refsPressTimer); refsPressTimer = null; }
    if (refsPreviewActive) {
      hideRefPreview();
      // Swallow the click the browser fires right after this release so the hold
      // doesn't also copy; clear next tick (same trick as the toolbar drag guard).
      refsSuppressClick = true;
      setTimeout(() => { refsSuppressClick = false; }, 0);
    }
  }

  function renderRefCollections(list) {
    setRefsStatus('');
    refsBodyEl.innerHTML = '';
    if (!list.length) {
      setRefsStatus('No collections yet. Make one in Grokive first.');
      return;
    }
    resetRefsObserver();
    // Compact vertical list: small cover + name + count per row, so many fit in
    // the narrow panel and long names truncate instead of overflowing.
    const wrap = document.createElement('div');
    wrap.className = 'gks-refs-list';
    for (const c of list) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'gks-refs-row';
      const count = (c.imageCount != null) ? c.imageCount : (c.itemCount != null ? c.itemCount : null);
      row.title = (c.locked && !c.unlocked)
        ? (c.name + ' — locked; unlock it in Grokive to browse')
        : (c.name + (count != null ? (' — ' + count + ' image' + (count === 1 ? '' : 's')) : ''));

      const cover = document.createElement('span');
      cover.className = 'gks-refs-row-cover';
      if (c.locked && !c.unlocked) {
        cover.classList.add('gks-refs-locked');
        cover.textContent = '🔒';
      } else if (c.cover) {
        const im = document.createElement('img');
        im.alt = '';
        im.setAttribute('data-src', c.cover);
        cover.appendChild(im);
        refsObserver.observe(im);
      } else {
        cover.classList.add('gks-refs-empty');
        cover.textContent = '🖼';
      }

      const name = document.createElement('span');
      name.className = 'gks-refs-row-name';
      name.textContent = c.name;

      row.appendChild(cover);
      row.appendChild(name);
      if (count != null) {
        const cnt = document.createElement('span');
        cnt.className = 'gks-refs-row-count';
        cnt.textContent = String(count);
        row.appendChild(cnt);
      }
      row.addEventListener('click', () => openRefCollection(c));
      wrap.appendChild(row);
    }
    refsBodyEl.appendChild(wrap);
  }

  function renderRefImages(images, collection) {
    setRefsStatus('');
    refsBodyEl.innerHTML = '';
    if (!images.length) {
      setRefsStatus(
        (collection && collection.locked && !collection.unlocked)
          ? 'This collection is locked. Unlock it in Grokive to see its images.'
          : 'No images in this collection.'
      );
      return;
    }
    resetRefsObserver();
    const grid = document.createElement('div');
    grid.className = 'gks-refs-grid gks-refs-imgs';
    for (const it of images) {
      const tile = document.createElement('button');
      tile.type = 'button';
      tile.className = 'gks-refs-thumb';
      tile.title = (it.prompt ? truncate(it.prompt, 140) + ' — ' : '') + 'Click to copy · press & hold to preview';

      const im = document.createElement('img');
      im.alt = '';
      im.draggable = false;
      im.setAttribute('data-src', it.thumb);

      const overlay = document.createElement('span');
      overlay.className = 'gks-refs-copy';
      overlay.textContent = '📋 Copy';

      tile.appendChild(im);
      tile.appendChild(overlay);
      tile.addEventListener('mouseenter', () => { prefetchRefImage(it.href); });
      // Press & hold → big preview; quick click → copy. The release of a hold sets a
      // flag (in endRefPress) so the trailing click doesn't also copy.
      tile.addEventListener('pointerdown', (ev) => startRefPress(it, ev));
      tile.addEventListener('pointerleave', cancelPendingRefPress);
      tile.addEventListener('contextmenu', (ev) => { if (refsPreviewActive || refsPressTimer) ev.preventDefault(); });
      tile.addEventListener('click', () => {
        if (refsSuppressClick) return;   // this click just ended a press-and-hold preview
        copyRefImage(it, tile);
      });
      grid.appendChild(tile);
      refsObserver.observe(im);
    }
    refsBodyEl.appendChild(grid);
  }

  async function loadRefCollectionsView() {
    refsView = 'collections';
    refsCurrentCollection = null;
    if (refsBackEl) refsBackEl.hidden = true;
    if (refsTitleEl) refsTitleEl.textContent = 'Reference images';
    setRefsStatus('Loading collections…');
    refsBodyEl.innerHTML = '';
    const res = await send({ type: 'getCollections' });
    if (!refsIsOpen() || refsView !== 'collections') return;   // closed/navigated away
    if (!res.ok) { setRefsStatus(res.error || 'Could not load collections.', 'error'); return; }
    renderRefCollections((res.data && res.data.collections) || []);
  }

  async function openRefCollection(c) {
    refsView = 'images';
    refsCurrentCollection = c;
    if (refsBackEl) refsBackEl.hidden = false;
    if (refsTitleEl) refsTitleEl.textContent = c.name || 'Collection';
    setRefsStatus('Loading images…');
    refsBodyEl.innerHTML = '';
    const res = await send({ type: 'getCollectionImages', collectionId: c.id });
    if (!refsIsOpen() || refsCurrentCollection !== c) return;
    if (!res.ok) { setRefsStatus(res.error || 'Could not load images.', 'error'); return; }
    renderRefImages((res.data && res.data.images) || [], c);
  }

  function buildRefsPanel() {
    const panel = document.createElement('div');
    panel.className = 'gks-refs';

    const head = document.createElement('div');
    head.className = 'gks-refs-head';

    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'gks-refs-back';
    back.textContent = '‹ Collections';
    back.hidden = true;
    back.addEventListener('click', loadRefCollectionsView);

    const title = document.createElement('span');
    title.className = 'gks-refs-title';
    title.textContent = 'Reference images';

    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'gks-refs-x';
    x.title = 'Close';
    x.textContent = '×';
    x.addEventListener('click', closeRefsPanel);

    head.appendChild(back);
    head.appendChild(title);
    head.appendChild(x);

    const body = document.createElement('div');
    body.className = 'gks-refs-body';

    const status = document.createElement('div');
    status.className = 'gks-refs-status';
    status.style.display = 'none';

    panel.appendChild(head);
    panel.appendChild(body);
    panel.appendChild(status);

    refsPanelEl = panel;
    refsBodyEl = body;
    refsTitleEl = title;
    refsBackEl = back;
    refsStatusEl = status;
    document.body.appendChild(panel);
    return panel;
  }

  // Anchor the panel to the toolbar: open upward when there's room above (the
  // default bottom-right home), else downward. Right edges align.
  function positionRefsPanel() {
    if (!refsPanelEl || !toolbarEl) return;
    const r = toolbarEl.getBoundingClientRect();
    const margin = 8;
    const panelW = Math.min(380, window.innerWidth - margin * 2);
    let left = Math.round(r.right - panelW);            // align right edges with the bar
    if (left + panelW > window.innerWidth - margin) left = window.innerWidth - margin - panelW;
    if (left < margin) left = margin;                   // never clip off either edge
    refsPanelEl.style.width = panelW + 'px';
    refsPanelEl.style.left = left + 'px';
    refsPanelEl.style.right = 'auto';
    const spaceAbove = r.top - margin * 2;
    const spaceBelow = window.innerHeight - r.bottom - margin * 2;
    if (spaceAbove >= 260 || spaceAbove >= spaceBelow) {
      refsPanelEl.style.bottom = (window.innerHeight - r.top + margin) + 'px';
      refsPanelEl.style.top = 'auto';
      refsPanelEl.style.maxHeight = Math.min(window.innerHeight * 0.8, spaceAbove) + 'px';
    } else {
      refsPanelEl.style.top = (r.bottom + margin) + 'px';
      refsPanelEl.style.bottom = 'auto';
      refsPanelEl.style.maxHeight = Math.min(window.innerHeight * 0.8, spaceBelow) + 'px';
    }
  }

  function openRefsPanel() {
    if (!refsPanelEl) buildRefsPanel();
    positionRefsPanel();
    refsPanelEl.classList.add('gks-refs-open');
    loadRefCollectionsView();
  }

  function closeRefsPanel() {
    if (refsPanelEl) refsPanelEl.classList.remove('gks-refs-open');
    hideRefPreview();   // never leave a peek overlay up after the panel closes
  }

  function toggleRefsPanel() {
    if (refsIsOpen()) closeRefsPanel();
    else openRefsPanel();
  }

  function destroyRefsPanel() {
    if (refsObserver) { try { refsObserver.disconnect(); } catch (e) { /* noop */ } refsObserver = null; }
    try { if (refsPanelEl && refsPanelEl.parentNode) refsPanelEl.parentNode.removeChild(refsPanelEl); } catch (e) { /* noop */ }
    refsPanelEl = refsBodyEl = refsTitleEl = refsBackEl = refsStatusEl = null;
    refsView = 'collections';
    refsCurrentCollection = null;
    refsImageCache.clear();      // release the cached data: URLs with the panel
    refsImageInflight.clear();
    if (refsPressTimer) { clearTimeout(refsPressTimer); refsPressTimer = null; }
    try { if (refsPreviewEl && refsPreviewEl.parentNode) refsPreviewEl.parentNode.removeChild(refsPreviewEl); } catch (e) { /* noop */ }
    refsPreviewEl = refsPreviewImg = null;
    refsPreviewActive = false;
  }

  // Saved position lives under its OWN storage key (not `settings`, which the
  // background validates/strips to known keys). Values are viewport px (left/top).
  function loadPos() {
    return new Promise((resolve) => {
      try {
        ext.storage.local.get('toolbarPos')
          .then((r) => resolve((r && r.toolbarPos) || null))
          .catch(() => resolve(null));
      } catch (e) { resolve(null); }
    });
  }
  function savePos(pos) {
    try { ext.storage.local.set({ toolbarPos: pos }); } catch (e) { /* noop */ }
  }

  // Keep the bar fully on-screen; switches it to left/top anchoring. Returns {left,top}.
  function clampToViewport(bar) {
    if (!bar) return null;
    const rect = bar.getBoundingClientRect();
    const margin = 6;
    const maxLeft = Math.max(margin, window.innerWidth - rect.width - margin);
    const maxTop = Math.max(margin, window.innerHeight - rect.height - margin);
    let left = parseFloat(bar.style.left);
    let top = parseFloat(bar.style.top);
    if (isNaN(left)) left = rect.left;
    if (isNaN(top)) top = rect.top;
    left = Math.min(Math.max(margin, left), maxLeft);
    top = Math.min(Math.max(margin, top), maxTop);
    bar.style.left = left + 'px';
    bar.style.top = top + 'px';
    bar.style.right = 'auto';
    bar.style.bottom = 'auto';
    return { left: left, top: top };
  }

  async function applySavedPos(bar) {
    const pos = await loadPos();
    if (pos && typeof pos.left === 'number' && typeof pos.top === 'number') {
      bar.style.left = pos.left + 'px';
      bar.style.top = pos.top + 'px';
      bar.style.right = 'auto';
      bar.style.bottom = 'auto';
      clampToViewport(bar);
    }
  }

  function buildToolbar() {
    const bar = document.createElement('div');
    bar.className = 'gks-toolbar gks-collapsed-no';
    bar.setAttribute('role', 'toolbar');
    bar.setAttribute('aria-label', 'Grokive Prompt Studio');

    // Collapse/expand handle (the little "G" pill that stays when collapsed).
    const handle = document.createElement('button');
    handle.className = 'gks-handle';
    handle.type = 'button';
    handle.title = 'Grokive Prompt Studio — drag to move, click to collapse';
    handle.textContent = 'G';

    const group = document.createElement('div');
    group.className = 'gks-group';

    function mkBtn(label, title, handler) {
      const b = document.createElement('button');
      b.className = 'gks-btn';
      b.type = 'button';
      b.title = title;
      b.textContent = label;
      b.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        // Wrap so a thrown handler can never bubble to the page.
        Promise.resolve().then(() => handler(b)).catch((e) => {
          toast('Something went wrong.', 'error');
        });
      });
      return b;
    }

    const btnRandom = mkBtn('🎲', 'Random prompt from your library (Grokive)', doRandom);
    const btnEnhance = mkBtn('✨', 'Enhance the current prompt with AI', doEnhance);
    const btnSave = mkBtn('💾', 'Save the current prompt to your saveFolder', doSave);
    const btnStar = mkBtn('⭐', 'Save & star the current prompt (Grokive favorites)', doStar);
    btnStar.classList.add('gks-btn-star');
    const btnRefs = mkBtn('📎', 'Reference images — browse collections & copy one to paste into Grok', () => toggleRefsPanel());
    btnRefs.classList.add('gks-btn-refs');

    group.appendChild(btnRandom);
    group.appendChild(btnEnhance);
    group.appendChild(btnSave);
    group.appendChild(btnStar);
    group.appendChild(btnRefs);

    // Close button hides the toolbar for this page session.
    const close = document.createElement('button');
    close.className = 'gks-close';
    close.type = 'button';
    close.title = 'Hide toolbar (until reload)';
    close.textContent = '×';
    close.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      bar.classList.add('gks-hidden');
      closeRefsPanel();
    });

    // --- Drag-to-move (the handle is the grip) -------------------------------
    // Pointer events cover mouse + touch. A movement threshold distinguishes a
    // drag from a click, so nudging the handle doesn't accidentally collapse it.
    let dragging = false;
    let justDragged = false;
    let startX = 0, startY = 0, startLeft = 0, startTop = 0;
    const DRAG_THRESHOLD = 5;

    function onMove(ev) {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (!dragging && (Math.abs(dx) + Math.abs(dy)) < DRAG_THRESHOLD) return;
      if (!dragging) {
        dragging = true;
        bar.classList.add('gks-dragging');
        bar.style.right = 'auto';
        bar.style.bottom = 'auto';
        closeRefsPanel();   // the panel is anchored to the bar; don't leave it stranded
      }
      bar.style.left = (startLeft + dx) + 'px';
      bar.style.top = (startTop + dy) + 'px';
      ev.preventDefault();
    }
    function onUp(ev) {
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('pointerup', onUp);
      handle.removeEventListener('pointercancel', onUp);
      try { handle.releasePointerCapture(ev.pointerId); } catch (e) { /* noop */ }
      if (dragging) {
        dragging = false;
        bar.classList.remove('gks-dragging');
        const pos = clampToViewport(bar);
        if (pos) savePos(pos);
        justDragged = true;              // suppress the click that trails a drag
        setTimeout(() => { justDragged = false; }, 0);
      }
    }
    handle.addEventListener('pointerdown', (ev) => {
      if (ev.button != null && ev.button !== 0) return;   // primary button / touch only
      const rect = bar.getBoundingClientRect();
      startX = ev.clientX; startY = ev.clientY;
      startLeft = rect.left; startTop = rect.top;
      dragging = false;
      try { handle.setPointerCapture(ev.pointerId); } catch (e) { /* noop */ }
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onUp);
      handle.addEventListener('pointercancel', onUp);
    });

    handle.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (justDragged) return;           // this "click" was just the end of a drag
      bar.classList.toggle('gks-collapsed');
      closeRefsPanel();                  // the 📎 button is hidden while collapsed
      // Width changes on collapse/expand — re-clamp a custom-positioned bar so it
      // can't overflow off-screen after growing.
      if (bar.style.left && bar.style.left !== 'auto') {
        setTimeout(() => clampToViewport(bar), 200);
      }
    });

    bar.appendChild(handle);
    bar.appendChild(group);
    bar.appendChild(buildQuota());
    bar.appendChild(close);
    return bar;
  }

  function mountToolbar() {
    if (toolbarEl && document.body.contains(toolbarEl)) return;
    if (!document.body) return;
    toolbarEl = buildToolbar();
    document.body.appendChild(toolbarEl);
    applySavedPos(toolbarEl);   // restore where the user last dragged it
    startQuota();               // begin polling Imagine video quota
  }

  function removeToolbar() {
    stopQuota();
    destroyRefsPanel();
    try {
      if (toolbarEl && toolbarEl.parentNode) toolbarEl.parentNode.removeChild(toolbarEl);
    } catch (e) { /* noop */ }
    toolbarEl = null;
  }

  // ---- Init + react to settings changes ---------------------------------------
  async function init() {
    const settings = await getSettings();
    if (settings.injectOnGrok === false) {
      removeToolbar();
      return;
    }
    if (!document.body) {
      // body not ready yet (shouldn't happen at document_idle, but be safe)
      document.addEventListener('DOMContentLoaded', mountToolbar, { once: true });
      return;
    }
    mountToolbar();
  }

  // Live-toggle when the user flips injectOnGrok in Options.
  try {
    ext.storage.onChanged.addListener((changes, area) => {
      if (area !== 'local' || !changes.settings) return;
      const next = changes.settings.newValue || {};
      if (next.injectOnGrok === false) removeToolbar();
      else mountToolbar();
    });
  } catch (e) { /* storage.onChanged not critical */ }

  // Keep a custom-positioned toolbar on-screen when the window/viewport resizes.
  try {
    window.addEventListener('resize', () => {
      if (toolbarEl && toolbarEl.style.left && toolbarEl.style.left !== 'auto') {
        clampToViewport(toolbarEl);
      }
      if (refsIsOpen()) positionRefsPanel();
    });
  } catch (e) { /* noop */ }

  // Close the References panel on an outside click or Escape. The toolbar itself
  // is exempt so the 📎 button keeps toggling it (capture phase so a page that
  // stops propagation can't trap the click).
  try {
    document.addEventListener('pointerdown', (ev) => {
      if (!refsIsOpen()) return;
      if (refsPanelEl.contains(ev.target)) return;
      if (toolbarEl && toolbarEl.contains(ev.target)) return;
      closeRefsPanel();
    }, true);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && refsIsOpen()) closeRefsPanel();
    });
    // Quota popover: close on an outside tap. Registered ONCE here (not inside
    // buildQuota) so a toolbar remount can't leak a stale, detached handler.
    document.addEventListener('pointerdown', (ev) => {
      if (!quotaEl || !quotaEl.classList.contains('gks-pop-open')) return;
      if (quotaEl.contains(ev.target)) return;
      quotaEl.classList.remove('gks-pop-open');
      const b = quotaEl.querySelector('.gks-quota-badge');
      if (b) b.setAttribute('aria-expanded', 'false');
    });
    // Release anywhere ends a press-and-hold preview — a safety net in case the
    // pointer left the tile before lifting (the overlay itself is pointer-events:none).
    document.addEventListener('pointerup', endRefPress);
    document.addEventListener('pointercancel', endRefPress);
  } catch (e) { /* noop */ }

  // Refresh the quota when the tab comes back to the foreground so a backgrounded
  // tab doesn't show a stale count/countdown.
  try {
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && quotaPollTimer) refreshQuota();
    });
  } catch (e) { /* noop */ }

  init();
})();
