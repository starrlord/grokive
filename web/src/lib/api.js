// Thin client for the Flask JSON API (same origin).
import { toast } from '$lib/toast.js';

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// Fire-and-forget persistence (favorites / playlists / collections). Callers don't
// await these, so a silent failure would diverge the client from the server with no
// sign. Surface a toast on failure instead of swallowing — the local store keeps the
// optimistic change, but the user is told it didn't persist.
function saveJSON(url, body) {
  return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then((r) => { if (!r.ok) throw new Error(String(r.status)); })
    .catch(() => toast("Couldn't save your change — it may not stick. Check your connection.", { type: 'error' }));
}

export function fetchMedia(f, page = 1, pageSize = 120, collectionId = null) {
  const p = new URLSearchParams();
  p.set('view', f.view || 'recent');
  if (f.query) p.set('q', f.query);
  if (f.tags?.length) p.set('tags', f.tags.join(','));
  if (f.models?.length) p.set('models', f.models.join(','));
  if (f.resolutions?.length) p.set('res', f.resolutions.join(','));
  if (f.canvas) p.set('canvas', f.canvas);
  if (f.mediaType && f.mediaType !== 'all') p.set('type', f.mediaType);
  if (f.period && f.period !== 'all') p.set('period', f.period);
  if (collectionId) p.set('collection', collectionId);
  p.set('sort', f.sort || 'new');
  p.set('page', String(page));
  p.set('page_size', String(pageSize));
  return getJSON(`/api/media?${p.toString()}`);
}

export function fetchFacets(f = {}, collectionId = null) {
  const p = new URLSearchParams();
  p.set('view', f.view || 'recent');
  if (f.query) p.set('q', f.query);
  if (f.canvas) p.set('canvas', f.canvas);
  if (f.mediaType && f.mediaType !== 'all') p.set('type', f.mediaType);
  if (f.period && f.period !== 'all') p.set('period', f.period);
  if (collectionId) p.set('collection', collectionId);
  return getJSON(`/api/facets?${p.toString()}`);
}

export async function mediaByIds(ids) {
  if (!ids?.length) return [];
  const res = await fetch('/api/media/by-ids', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids })
  });
  if (!res.ok) return [];
  return (await res.json()).items || [];
}

export async function mediaRelated(id) {
  if (!id) return { base: null, generated: [] };
  return getJSON(`/api/media/related?id=${encodeURIComponent(id)}`).catch(() => ({ base: null, generated: [] }));
}

export async function fetchLibrary() {
  try { return await getJSON('/api/library'); } catch { return { favorites: [], stashed: [] }; }
}
export const saveLibrary = (library) => saveJSON('/api/library', library);

// --- Playlists -------------------------------------------------------------
export async function fetchPlaylists() {
  try { return (await getJSON('/api/playlists')).playlists || []; } catch { return []; }
}
export const savePlaylists = (playlists) => saveJSON('/api/playlists', { playlists });

// --- Collections -----------------------------------------------------------
export async function fetchCollections() {
  try { return (await getJSON('/api/collections')).collections || []; } catch { return []; }
}
export const saveCollections = (collections) => saveJSON('/api/collections', { collections });

// --- Export (streamed MP4 download) ----------------------------------------
async function downloadBlob(response, name) {
  if (!response.ok) {
    let msg = 'Export failed.';
    try { const j = await response.json(); if (j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = (name || 'export').replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '') + '.mp4';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 15000);
}
export const exportPlaylist = (id, name) =>
  fetch(`/api/playlists/${encodeURIComponent(id)}/export`).then((r) => downloadBlob(r, name));
export const exportSelection = (ids) =>
  fetch('/api/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids, name: 'selection' }) }).then((r) => downloadBlob(r, 'selection'));

// --- Delete (hard-delete + blocklist) --------------------------------------
export const deleteMedia = (ids) =>
  fetch('/api/media/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids })
  }).then(async (r) => {
    if (!r.ok) {
      let msg = 'Delete failed.';
      try { const j = await r.json(); if (j.error) msg = j.error; } catch {}
      throw new Error(msg);
    }
    return r.json();
  });

// --- Generate Movie (beat-synced montage; own background job slot) ----------
export async function generateMovie({ ids, song, options }) {
  const fd = new FormData();
  fd.append('video_ids', JSON.stringify(ids || []));
  fd.append('song', song);
  for (const [k, v] of Object.entries(options || {})) {
    if (v !== undefined && v !== null && v !== '') fd.append(k, String(v));
  }
  const res = await fetch('/api/movie/generate', { method: 'POST', body: fd });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.error || `Could not start movie generation (HTTP ${res.status}).`);
  // A success without a job_id means we didn't reach the movie API (e.g. an older
  // server build serving the SPA shell). Make that legible instead of "starting" forever.
  if (!data || !data.job_id) throw new Error('Unexpected server response — is this server running the latest build?');
  return data; // { ok, job_id }
}
export const movieStatus = () => getJSON('/api/movie/status');
// Durably tell the server the finished montage was dealt with, so the floating
// chip stays hidden across reloads. Fire-and-forget — the local ack hides it now.
export async function dismissMovie() {
  try { await fetch('/api/movie/dismiss', { method: 'POST' }); } catch { /* best-effort */ }
}
export async function commitMovie() {
  const res = await fetch('/api/movie/commit', { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || 'Could not add the movie to your gallery.');
  return data; // { ok, id, collection_id, already? }
}
// `v` (the job id) busts the browser cache — the result path is otherwise the
// same URL for every render, so a new movie would re-serve the cached old one.
export function movieResultUrl(download = false, v = '') {
  const p = new URLSearchParams();
  if (download) p.set('download', '1');
  if (v) p.set('v', v);
  const qs = p.toString();
  return `/api/movie/result${qs ? `?${qs}` : ''}`;
}

// --- Jobs (sync + subtitles share one slot) --------------------------------
export const startSync = () => fetch('/api/sync', { method: 'POST' });
export const startSubtitles = () => fetch('/api/subtitles', { method: 'POST' });
export const syncStatus = () => getJSON('/api/sync/status');

// --- Config + settings -----------------------------------------------------
export const getConfig = () => getJSON('/api/config');
export const postConfig = (curlText) =>
  fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: curlText });
