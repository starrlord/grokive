// Grokive Prompt Studio — content script for grok.com
//
// Injects a small collapsible floating toolbar (bottom-right) onto grok.com with
// three actions: Random (pull a random saved prompt), Enhance (rewrite the field
// text via the server's AI), and Save (store the field text into the saveFolder).
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

  // ---- Toolbar construction ----------------------------------------------------
  let toolbarEl = null;

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

    group.appendChild(btnRandom);
    group.appendChild(btnEnhance);
    group.appendChild(btnSave);

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
      // Width changes on collapse/expand — re-clamp a custom-positioned bar so it
      // can't overflow off-screen after growing.
      if (bar.style.left && bar.style.left !== 'auto') {
        setTimeout(() => clampToViewport(bar), 200);
      }
    });

    bar.appendChild(handle);
    bar.appendChild(group);
    bar.appendChild(close);
    return bar;
  }

  function mountToolbar() {
    if (toolbarEl && document.body.contains(toolbarEl)) return;
    if (!document.body) return;
    toolbarEl = buildToolbar();
    document.body.appendChild(toolbarEl);
    applySavedPos(toolbarEl);   // restore where the user last dragged it
  }

  function removeToolbar() {
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
    });
  } catch (e) { /* noop */ }

  init();
})();
