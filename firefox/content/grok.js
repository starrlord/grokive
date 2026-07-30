// Grokive Prompt Studio — content script for grok.com
//
// Injects a small collapsible floating toolbar (bottom-right) onto grok.com with
// three actions: Random (pull a random saved prompt), Enhance (rewrite the field
// text via the server's AI), and Save (store the field text into the saveFolder).
// It also shows a live Grok weekly-usage readout (% of the weekly allowance used,
// a per-product breakdown, and time until reset), read straight from grok.com's own
// billing RPC, scoped to /imagine pages.
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

  // ---- Weekly usage (Grok credits) --------------------------------------------
  // Grok replaced the old per-bucket Imagine quota (Img/Pro/Edit/480p/720p counts)
  // with a single WEEKLY (or monthly) usage allowance shared across products, plus
  // a per-product breakdown — the same data behind grok.com's own "Usage" panel.
  // It's served by a gRPC-Web unary RPC, grok_api_v2.GrokBuildBilling/
  // GetGrokCreditsConfig, at the root path below. The toolbar lives ON grok.com, so
  // this is a SAME-ORIGIN fetch and the page's session cookie rides along with
  // credentials:'include' — no API key. The service accepts only proto encoding, so
  // we frame the request and decode the response by hand (a small, flat message).
  const CREDITS_URL = 'https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig';
  const QUOTA_POLL_MS = 5 * 60 * 1000;   // weekly numbers move slowly; poll every 5 min

  // billing_product.Product enum -> display. Also the fallback sort order (Imagine
  // first, since that's this extension's context).
  const CREDIT_PRODUCTS = {
    5: { key: 'imagine', label: 'Imagine' },
    4: { key: 'chat', label: 'Chat' },
    6: { key: 'voice', label: 'Voice' },
    2: { key: 'build', label: 'Build' },
    3: { key: 'plugins', label: 'Plugins' },
    1: { key: 'api', label: 'API' }
  };
  const CREDIT_PERIODS = { 1: 'monthly', 2: 'weekly' };

  let quotaEl = null;
  let quotaPollTimer = null;   // network poll (only while on an Imagine page)
  let quotaTickTimer = null;   // 1s ticker: watches SPA nav + refreshes the reset label
  let quotaRefreshing = false; // guard against overlapping fetches
  let quotaPath = '';          // last seen pathname, to detect client-side nav
  let creditState = null;      // decoded view model, or null when unknown/hidden

  // ---- minimal protobuf wire decoder ------------------------------------------
  // Just enough to walk GetGrokCreditsConfigResponse: varint, 32-bit (float),
  // 64-bit (double) and length-delimited (sub-message / string). Never throws.
  function pbVarint(st) {
    let shift = 0, result = 0;
    for (;;) {
      const byte = st.b[st.i++];
      result += (byte & 0x7f) * Math.pow(2, shift);   // Number is exact for our ints
      if ((byte & 0x80) === 0) break;
      shift += 7;
    }
    return result;
  }
  // Call fn(field, wireType, value) for each field. Length-delimited -> Uint8Array,
  // varint -> number, 32-bit -> float, 64-bit -> double. Bails on an unknown wire
  // type instead of throwing (a non-quota response just decodes to nothing).
  function pbFields(bytes, fn) {
    const st = { b: bytes, i: 0 };
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    while (st.i < bytes.length) {
      const tag = pbVarint(st);
      const field = tag >>> 3, wt = tag & 7;
      if (wt === 0) fn(field, wt, pbVarint(st));
      else if (wt === 1) { if (st.i + 8 > bytes.length) break; const o = st.i; st.i += 8; fn(field, wt, dv.getFloat64(o, true)); }
      else if (wt === 2) { const len = pbVarint(st); const s = bytes.subarray(st.i, st.i + len); st.i += len; fn(field, wt, s); }
      else if (wt === 5) { if (st.i + 4 > bytes.length) break; const o = st.i; st.i += 4; fn(field, wt, dv.getFloat32(o, true)); }
      else break;
    }
  }
  function pbTimestampMs(msg) {   // google.protobuf.Timestamp { 1: seconds }
    let sec = 0;
    pbFields(msg, (f, wt, v) => { if (f === 1 && wt === 0) sec = v; });
    return sec * 1000;
  }
  function pbCentVal(msg) {       // prod_charger.Cent { 1: val (int64 cents) }
    let val = 0;
    pbFields(msg, (f, wt, v) => { if (f === 1 && wt === 0) val = v; });
    return val;
  }

  // Decode GrokCreditsConfig into the view model the UI consumes. Field numbers are
  // from the service's proto descriptor (verified against a live response):
  //   1 credit_usage_percent(float)  2 on_demand_cap  3 on_demand_used
  //   7 product_usage(repeated)      8 current_period 12 prepaid_balance
  function decodeCreditsConfig(cfg) {
    const out = {
      usedPercent: null, periodType: 'unspecified', resetAt: null,
      products: [], prepaidCents: 0, onDemandCapCents: 0, onDemandUsedCents: 0
    };
    pbFields(cfg, (f, wt, v) => {
      if (f === 1 && wt === 5) out.usedPercent = v;
      else if (f === 2 && wt === 2) out.onDemandCapCents = pbCentVal(v);
      else if (f === 3 && wt === 2) out.onDemandUsedCents = pbCentVal(v);
      else if (f === 7 && wt === 2) {                      // ProductUsage { 1: product, 2: usage_percent }
        let product = 0, pct = 0;
        pbFields(v, (pf, pwt, pv) => {
          if (pf === 1 && pwt === 0) product = pv;
          else if (pf === 2 && pwt === 5) pct = pv;
        });
        const meta = CREDIT_PRODUCTS[product];
        if (meta) out.products.push({ key: meta.key, label: meta.label, percent: pct });
      } else if (f === 8 && wt === 2) {                    // UsagePeriod { 1: type, 3: end }
        pbFields(v, (uf, uwt, uv) => {
          if (uf === 1 && uwt === 0) out.periodType = CREDIT_PERIODS[uv] || 'unspecified';
          else if (uf === 3 && uwt === 2) out.resetAt = pbTimestampMs(uv);
        });
      } else if (f === 12 && wt === 2) out.prepaidCents = pbCentVal(v);
    });
    return out;
  }

  // Parse a gRPC-Web response body (concatenated 5-byte-prefixed frames): the data
  // frame holds the proto message; the trailer frame (flag bit 0x80) carries
  // grpc-status. Returns the decoded config, or null on error / no data.
  function decodeGrpcWeb(buf) {
    const bytes = new Uint8Array(buf);
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let i = 0, message = null, statusOk = true, sawStatus = false;
    while (i + 5 <= bytes.length) {
      const flag = bytes[i];
      const len = dv.getUint32(i + 1, false);
      i += 5;
      const payload = bytes.subarray(i, i + len);
      i += len;
      if (flag & 0x80) {
        const m = /grpc-status:\s*(\d+)/i.exec(new TextDecoder().decode(payload));
        if (m) { sawStatus = true; statusOk = m[1] === '0'; }
      } else if (!(flag & 0x01)) {
        message = payload;   // uncompressed data frame
      }
    }
    if (sawStatus && !statusOk) return null;
    if (!message) return null;
    let cfg = null;
    pbFields(message, (f, wt, v) => { if (f === 1 && wt === 2) cfg = v; });   // response { 1: config }
    return cfg ? decodeCreditsConfig(cfg) : null;
  }

  // Fetch + decode the weekly usage. Same-origin gRPC-Web unary; the request
  // message (GetGrokCreditsConfigRequest) sets no fields, so the body is a single
  // empty (zero-length) frame: [flag=0, length=0].
  async function fetchCreditsConfig() {
    try {
      const res = await fetch(CREDITS_URL, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/grpc-web+proto',
          'Accept': 'application/grpc-web+proto',
          'X-Grpc-Web': '1',
          'x-user-agent': 'grpc-web-javascript/0.1'
        },
        body: new Uint8Array([0, 0, 0, 0, 0])
      });
      if (!res || !res.ok) return null;
      const buf = await res.arrayBuffer();
      return decodeGrpcWeb(buf);
    } catch (e) {
      return null;   // never throw — the readout just stays hidden
    }
  }

  function fmtPct(v) {
    if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
    const r = Math.round(v * 10) / 10;   // one decimal, but drop a trailing .0
    return (Number.isInteger(r) ? String(r) : r.toFixed(1)) + '%';
  }

  function fmtMoney(cents) {
    return '$' + (Math.max(0, cents) / 100).toFixed(2);
  }

  // Coarse "in 6d 3h" / "in 4h 12m" / "in 9m" until the weekly reset.
  function fmtResetIn(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return 'in ' + d + 'd ' + h + 'h';
    if (h > 0) return 'in ' + h + 'h ' + m + 'm';
    if (m > 0) return 'in ' + m + 'm';
    return 'soon';
  }

  // Urgency bucket for the weekly % USED — green while little is used, red near the
  // cap. Badge shows "% used" (matching grok.com's own wording), coloured this way.
  function creditUrgency(used) {
    if (typeof used !== 'number') return 'ok';
    if (used >= 100) return 'out';
    if (used >= 90) return 'low';
    return 'ok';
  }

  function periodLabel(type) {
    return type === 'monthly' ? 'Monthly' : 'Weekly';
  }

  // One-line tooltip, e.g. "Weekly usage — 2% used · Imagine 2% · resets in 6d 3h".
  function buildQuotaTitle() {
    if (!creditState || typeof creditState.usedPercent !== 'number') return 'Grok weekly usage';
    const parts = [periodLabel(creditState.periodType) + ' usage — ' + fmtPct(creditState.usedPercent) + ' used'];
    for (const p of creditState.products) {
      if (p.percent > 0) parts.push(p.label + ' ' + fmtPct(p.percent));
    }
    if (creditState.resetAt) parts.push('resets ' + fmtResetIn(creditState.resetAt - Date.now()));
    return parts.join(' · ');
  }

  function clearChildren(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  function renderQuota() {
    if (!quotaEl) return;
    const st = creditState;
    // Reveal only once we have a usable number; a failed/blocked fetch (e.g. signed
    // out) leaves the block hidden rather than showing a broken readout.
    if (!st || typeof st.usedPercent !== 'number') {
      quotaEl.style.display = 'none';
      return;
    }
    quotaEl.style.display = '';
    // Round once and drive BOTH the number and the colour off it, so the badge's
    // displayed % and its urgency colour can never disagree at a threshold seam
    // (e.g. 99.96 shouldn't render "100%" while the colour stays amber).
    const shownPct = Math.round(st.usedPercent * 10) / 10;
    const urg = creditUrgency(shownPct);

    const badge = quotaEl.querySelector('.gks-quota-badge');
    if (badge) {
      badge.classList.remove('gks-q-ok', 'gks-q-low', 'gks-q-out');
      badge.classList.add('gks-q-' + urg);
      badge.title = buildQuotaTitle();
    }
    const bVal = quotaEl.querySelector('.gks-qb-val');
    if (bVal) bVal.textContent = fmtPct(shownPct);

    const title = quotaEl.querySelector('.gks-q-title');
    if (title) title.textContent = periodLabel(st.periodType) + ' usage';
    const reset = quotaEl.querySelector('.gks-q-reset');
    if (reset) {
      if (st.resetAt) { reset.textContent = 'resets ' + fmtResetIn(st.resetAt - Date.now()); reset.style.display = ''; }
      else reset.style.display = 'none';
    }

    const totalVal = quotaEl.querySelector('.gks-q-total-val');
    if (totalVal) totalVal.textContent = fmtPct(shownPct) + ' used';
    const fill = quotaEl.querySelector('.gks-q-total .gks-q-bar-fill');
    if (fill) {
      fill.style.width = Math.max(0, Math.min(100, shownPct)) + '%';
      fill.classList.remove('gks-q-ok', 'gks-q-low', 'gks-q-out');
      fill.classList.add('gks-q-' + urg);
    }

    // Per-product breakdown: skip zero-usage products, most-used first.
    const bd = quotaEl.querySelector('.gks-q-breakdown');
    if (bd) {
      clearChildren(bd);
      const rows = st.products.filter((p) => p.percent > 0).sort((a, b) => b.percent - a.percent);
      bd.style.display = rows.length ? '' : 'none';
      for (const p of rows) {
        const row = document.createElement('div');
        row.className = 'gks-q-row';
        row.setAttribute('data-product', p.key);
        const lab = document.createElement('span');
        lab.className = 'gks-q-label';
        lab.textContent = p.label;
        const bar = document.createElement('span');
        bar.className = 'gks-q-rowbar';
        const rf = document.createElement('i');
        rf.className = 'gks-q-rowbar-fill';
        rf.style.width = Math.max(0, Math.min(100, p.percent)) + '%';
        bar.appendChild(rf);
        const val = document.createElement('span');
        val.className = 'gks-q-rowval';
        val.textContent = fmtPct(p.percent);
        row.appendChild(lab);
        row.appendChild(bar);
        row.appendChild(val);
        bd.appendChild(row);
      }
    }

    // Extra usage credits (prepaid balance) + on-demand spend, when present.
    const extra = quotaEl.querySelector('.gks-q-extra');
    if (extra) {
      const bits = [];
      if (st.prepaidCents > 0) bits.push('Extra credits ' + fmtMoney(st.prepaidCents));
      if (st.onDemandCapCents > 0) bits.push('On-demand ' + fmtMoney(st.onDemandUsedCents) + ' / ' + fmtMoney(st.onDemandCapCents));
      extra.textContent = bits.join(' · ');
      extra.style.display = bits.length ? '' : 'none';
    }
  }

  async function refreshQuota() {
    if (quotaRefreshing) return;
    quotaRefreshing = true;
    const data = await fetchCreditsConfig();
    quotaRefreshing = false;
    // Keep the last good numbers on a transient failure (don't blank the badge);
    // leaving Imagine explicitly clears creditState via syncQuota.
    if (data) creditState = data;
    renderQuota();
  }

  function buildQuota() {
    const wrap = document.createElement('div');
    wrap.className = 'gks-quota';
    wrap.style.display = 'none';   // hidden until the first successful fetch

    // The always-visible badge: ⚡ glyph + weekly % used.
    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'gks-quota-badge';
    badge.setAttribute('aria-haspopup', 'true');
    badge.setAttribute('aria-expanded', 'false');
    badge.title = 'Grok weekly usage — hover for the per-product breakdown';
    const bIcon = document.createElement('span');
    bIcon.className = 'gks-qb-icon';
    bIcon.textContent = '⚡';
    const bVal = document.createElement('span');
    bVal.className = 'gks-qb-val';
    bVal.textContent = '…';
    badge.appendChild(bIcon);
    badge.appendChild(bVal);

    // Popover: header (period + reset), a total bar, the product breakdown, and an
    // extra-credits line. Rows are filled in renderQuota.
    const pop = document.createElement('div');
    pop.className = 'gks-quota-pop';
    pop.setAttribute('role', 'tooltip');

    const head = document.createElement('div');
    head.className = 'gks-q-head';
    const title = document.createElement('span');
    title.className = 'gks-q-title';
    title.textContent = 'Weekly usage';
    const reset = document.createElement('span');
    reset.className = 'gks-q-reset';
    reset.style.display = 'none';
    head.appendChild(title);
    head.appendChild(reset);

    const total = document.createElement('div');
    total.className = 'gks-q-total';
    const totalVal = document.createElement('span');
    totalVal.className = 'gks-q-total-val';
    totalVal.textContent = '…';
    const bar = document.createElement('span');
    bar.className = 'gks-q-bar';
    const fill = document.createElement('i');
    fill.className = 'gks-q-bar-fill';
    bar.appendChild(fill);
    total.appendChild(totalVal);
    total.appendChild(bar);

    const breakdown = document.createElement('div');
    breakdown.className = 'gks-q-breakdown';

    const extra = document.createElement('div');
    extra.className = 'gks-q-extra';
    extra.style.display = 'none';

    pop.appendChild(head);
    pop.appendChild(total);
    pop.appendChild(breakdown);
    pop.appendChild(extra);

    // Open/close. Hover is CSS (:hover); a tap on touch has no hover, so toggle a
    // class on click. The popover opens upward from the bottom-anchored bar.
    badge.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      wrap.classList.toggle('gks-pop-open');
      badge.setAttribute('aria-expanded', wrap.classList.contains('gks-pop-open') ? 'true' : 'false');
    });
    // Outside-tap close is handled by ONE top-level listener (registered in init,
    // alongside the References outside-click handler) keyed off the live `quotaEl`.

    wrap.appendChild(badge);
    wrap.appendChild(pop);
    quotaEl = wrap;
    return wrap;
  }

  // The weekly allowance is account-wide, but keep the readout scoped to /imagine*
  // to match where it's relevant to generating (and the existing UX contract). grok
  // is an SPA, so a one-time path check isn't enough; the 1s ticker also watches for
  // client-side navigation (Firefox Xray wrappers block monkeypatching the page's
  // history.pushState from a content script, so polling the path is the robust,
  // framework-agnostic approach).
  function isImaginePath() {
    try { return /^\/imagine(\/|$)/.test(location.pathname); }
    catch (e) { return false; }
  }

  // Start/stop the network poll based on whether we're on an Imagine page.
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
      creditState = null;   // clear so the block hides once off Imagine
      renderQuota();
    }
  }

  function quotaTick() {
    if (location.pathname !== quotaPath) {   // client-side navigation
      quotaPath = location.pathname;
      syncQuota();
    }
    // Keep the "resets in …" label fresh while polling (cheap single-node write).
    if (quotaPollTimer && quotaEl && creditState && creditState.resetAt) {
      const reset = quotaEl.querySelector('.gks-q-reset');
      if (reset && reset.style.display !== 'none') {
        reset.textContent = 'resets ' + fmtResetIn(creditState.resetAt - Date.now());
      }
    }
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

  // Anchor a floating panel to the toolbar: open upward when there's room above (the
  // default bottom-right home), else downward. Right edges align. Shared by the
  // References and Find panels — they're anchored identically.
  function positionPanel(panel) {
    if (!panel || !toolbarEl) return;
    const r = toolbarEl.getBoundingClientRect();
    const margin = 8;
    const panelW = Math.min(380, window.innerWidth - margin * 2);
    let left = Math.round(r.right - panelW);            // align right edges with the bar
    if (left + panelW > window.innerWidth - margin) left = window.innerWidth - margin - panelW;
    if (left < margin) left = margin;                   // never clip off either edge
    panel.style.width = panelW + 'px';
    panel.style.left = left + 'px';
    panel.style.right = 'auto';
    const spaceAbove = r.top - margin * 2;
    const spaceBelow = window.innerHeight - r.bottom - margin * 2;
    if (spaceAbove >= 260 || spaceAbove >= spaceBelow) {
      panel.style.bottom = (window.innerHeight - r.top + margin) + 'px';
      panel.style.top = 'auto';
      panel.style.maxHeight = Math.min(window.innerHeight * 0.8, spaceAbove) + 'px';
    } else {
      panel.style.top = (r.bottom + margin) + 'px';
      panel.style.bottom = 'auto';
      panel.style.maxHeight = Math.min(window.innerHeight * 0.8, spaceBelow) + 'px';
    }
  }

  function positionRefsPanel() { positionPanel(refsPanelEl); }

  function openRefsPanel() {
    if (!refsPanelEl) buildRefsPanel();
    closeFindPanel();   // both anchor to the same spot — only one at a time
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

  // ---- Find panel (search every saved prompt → insert or copy) ----------------
  // Searches your WHOLE Grokive prompt library (all folders) from grok.com. The
  // background holds the cached library and does the matching, so typing costs one
  // small message per keystroke instead of re-shipping the library. Click a result
  // to drop the full prompt into the page's input; 📋 copies it instead.
  let findPanelEl = null;
  let findBodyEl = null;
  let findInputEl = null;
  let findStatusEl = null;
  let findMoreEl = null;
  let findTimer = null;
  let findSeq = 0;             // guards against an older reply overwriting a newer one
  let findOffset = 0;
  let findTotal = 0;
  let findTerms = [];
  const FIND_PAGE = 20;
  const FIND_DEBOUNCE_MS = 180;

  function findIsOpen() {
    return !!(findPanelEl && findPanelEl.classList.contains('gks-find-open'));
  }

  function setFindStatus(message, kind) {
    if (!findStatusEl) return;
    findStatusEl.textContent = message || '';
    findStatusEl.classList.toggle('gks-refs-status-err', kind === 'error');
    findStatusEl.style.display = message ? '' : 'none';
  }

  // Wrap matched terms in <mark>, built from DOM nodes so prompt text can never
  // inject markup into the page.
  function findHighlightInto(node, text, terms) {
    node.textContent = '';
    if (!terms || !terms.length) { node.textContent = text; return; }
    const lower = text.toLowerCase();
    const ranges = [];
    for (const t of terms) {
      let i = lower.indexOf(t);
      while (i >= 0 && ranges.length < 300) {
        ranges.push([i, i + t.length]);
        i = lower.indexOf(t, i + t.length);
      }
    }
    if (!ranges.length) { node.textContent = text; return; }
    ranges.sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const r of ranges) {
      const last = merged[merged.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
      else merged.push([r[0], r[1]]);
    }
    const frag = document.createDocumentFragment();
    let pos = 0;
    for (const pair of merged) {
      if (pair[0] > pos) frag.appendChild(document.createTextNode(text.slice(pos, pair[0])));
      const m = document.createElement('mark');
      m.textContent = text.slice(pair[0], pair[1]);
      frag.appendChild(m);
      pos = pair[1];
    }
    if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
    node.appendChild(frag);
  }

  // Rows come back preview-capped (a saved prompt may be up to 100K chars), so
  // resolve the real text by id before inserting or copying.
  async function findFullText(r) {
    if (!r.truncated) return r.text;
    const got = await send({ type: 'getPrompt', id: r.id });
    if (got.ok && got.data && got.data.prompt) return got.data.prompt.text;
    return r.text;
  }

  async function insertFindResult(r, row) {
    setBusy(row, true);
    const text = await findFullText(r);
    setBusy(row, false);
    if (!text) { toast('That prompt is empty.', 'error'); return; }
    const field = findPromptField();
    if (field && setFieldText(field, text)) {
      try { field.focus(); } catch (e) { /* noop */ }
      closeFindPanel();
      toast('Prompt inserted', 'ok');
      return;
    }
    const copy = await send({ type: 'copyToClipboard', text: text });
    if (copy.ok) toast('No input found — copied to clipboard', 'ok');
    else toast('No input found and copy failed.', 'error');
  }

  async function copyFindResult(r, btn) {
    setBusy(btn, true);
    const res = await send({ type: 'copyPrompt', id: r.id, text: r.text });
    setBusy(btn, false);
    if (res.ok) toast('Prompt copied ✓', 'ok');
    else toast(res.error || 'Copy failed.', 'error');
  }

  // One toggle for both entry points (the text and the ⌄), so the caret's state
  // can never drift from the row's.
  function toggleFindRow(row) {
    const open = row.classList.toggle('gks-find-open-row');
    const caret = row.querySelector('.gks-find-mini-expand');
    if (caret) {
      caret.setAttribute('aria-expanded', open ? 'true' : 'false');
      caret.title = open ? 'Collapse' : 'Expand / collapse the full prompt';
    }
  }

  function buildFindRow(r) {
    const row = document.createElement('div');
    row.className = 'gks-find-row';

    // Clicking the text READS it (the row is clamped to 3 lines) — inserting is the
    // explicit Insert button, so skimming a long prompt can't fire off a page edit.
    const body = document.createElement('div');
    body.className = 'gks-find-text';
    body.title = 'Click to expand / collapse · Insert puts it in the prompt field';
    findHighlightInto(body, r.text + (r.truncated ? '…' : ''), findTerms);
    body.addEventListener('click', () => toggleFindRow(row));

    const foot = document.createElement('div');
    foot.className = 'gks-find-foot';

    const meta = document.createElement('span');
    meta.className = 'gks-find-meta';
    const bits = [r.folder || 'Unfiled'];
    if (r.starred) bits.push('★');
    if (r.truncated) bits.push(r.length.toLocaleString() + ' chars');
    meta.textContent = bits.join(' · ');
    if (r.tags && r.tags.length) meta.title = 'Tags: ' + r.tags.join(', ');

    const expand = document.createElement('button');
    expand.type = 'button';
    expand.className = 'gks-find-mini gks-find-mini-expand';
    expand.textContent = '⌄';
    expand.title = 'Expand / collapse the full prompt';
    expand.setAttribute('aria-expanded', 'false');
    expand.addEventListener('click', (ev) => {
      ev.stopPropagation();
      toggleFindRow(row);
    });

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'gks-find-mini';
    copy.textContent = '📋';
    copy.title = 'Copy the full prompt to the clipboard';
    copy.addEventListener('click', (ev) => {
      ev.stopPropagation();
      copyFindResult(r, copy);
    });

    const insert = document.createElement('button');
    insert.type = 'button';
    insert.className = 'gks-find-mini gks-find-mini-go';
    insert.textContent = 'Insert';
    insert.title = 'Put this prompt into the Grok input field';
    insert.addEventListener('click', (ev) => {
      ev.stopPropagation();
      insertFindResult(r, row);
    });

    foot.appendChild(meta);
    foot.appendChild(expand);
    foot.appendChild(copy);
    foot.appendChild(insert);

    row.appendChild(body);
    row.appendChild(foot);
    return row;
  }

  function clearFindResults() {
    if (findBodyEl) findBodyEl.innerHTML = '';
    if (findMoreEl) findMoreEl.style.display = 'none';
    findOffset = 0;
    findTotal = 0;
    findTerms = [];
  }

  async function runFind(append) {
    if (!findPanelEl) return;
    const query = (findInputEl.value || '').trim();
    if (!query) {
      clearFindResults();
      setFindStatus('Type to search every saved prompt.');
      return;
    }

    const seq = ++findSeq;
    const offset = append ? findOffset : 0;
    if (!append) setFindStatus('Searching…');
    if (append && findMoreEl) findMoreEl.disabled = true;

    const res = await send({
      type: 'searchPrompts',
      query: query,
      folder: '__all',          // the whole library, every folder
      offset: offset,
      limit: FIND_PAGE
    });

    // Re-enable BEFORE the staleness guard, or a keystroke landing mid-request
    // would leave "Show more" stuck disabled.
    if (findMoreEl) findMoreEl.disabled = false;
    if (seq !== findSeq || !findPanelEl) return;   // superseded, or the panel is gone

    if (!res.ok) {
      clearFindResults();
      setFindStatus(res.error || 'Search failed.', 'error');
      return;
    }

    const data = res.data || {};
    const rows = Array.isArray(data.results) ? data.results : [];
    findTerms = Array.isArray(data.terms) ? data.terms : [];
    findTotal = (typeof data.total === 'number') ? data.total : rows.length;

    if (!append) findBodyEl.innerHTML = '';
    for (const r of rows) findBodyEl.appendChild(buildFindRow(r));
    findOffset = offset + rows.length;

    if (!findTotal) setFindStatus('No matches.');
    else setFindStatus(findOffset + ' of ' + findTotal);
    if (findMoreEl) findMoreEl.style.display = (findOffset < findTotal) ? '' : 'none';
    if (!append) findBodyEl.scrollTop = 0;
  }

  function scheduleFind() {
    if (findTimer) clearTimeout(findTimer);
    findTimer = setTimeout(() => { findTimer = null; runFind(false); }, FIND_DEBOUNCE_MS);
  }

  function buildFindPanel() {
    const panel = document.createElement('div');
    panel.className = 'gks-refs gks-find';   // reuse the References panel chrome

    const head = document.createElement('div');
    head.className = 'gks-refs-head';

    const title = document.createElement('span');
    title.className = 'gks-refs-title';
    title.textContent = 'Find a prompt';

    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'gks-refs-x';
    x.title = 'Close';
    x.textContent = '×';
    x.addEventListener('click', closeFindPanel);

    head.appendChild(title);
    head.appendChild(x);

    const searchWrap = document.createElement('div');
    searchWrap.className = 'gks-find-search';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'gks-find-input';
    input.placeholder = 'Search all prompts…';
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    input.addEventListener('input', scheduleFind);
    input.addEventListener('keydown', (ev) => {
      ev.stopPropagation();                      // keep grok.com's own hotkeys out of it
      if (ev.key === 'Escape') { ev.preventDefault(); closeFindPanel(); }
      if (ev.key === 'Enter') {
        ev.preventDefault();
        if (findTimer) { clearTimeout(findTimer); findTimer = null; }
        runFind(false);
      }
    });
    searchWrap.appendChild(input);

    const body = document.createElement('div');
    body.className = 'gks-refs-body gks-find-body';

    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'gks-find-more';
    more.textContent = 'Show more';
    more.style.display = 'none';
    more.addEventListener('click', () => runFind(true));

    const status = document.createElement('div');
    status.className = 'gks-refs-status';
    status.style.display = 'none';

    panel.appendChild(head);
    panel.appendChild(searchWrap);
    panel.appendChild(body);
    panel.appendChild(more);
    panel.appendChild(status);

    findPanelEl = panel;
    findBodyEl = body;
    findInputEl = input;
    findStatusEl = status;
    findMoreEl = more;
    document.body.appendChild(panel);
    return panel;
  }

  function openFindPanel() {
    if (!findPanelEl) buildFindPanel();
    closeRefsPanel();   // both anchor to the same spot — only one at a time
    positionPanel(findPanelEl);
    findPanelEl.classList.add('gks-find-open');
    if (!(findInputEl.value || '').trim()) setFindStatus('Type to search every saved prompt.');
    try { findInputEl.focus(); findInputEl.select(); } catch (e) { /* noop */ }
  }

  function closeFindPanel() {
    if (findPanelEl) findPanelEl.classList.remove('gks-find-open');
    if (findTimer) { clearTimeout(findTimer); findTimer = null; }
    findSeq++;   // abandon any in-flight reply
  }

  function toggleFindPanel() {
    if (findIsOpen()) closeFindPanel();
    else openFindPanel();
  }

  function destroyFindPanel() {
    if (findTimer) { clearTimeout(findTimer); findTimer = null; }
    try { if (findPanelEl && findPanelEl.parentNode) findPanelEl.parentNode.removeChild(findPanelEl); } catch (e) { /* noop */ }
    findPanelEl = findBodyEl = findInputEl = findStatusEl = findMoreEl = null;
    findOffset = 0;
    findTotal = 0;
    findTerms = [];
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
    const btnFind = mkBtn('🔎', 'Search all your saved prompts — insert or copy one', () => toggleFindPanel());
    btnFind.classList.add('gks-btn-find');
    const btnRefs = mkBtn('📎', 'Reference images — browse collections & copy one to paste into Grok', () => toggleRefsPanel());
    btnRefs.classList.add('gks-btn-refs');

    group.appendChild(btnRandom);
    group.appendChild(btnFind);
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
      closeFindPanel();
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
        closeRefsPanel();   // the panels are anchored to the bar; don't leave one stranded
        closeFindPanel();
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
      closeRefsPanel();                  // the 📎/🔎 buttons are hidden while collapsed
      closeFindPanel();
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
    destroyFindPanel();
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
      if (findIsOpen()) positionPanel(findPanelEl);
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
    document.addEventListener('pointerdown', (ev) => {
      if (!findIsOpen()) return;
      if (findPanelEl.contains(ev.target)) return;
      if (toolbarEl && toolbarEl.contains(ev.target)) return;
      closeFindPanel();
    }, true);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && refsIsOpen()) closeRefsPanel();
      if (ev.key === 'Escape' && findIsOpen()) closeFindPanel();
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
