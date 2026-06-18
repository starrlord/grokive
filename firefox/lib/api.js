// lib/api.js — Grokive Prompt Studio
// =============================================================================
// SINGLE SOURCE OF TRUTH for: settings (load/merge-with-defaults + save), all
// network access to the Grokive server, auth/login retry, and the random pick.
//
// COUPLING: this file is listed FIRST in manifest.json `background.scripts`, so
// it runs before background/background.js and attaches its API to the shared
// background global as `self.GrokiveAPI`. background.js then consumes that
// object to service ext.runtime.onMessage requests.
//
// CORS-via-background rationale: the Grokive server sends NO CORS headers.
// Page contexts (popup/options/content) therefore CANNOT fetch it directly.
// The background page, however, holds `<all_urls>` host permission, so its
// fetches bypass CORS entirely. That is why ALL network lives here, in the
// background, and every other surface talks to us through runtime messages.
// =============================================================================

(function () {
  'use strict';

  // WebExtension promise-API shim (Firefox `browser`, fall back to `chrome`).
  const ext = (typeof browser !== 'undefined') ? browser : chrome;

  // --- Settings ------------------------------------------------------------
  // Exact contract defaults. Any missing key is filled from here so popup,
  // options, and content can never break on a partially-written settings blob.
  const DEFAULTS = {
    baseUrl: 'http://localhost:8080', // Grokive server origin, no trailing slash
    username: '',                     // only used if the server requires auth
    password: '',
    saveFolder: 'Firefox',            // folder new prompts are saved into
    sourceFolder: '__all',            // last-picked folder for random pull
    dialogueLevel: 'normal',          // normal | dirtier | filthier
    injectOnGrok: true                // show the toolbar on grok.com
  };

  // Merge stored settings over defaults; also normalise baseUrl (no trailing /).
  async function getSettingsRaw() {
    let stored = {};
    try {
      const got = await ext.storage.local.get('settings');
      stored = (got && got.settings) ? got.settings : {};
    } catch (e) {
      stored = {};
    }
    const merged = Object.assign({}, DEFAULTS, stored);
    merged.baseUrl = stripTrailingSlash(String(merged.baseUrl || DEFAULTS.baseUrl));
    return merged;
  }

  function stripTrailingSlash(s) {
    return s.replace(/\/+$/, '');
  }

  // Public wrappers always return the {ok,data,error} envelope.
  async function getSettings() {
    try {
      return ok(await getSettingsRaw());
    } catch (e) {
      return fail(e);
    }
  }

  async function setSettings(partial) {
    try {
      const current = await getSettingsRaw();
      const next = Object.assign({}, current, partial || {});
      next.baseUrl = stripTrailingSlash(String(next.baseUrl || DEFAULTS.baseUrl));
      // Only persist known keys so the blob stays clean.
      const clean = {};
      for (const k of Object.keys(DEFAULTS)) clean[k] = next[k];
      await ext.storage.local.set({ settings: clean });
      return ok(clean);
    } catch (e) {
      return fail(e);
    }
  }

  // --- Envelope helpers ----------------------------------------------------
  function ok(data) { return { ok: true, data: data }; }
  function fail(err) {
    const msg = (err && err.message) ? err.message : String(err);
    return { ok: false, error: msg };
  }

  // --- Low-level fetch + auth ----------------------------------------------
  // All requests use credentials:'include' so the Grokive session cookie rides
  // along. Throws a friendly Error if the server can't be reached.
  async function rawFetch(baseUrl, path, opts) {
    const url = baseUrl + path;
    let res;
    try {
      res = await fetch(url, Object.assign({ credentials: 'include' }, opts || {}));
    } catch (e) {
      throw new Error("Can't reach Grokive at " + baseUrl + '. Is the server running?');
    }
    return res;
  }

  async function getJson(baseUrl, path) {
    const res = await rawFetch(baseUrl, path, { method: 'GET' });
    return { res: res, json: await safeJson(res) };
  }

  async function postJson(baseUrl, path, body) {
    const res = await rawFetch(baseUrl, path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    return { res: res, json: await safeJson(res) };
  }

  async function safeJson(res) {
    try {
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  // GET /api/auth/status -> { auth_required, authed }
  async function authStatus(baseUrl) {
    try {
      const { res, json } = await getJson(baseUrl, '/api/auth/status');
      if (!res.ok || !json) {
        return { auth_required: false, authed: false, reachable: res ? true : false };
      }
      return {
        auth_required: !!json.auth_required,
        authed: !!json.authed,
        reachable: true
      };
    } catch (e) {
      // Network failure bubbles up so callers can show the unreachable message.
      throw e;
    }
  }

  // POST /api/login with stored creds. Returns true on success.
  async function tryLogin(s) {
    if (!s.username) return false; // nothing to log in with
    const { res, json } = await postJson(s.baseUrl, '/api/login', {
      username: s.username,
      password: s.password
    });
    return res.ok && json && json.ok === true;
  }

  // Ensure we're authed for a protected call. Returns null if good to go, or a
  // friendly error string if login is required but failed.
  async function ensureAuth(s) {
    let st;
    try {
      st = await authStatus(s.baseUrl);
    } catch (e) {
      return e.message; // unreachable
    }
    if (!st.auth_required || st.authed) return null;
    // Needs auth and we're not authed -> attempt login with stored creds.
    const loggedIn = await tryLogin(s);
    if (loggedIn) return null;
    return 'Login failed — check credentials, or run the server with AUTH_DISABLED=true.';
  }

  // Run a protected request, transparently logging in + retrying once on 401.
  // `doRequest` is an async fn (s) => { res, json }.
  async function withAuth(s, doRequest) {
    // Proactive auth check (cheap; also primes the cookie).
    const authErr = await ensureAuth(s);
    if (authErr) throw new Error(authErr);

    let { res, json } = await doRequest(s);
    if (!res) throw new Error('Request failed — no response from the server.');
    if (res.status === 401) {
      // Session may have lapsed mid-flight — re-login once and retry.
      const loggedIn = await tryLogin(s);
      if (loggedIn) {
        ({ res, json } = await doRequest(s));
      }
      if (res && res.status === 401) {
        throw new Error('Login failed — check credentials, or run the server with AUTH_DISABLED=true.');
      }
    }
    return { res: res, json: json };
  }

  // Pull an error message out of an HTTP failure / {ok:false} body.
  function extractError(res, json, fallbackMsg) {
    if (json && json.error) return json.error;
    if (!res.ok) return (fallbackMsg || 'Request failed') + ' (HTTP ' + res.status + ')';
    return fallbackMsg || 'Request failed';
  }

  // --- High-level API surface ---------------------------------------------

  // { type:'status' }
  async function status() {
    try {
      const s = await getSettingsRaw();
      let authRequired = false, authed = false, connected = false;
      try {
        const st = await authStatus(s.baseUrl);
        connected = true;
        authRequired = st.auth_required;
        authed = st.authed;
        // If auth is required but we're not yet authed, try logging in so the
        // reported status reflects what protected calls will actually see.
        if (authRequired && !authed && s.username) {
          if (await tryLogin(s)) authed = true;
        }
      } catch (e) {
        // Unreachable: deliver the documented offline envelope (connected:false)
        // so popup applyStatus() and options testConnection() hit their dedicated
        // "Offline — can't reach Grokive" branches instead of a generic failure.
        return ok({
          connected: false,
          baseUrl: s.baseUrl,
          authRequired: false,
          authed: false,
          llmReady: false,
          embedReady: false,
          model: '',
          saveFolder: s.saveFolder
        });
      }

      // Prompt-studio / LLM readiness (best-effort; needs auth if required).
      let llmReady = false, embedReady = false, model = '';
      try {
        const ps = await withAuth(s, (ss) => getJson(ss.baseUrl, '/api/prompts/status'));
        if (ps.res.ok && ps.json) {
          llmReady = !!ps.json.llm_configured;
          embedReady = !!ps.json.embed_configured;
          model = ps.json.model || '';
        }
      } catch (e) {
        // Leave LLM fields false if status can't be read (e.g. login failed).
      }

      return ok({
        connected: connected,
        baseUrl: s.baseUrl,
        authRequired: authRequired,
        authed: authed,
        llmReady: llmReady,
        embedReady: embedReady,
        model: model,
        saveFolder: s.saveFolder
      });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'getResponses' } -> responses + folder summary (A-Z), unfiled, total
  async function getResponses() {
    try {
      const s = await getSettingsRaw();
      const { res, json } = await withAuth(s, (ss) => getJson(ss.baseUrl, '/api/prompts/responses'));
      if (!res.ok || !json) {
        return { ok: false, error: extractError(res, json, 'Could not load prompts') };
      }
      const responses = Array.isArray(json.responses) ? json.responses : [];

      const counts = new Map();
      let unfiled = 0;
      for (const r of responses) {
        const folder = (r && typeof r.folder === 'string') ? r.folder.trim() : '';
        if (!folder) { unfiled++; continue; }
        counts.set(folder, (counts.get(folder) || 0) + 1);
      }
      const folders = Array.from(counts.entries())
        .map(([name, count]) => ({ name: name, count: count }))
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));

      return ok({
        responses: responses,
        folders: folders,
        unfiled: unfiled,
        starred: responses.filter((r) => r && r.starred).length,
        total: responses.length
      });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'randomPrompt', folder } — background does the pick.
  // folder: '__all' | '__unfiled' | '__starred' | a folder name
  async function randomPrompt(folder) {
    try {
      const s = await getSettingsRaw();
      const sel = folder || s.sourceFolder || '__all';
      const { res, json } = await withAuth(s, (ss) => getJson(ss.baseUrl, '/api/prompts/responses'));
      if (!res.ok || !json) {
        return { ok: false, error: extractError(res, json, 'Could not load prompts') };
      }
      const all = Array.isArray(json.responses) ? json.responses : [];

      let pool;
      if (sel === '__all') {
        pool = all;
      } else if (sel === '__unfiled') {
        pool = all.filter((r) => !(r && typeof r.folder === 'string' && r.folder.trim()));
      } else if (sel === '__starred') {
        pool = all.filter((r) => r && r.starred);
      } else {
        pool = all.filter((r) => r && typeof r.folder === 'string' && r.folder.trim() === sel);
      }

      if (!pool.length) {
        return { ok: false, error: 'No prompts in that folder yet.' };
      }

      const picked = pool[Math.floor(Math.random() * pool.length)];
      return ok({
        prompt: {
          id: picked.id,
          text: picked.text || '',
          folder: (picked.folder || '').trim(),
          tags: Array.isArray(picked.tags) ? picked.tags : []
        },
        count: pool.length
      });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'enhance', prompt, dialogueLevel, dialogueOnly }
  async function enhance(prompt, dialogueLevel, dialogueOnly) {
    try {
      const s = await getSettingsRaw();
      const level = (['normal', 'dirtier', 'filthier'].indexOf(dialogueLevel) >= 0)
        ? dialogueLevel : s.dialogueLevel;
      const body = {
        prompt: prompt || '',
        dialogue_level: level,
        dialogue_only: !!dialogueOnly
      };
      const { res, json } = await withAuth(s, (ss) => postJson(ss.baseUrl, '/api/prompts/enhance', body));
      if (!res.ok || !json || json.ok === false) {
        return { ok: false, error: extractError(res, json, 'Enhance failed') };
      }
      return ok({ prompt: json.prompt || '' });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'generate', prompt, mode, n }
  async function generate(prompt, mode, n, instruction) {
    try {
      const s = await getSettingsRaw();
      const m = (['variations', 'remix', 'polish'].indexOf(mode) >= 0) ? mode : 'variations';
      let count = parseInt(n, 10);
      if (isNaN(count)) count = 4;
      count = Math.max(1, Math.min(8, count));
      if (m === 'polish') count = 1;
      const body = { prompt: prompt || '', mode: m, n: count };
      if (instruction) body.instruction = instruction;
      const { res, json } = await withAuth(s, (ss) => postJson(ss.baseUrl, '/api/prompts/generate', body));
      if (!res.ok || !json || json.ok === false) {
        return { ok: false, error: extractError(res, json, 'Generate failed') };
      }
      const variations = Array.isArray(json.variations) ? json.variations : [];
      return ok({ variations: variations });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'savePrompt', text, folder, starred } — folder defaults to settings.saveFolder.
  // Uses the server's append+dedupe endpoint so it never clobbers other items.
  async function savePrompt(text, folder, starred) {
    try {
      const s = await getSettingsRaw();
      const t = (text || '').trim();
      if (!t) return { ok: false, error: 'Nothing to save — the prompt is empty.' };
      const target = (folder && String(folder).trim()) ? String(folder).trim() : s.saveFolder;
      const body = { text: t, folder: target, starred: !!starred };
      const { res, json } = await withAuth(s, (ss) => postJson(ss.baseUrl, '/api/prompts/responses/add', body));
      if (!res.ok || !json || json.ok === false) {
        return { ok: false, error: extractError(res, json, 'Save failed') };
      }
      return ok({ added: !!json.added, folder: target, starred: !!json.starred });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'starPrompt', id, starred } — flip the starred flag on a saved prompt.
  // Uses the server's by-id endpoint so it never clobbers other items.
  async function starPrompt(id, starred) {
    try {
      const s = await getSettingsRaw();
      const pid = (id == null) ? '' : String(id).trim();
      if (!pid) return { ok: false, error: 'Nothing to star — missing prompt id.' };
      const body = { id: pid, starred: !!starred };
      const { res, json } = await withAuth(s, (ss) => postJson(ss.baseUrl, '/api/prompts/responses/star', body));
      if (!res.ok || !json || json.ok === false) {
        return { ok: false, error: extractError(res, json, 'Star failed') };
      }
      return ok({ starred: !!starred });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'copyToClipboard', text }
  // MV2 background is a real page with a document, so we can copy without a user
  // gesture using a hidden <textarea> + execCommand('copy') ("clipboardWrite").
  function copyToClipboard(text) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text == null ? '' : String(text);
      // Keep it off-screen and unfocusable-by-sight but selectable.
      ta.style.position = 'fixed';
      ta.style.top = '-9999px';
      ta.style.left = '-9999px';
      ta.setAttribute('readonly', '');
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      let copied = false;
      try {
        copied = document.execCommand('copy');
      } catch (e) {
        copied = false;
      }
      document.body.removeChild(ta);
      if (!copied) return { ok: false, error: 'Clipboard copy was blocked by the browser.' };
      return ok({ copied: true });
    } catch (e) {
      return fail(e);
    }
  }

  // --- Expose on the shared background global ------------------------------
  // background.js reads self.GrokiveAPI (see manifest background.scripts order).
  self.GrokiveAPI = {
    DEFAULTS: DEFAULTS,
    getSettings: getSettings,
    setSettings: setSettings,
    status: status,
    getResponses: getResponses,
    randomPrompt: randomPrompt,
    enhance: enhance,
    generate: generate,
    savePrompt: savePrompt,
    starPrompt: starPrompt,
    copyToClipboard: copyToClipboard,
    // Internal helper reused by the 'random-prompt' command in background.js:
    getSettingsRaw: getSettingsRaw
  };
})();
