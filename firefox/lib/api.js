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

  // --- Saved-prompt library cache -----------------------------------------
  // /api/prompts/responses hands back the WHOLE library in one shot (there is no
  // server-side search for saved prompts — the web UI filters client-side too).
  // Search therefore runs HERE, over a short-lived cache, so a keystroke never
  // refetches the library and only the matched page crosses the sendMessage
  // channel. Every write path refreshes the cache from its own response (those
  // endpoints return the full updated list), so a save is visible immediately.
  const RESPONSES_TTL_MS = 60 * 1000;
  let responsesCache = null;      // { at: <epoch ms>, list: [...] }
  let responsesInflight = null;   // single-flight guard, see loadResponses

  function cacheResponses(list) {
    responsesCache = { at: Date.now(), list: Array.isArray(list) ? list : [] };
  }

  // Refresh the cache from a write endpoint's echoed list, or drop it if absent.
  function syncResponsesCache(json) {
    if (json && Array.isArray(json.responses)) cacheResponses(json.responses);
    else responsesCache = null;
  }

  // Load the library, from cache when it's fresh. `force` skips the TTL — the source
  // picker's ↻ uses it to pull in changes the extension can't observe (a star or a
  // delete made in the web UI, autonomous tagging, an import).
  //
  // SINGLE-FLIGHT: a miss parks every concurrent caller on ONE promise. Without it a
  // burst — spamming 🎲, or the popup's folder refresh racing a roll — pulls the whole
  // multi-MB library down once per caller.
  async function loadResponses(s, force) {
    if (!force && responsesCache && (Date.now() - responsesCache.at) < RESPONSES_TTL_MS) {
      return responsesCache.list;
    }
    if (responsesInflight) return responsesInflight;   // already fetching: ride along
    responsesInflight = (async () => {
      const { res, json } = await withAuth(s, (ss) => getJson(ss.baseUrl, '/api/prompts/responses'));
      if (!res.ok || !json) throw new Error(extractError(res, json, 'Could not load prompts'));
      const list = Array.isArray(json.responses) ? json.responses : [];
      cacheResponses(list);
      return list;
    })();
    try {
      return await responsesInflight;
    } finally {
      responsesInflight = null;   // clear on failure too, or one error wedges every reader
    }
  }

  // { type:'getResponses', refresh? } -> folder summary (A-Z), unfiled, starred, total
  //
  // Deliberately does NOT return the library itself. It used to, and nothing read it —
  // the popup and the toolbar's source picker only ever want the folder counts, so the
  // whole multi-MB list was being structured-cloned across sendMessage for nothing.
  // Callers that need prompt text use searchPrompts (paged + preview-capped) or getPrompt.
  async function getResponses(opts) {
    try {
      const s = await getSettingsRaw();
      const responses = await loadResponses(s, !!(opts && opts.refresh));

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
        folders: folders,
        unfiled: unfiled,
        starred: responses.filter((r) => r && r.starred).length,
        total: responses.length
      });
    } catch (e) {
      return fail(e);
    }
  }

  // --- Search --------------------------------------------------------------
  // Multi-token AND match, order-independent: every whitespace-separated term must
  // appear somewhere in the prompt's text, its folder name, or one of its tags. So
  // "beach sunset" finds "sunset on the beach", and "poses tall" finds a tall-subject
  // prompt filed under Poses.
  const SEARCH_TERM_MAX = 8;        // ignore silly-long queries rather than scanning 50x
  const SEARCH_PAGE_MAX = 50;       // hard cap on rows per page (payload guard)
  const SEARCH_TEXT_PREVIEW = 4000; // per-row text cap; the server allows 100K per prompt

  function searchTerms(q) {
    return String(q == null ? '' : q).toLowerCase().split(/\s+/).filter(Boolean).slice(0, SEARCH_TERM_MAX);
  }

  // One flat string per record. Terms are whitespace-split, so none can contain a
  // space — the joining spaces are enough to stop a match spanning two fields.
  function searchHaystack(r) {
    const tags = Array.isArray(r.tags) ? r.tags.join(' ') : '';
    return ((r.text || '') + ' ' + (r.folder || '') + ' ' + tags).toLowerCase();
  }

  function inFolderSelection(r, sel) {
    const folder = (r && typeof r.folder === 'string') ? r.folder.trim() : '';
    if (sel === '__all') return true;
    if (sel === '__unfiled') return !folder;
    if (sel === '__starred') return !!(r && r.starred);
    return folder === sel;
  }

  // A row as the UI sees it: full text unless it's enormous, in which case it comes
  // back preview-capped with `truncated` set (getPrompt/copyPrompt resolve the rest).
  function searchRow(r) {
    const text = String(r.text || '');
    const capped = text.length > SEARCH_TEXT_PREVIEW;
    return {
      id: String(r.id == null ? '' : r.id),
      text: capped ? text.slice(0, SEARCH_TEXT_PREVIEW) : text,
      truncated: capped,
      length: text.length,
      folder: (r.folder || '').trim(),
      tags: Array.isArray(r.tags) ? r.tags : [],
      starred: !!r.starred
    };
  }

  // { type:'searchPrompts', query, folder, offset, limit }
  // -> { results, total, offset, limit, terms, library }
  // Library order is preserved (newest-first, same as the web UI's list).
  async function searchPrompts(query, opts) {
    try {
      const s = await getSettingsRaw();
      const o = opts || {};
      const list = await loadResponses(s);
      const sel = o.folder || '__all';
      const terms = searchTerms(query);

      const matches = [];
      for (const r of list) {
        if (!r || typeof r !== 'object') continue;
        if (!inFolderSelection(r, sel)) continue;
        if (terms.length) {
          const hay = searchHaystack(r);
          let all = true;
          for (const t of terms) {
            if (hay.indexOf(t) < 0) { all = false; break; }
          }
          if (!all) continue;
        }
        matches.push(r);
      }

      let offset = parseInt(o.offset, 10);
      if (isNaN(offset) || offset < 0) offset = 0;
      let limit = parseInt(o.limit, 10);
      if (isNaN(limit) || limit <= 0) limit = 25;
      limit = Math.min(limit, SEARCH_PAGE_MAX);

      return ok({
        results: matches.slice(offset, offset + limit).map(searchRow),
        total: matches.length,
        offset: offset,
        limit: limit,
        terms: terms,
        library: list.length
      });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'getPrompt', id } -> the FULL record (search rows are preview-capped).
  async function getPrompt(id) {
    try {
      const s = await getSettingsRaw();
      const pid = (id == null) ? '' : String(id).trim();
      if (!pid) return { ok: false, error: 'Missing prompt id.' };
      const list = await loadResponses(s);
      const r = list.find((x) => x && String(x.id) === pid);
      if (!r) return { ok: false, error: 'That prompt is no longer in your library.' };
      // Deliberately NOT searchRow(): this is the uncapped-text escape hatch.
      return ok({
        prompt: {
          id: pid,
          text: String(r.text || ''),
          folder: (r.folder || '').trim(),
          tags: Array.isArray(r.tags) ? r.tags : [],
          starred: !!r.starred
        }
      });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'copyPrompt', id, text } — copy ONE saved prompt's full text in a single
  // round trip: resolve the untruncated text by id, falling back to the row text the
  // caller already holds if the lookup fails (e.g. deleted since the search ran).
  async function copyPrompt(id, text) {
    try {
      let full = (text == null) ? '' : String(text);
      const pid = (id == null) ? '' : String(id).trim();
      if (pid) {
        const got = await getPrompt(pid);
        if (got.ok && got.data && got.data.prompt) full = got.data.prompt.text;
      }
      if (!full.trim()) return { ok: false, error: 'Nothing to copy.' };
      return copyToClipboard(full);
    } catch (e) {
      return fail(e);
    }
  }

  // Human name for a source sentinel. Shared with background.js's hotkey notification;
  // content/grok.js keeps its own copy (content scripts can't reach this file).
  function sourceLabel(sel) {
    if (sel === '__starred') return '★ Starred';
    if (sel === '__unfiled') return 'Unfiled';
    if (!sel || sel === '__all') return 'All prompts';
    return sel;
  }

  // Nothing to draw from — say WHICH pool is empty, so an empty ★ Starred doesn't
  // read as "your library is empty".
  function emptyPoolError(sel) {
    if (sel === '__starred') return 'No starred prompts yet — ★ some in Grokive first.';
    if (sel === '__unfiled') return 'No unfiled prompts — everything is in a folder.';
    if (!sel || sel === '__all') return 'Your prompt library is empty.';
    return 'No prompts in "' + sel + '" yet.';
  }

  // { type:'randomPrompt', folder } — background does the pick.
  // folder: '__all' | '__unfiled' | '__starred' | a folder name
  //
  // Reads the SHARED cache rather than pulling the library per roll: rolling is the one
  // action people repeat, and the payload is multi-MB. A roll now also warms the cache
  // that search/getPrompt read, and vice versa. Worst case that costs a roll a snapshot
  // up to RESPONSES_TTL_MS old — the source picker's ↻ forces a refresh when it matters.
  async function randomPrompt(folder) {
    try {
      const s = await getSettingsRaw();
      const sel = folder || s.sourceFolder || '__all';
      const all = await loadResponses(s);

      // Always filter() — never alias loadResponses' array, which IS the cache's list.
      // inFolderSelection is the same predicate search scopes by, so the two agree.
      const pool = all.filter((r) => r && typeof r === 'object' && inFolderSelection(r, sel));
      if (!pool.length) {
        return { ok: false, error: emptyPoolError(sel) };
      }

      const picked = pool[Math.floor(Math.random() * pool.length)];
      return ok({
        prompt: {
          id: picked.id,
          text: picked.text || '',
          folder: (picked.folder || '').trim(),
          tags: Array.isArray(picked.tags) ? picked.tags : []
        },
        count: pool.length,
        source: sel,
        sourceLabel: sourceLabel(sel)
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
        syncResponsesCache(json);   // a rejection means our view is suspect — refresh it or drop it
        return { ok: false, error: extractError(res, json, 'Save failed') };
      }
      syncResponsesCache(json);   // the endpoint echoes the full list — keep search current
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
        // The 404 "Unknown prompt id" body carries the authoritative list. Taking it here
        // evicts the phantom row; without this the stale row survives the whole TTL and
        // every retry of ★ reproduces the same error.
        syncResponsesCache(json);
        return { ok: false, error: extractError(res, json, 'Star failed') };
      }
      syncResponsesCache(json);   // ★ flips must show up in the next search
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

  // --- Collections + reference images -------------------------------------
  // The grok.com toolbar's "References" panel browses your saved collections and
  // copies a raw image to the clipboard so you can paste it into Grok Imagine as
  // a reference image. As with everything else, the network lives HERE in the
  // background: the content script can't fetch the Grokive server directly (CORS),
  // and a cross-site <img> from grok.com would drop the Lax session cookie when
  // auth is on — so even image bytes are proxied through here, where the cookie
  // and login-retry already work.

  // { type:'getCollections' } -> [{ id, name, cover, imageCount, itemCount, locked, unlocked }]
  // Order is preserved (the server's collections.json order, same as the web UI).
  async function getCollections() {
    try {
      const s = await getSettingsRaw();
      const { res, json } = await withAuth(s, (ss) => getJson(ss.baseUrl, '/api/collections'));
      if (!res.ok || !json) {
        return { ok: false, error: extractError(res, json, 'Could not load collections') };
      }
      const raw = Array.isArray(json.collections) ? json.collections : [];
      const collections = raw
        .map((c) => ({
          id: String(c && c.id != null ? c.id : ''),
          name: String((c && c.name) || 'Untitled'),
          // Summaries give a chosen cover plus a few recent covers; take the first.
          cover: (c && (c.cover || (Array.isArray(c.covers) ? c.covers[0] : null))) || null,
          imageCount: (c && typeof c.image_count === 'number') ? c.image_count : null,
          itemCount: (c && typeof c.item_count === 'number') ? c.item_count : null,
          locked: !!(c && c.locked),
          unlocked: !!(c && c.unlocked)
        }))
        .filter((c) => c.id);
      return ok({ collections: collections });
    } catch (e) {
      return fail(e);
    }
  }

  // { type:'getCollectionImages', collectionId } -> [{ id, thumb, href, prompt, w, h }]
  // Images only (videos can't be reference images). A locked-and-not-unlocked
  // collection comes back empty from the server — the caller surfaces that.
  async function getCollectionImages(collectionId) {
    try {
      const s = await getSettingsRaw();
      const cid = (collectionId == null) ? '' : String(collectionId).trim();
      if (!cid) return { ok: false, error: 'Missing collection id.' };
      // view=collections matches the web UI's collection browser: it bypasses the
      // "hide archived/stashed from Recent" rule (the default `recent` view would
      // hide reference images the user has archived, making the collection look empty).
      const path = '/api/media?view=collections&type=image&sort=new&page_size=300&collection=' + encodeURIComponent(cid);
      const { res, json } = await withAuth(s, (ss) => getJson(ss.baseUrl, path));
      if (!res.ok || !json) {
        return { ok: false, error: extractError(res, json, 'Could not load images') };
      }
      const items = Array.isArray(json.items) ? json.items : [];
      const images = [];
      for (const it of items) {
        if (!it || it.media_type !== 'image' || !it.href) continue;
        images.push({
          id: String(it.id == null ? '' : it.id),
          thumb: it.thumb || it.href,   // fall back to the full image if no thumb
          href: it.href,
          prompt: it.prompt || '',
          w: it.thumb_w || it.media_w || null,
          h: it.thumb_h || it.media_h || null
        });
      }
      return ok({ images: images, total: (typeof json.total === 'number') ? json.total : images.length });
    } catch (e) {
      return fail(e);
    }
  }

  // Defense-in-depth: only ever fetch the Grokive server's OWN media/thumbnail
  // routes, so this proxy can't be coerced into fetching an arbitrary URL.
  function isAllowedMediaPath(p) {
    return /^\/(media|thumbnails)\//.test(p);
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      try {
        const fr = new FileReader();
        fr.onload = () => resolve(fr.result);
        fr.onerror = () => reject(new Error('Could not read the image bytes.'));
        fr.readAsDataURL(blob);
      } catch (e) {
        reject(e);
      }
    });
  }

  // { type:'fetchImageData', href } -> { dataUrl, type }
  // Fetch a /media or /thumbnails resource here (auth cookie + credentials ride
  // along; no CORS; no cross-site SameSite drop) and hand it back as a data: URL.
  // The content script renders that directly in an <img> or turns it into a Blob
  // for the clipboard. A data: URL is same-origin to the page, so the canvas it's
  // later drawn onto (to normalise to PNG) is NOT tainted.
  async function fetchImageData(href) {
    try {
      const s = await getSettingsRaw();
      let p = (href == null) ? '' : String(href).trim();
      if (!p) return { ok: false, error: 'Missing image path.' };
      if (!p.startsWith('/')) p = '/' + p;            // stored paths are relative
      if (!isAllowedMediaPath(p)) {
        return { ok: false, error: 'Refusing to fetch a non-media path.' };
      }
      // Prime auth so the (auth-gated) media route sees the session cookie.
      const authErr = await ensureAuth(s);
      if (authErr) return { ok: false, error: authErr };

      let res = await rawFetch(s.baseUrl, p, { method: 'GET' });
      if (res.status === 401) {
        const loggedIn = await tryLogin(s);
        if (loggedIn) res = await rawFetch(s.baseUrl, p, { method: 'GET' });
        if (res && res.status === 401) {
          // Mirror withAuth's clearer message instead of a bare HTTP 401.
          return { ok: false, error: 'Login failed — check credentials, or run the server with AUTH_DISABLED=true.' };
        }
      }
      if (!res.ok) {
        return { ok: false, error: 'Image fetch failed (HTTP ' + res.status + ').' };
      }
      const blob = await res.blob();
      if (!blob || !blob.size) return { ok: false, error: 'The image came back empty.' };
      // Bound the messaging channel: the bytes cross runtime.sendMessage as a base64
      // data: URL (~33% larger). Match the server's own 30 MB upload ceiling.
      if (blob.size > 30 * 1024 * 1024) {
        return { ok: false, error: 'Image is too large to copy (' + Math.round(blob.size / (1024 * 1024)) + ' MB).' };
      }
      const dataUrl = await blobToDataUrl(blob);
      if (typeof dataUrl !== 'string' || dataUrl.indexOf('data:') !== 0) {
        return { ok: false, error: 'Could not read the image bytes.' };
      }
      return ok({ dataUrl: dataUrl, type: blob.type || '' });
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
    searchPrompts: searchPrompts,
    getPrompt: getPrompt,
    copyPrompt: copyPrompt,
    randomPrompt: randomPrompt,
    enhance: enhance,
    generate: generate,
    savePrompt: savePrompt,
    starPrompt: starPrompt,
    getCollections: getCollections,
    getCollectionImages: getCollectionImages,
    fetchImageData: fetchImageData,
    copyToClipboard: copyToClipboard,
    // Internal helpers reused by the 'random-prompt' command in background.js:
    getSettingsRaw: getSettingsRaw,
    sourceLabel: sourceLabel
  };
})();