export const getSettings = () => getJSON('/api/settings');
export const postSettings = (body) =>
  fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

// --- Prompt Studio (Phase 0: corpus vocabulary + structured composer) -------
export async function fetchPromptVocabulary() {
  try { return await getJSON('/api/prompts/vocabulary'); }
  catch { return { total_prompts: 0, unique_prompts: 0, slots: [], prompts: [] }; }
}
async function postJSON(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return res.ok ? res.json() : null;
}
// Two-stage Grok Imagine prompts: { image } (detailed base frame) + { motion } (short animate).
export const composePrompts = (components) =>
  postJSON('/api/prompts/compose', { components }).then((d) => ({ image: d?.image ?? '', motion: d?.motion ?? '' }));
export const parsePrompt = (text, llm = false) =>
  postJSON('/api/prompts/parse', { text, llm }).then((d) => (d?.components ?? {}));

// Phase 1 — local embeddings: semantic search + auto theme clusters.
export const promptEmbedStatus = () => getJSON('/api/prompts/status').catch(() => ({ embed_configured: false }));
export const startPromptEmbed = () =>
  fetch('/api/prompts/embed', { method: 'POST' }).then((r) => r.json()).catch(() => ({}));
export const fetchPromptThemes = (k) =>
  getJSON(`/api/prompts/themes${k ? `?k=${k}` : ''}`).catch(() => ({ themes: [] }));
export function similarPrompts({ id, text, k = 30 } = {}) {
  const p = new URLSearchParams();
  if (id) p.set('id', id);
  if (text) p.set('text', text);
  p.set('k', String(k));
  return getJSON(`/api/prompts/similar?${p.toString()}`).catch(() => ({ results: [] }));
}

// Phase 2 — local LLM: prompt variations / remix / polish.
export async function generatePrompts(body) {
  const res = await fetch('/api/prompts/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Generation failed.');
  return d; // { variations: string[], model }
}

// Auto-tag — suggest a folder + tags for one saved prompt via the local LLM. Pass the labels
// already in use so the model reuses them instead of inventing near-duplicates.
export async function autotagPrompt(text, { folders = [], tags = [] } = {}) {
  const res = await fetch('/api/prompts/autotag', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, folders, tags })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Auto-tag failed.');
  return d; // { folder, tags: string[], model }
}

// Audit labels — suggest corrections for prompts that already have folders/tags.
export async function auditPromptLabels(text, { folder = '', current_tags = [], folders = [], tags = [] } = {}) {
  const res = await fetch('/api/prompts/audit-labels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, folder, current_tags, folders, tags })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Label audit failed.');
  return d; // { folder, tags: string[], remove_tags: string[], reason, model }
}

// Scene Builder — script a continuous multi-clip scene (length ÷ 6s/10s increment → beats).
export async function generateScene(body) {
  const res = await fetch('/api/prompts/scene', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Scene generation failed.');
  return d; // { beats: string[], clips, increment, length_seconds }
}

// Freeform — direct, unconstrained generation in the active persona's voice (numbered list).
export async function generateFreeform(body) {
  const res = await fetch('/api/prompts/freeform', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Generation failed.');
  return d; // { items: string[], model }
}

// Freeform presets — saved request + required repeated text/prefix.
export async function fetchFreeformPresets() {
  try { return (await getJSON('/api/prompts/freeform-presets')).presets || []; } catch { return []; }
}
export const saveFreeformPresets = (presets) => saveJSON('/api/prompts/freeform-presets', { presets });

// Saved scenes (durable, server-side, shared across devices).
export async function fetchScenes() {
  try { return (await getJSON('/api/prompts/scenes')).scenes || []; } catch { return []; }
}
export const saveScenes = (scenes) => saveJSON('/api/prompts/scenes', { scenes });

// Saved responses — starred Prompt Studio outputs (durable, server-side).
export async function fetchSavedResponses() {
  try { return (await getJSON('/api/prompts/responses')).responses || []; } catch { return []; }
}
export const saveSavedResponses = (responses) => saveJSON('/api/prompts/responses', { responses });

// Persona cards (durable, server-side, shared across devices). Returns null when the GET FAILS
// (so callers don't mistake an unreachable server for a genuinely empty list and overwrite it);
// returns [] only when the server really has no cards.
export async function fetchPersonas() {
  try { return (await getJSON('/api/prompts/personas')).personas || []; } catch { return null; }
}
export const savePersonas = (personas) => saveJSON('/api/prompts/personas', { personas });

// --- Auth ------------------------------------------------------------------
export async function authStatus() {
  try {
    return await getJSON('/api/auth/status');
  } catch {
    return { auth_required: false, authed: true };
  }
}
export async function login(username, password) {
  const res = await fetch('/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password })
  });
  if (res.ok) return { ok: true };
  const data = await res.json().catch(() => ({}));
  return { ok: false, error: data.error || 'Login failed.' };
}
export const logout = () => fetch('/api/logout', { method: 'POST' }).catch(() => {});
