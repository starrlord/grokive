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
  // Send the active chip selections so each facet's counts reflect the others
  // (e.g. selecting tags narrows the resolution/model chips). The server excludes
  // each facet's own dimension so its full option list stays visible.
  if (f.tags?.length) p.set('tags', f.tags.join(','));
  if (f.models?.length) p.set('models', f.models.join(','));
  if (f.resolutions?.length) p.set('res', f.resolutions.join(','));
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
  // Sanitize to a filesystem-safe stem; fall back to 'export' when the name is all
  // non-ASCII/symbols (e.g. a fully non-Latin prompt) so it can't collapse to '.mp4'.
  const stem = (name || 'export').replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '') || 'export';
  a.download = stem + '.mp4';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 15000);
}
export const exportPlaylist = (id, name) =>
  fetch(`/api/playlists/${encodeURIComponent(id)}/export`).then((r) => downloadBlob(r, name));
export const exportSelection = (ids, name = 'selection') =>
  fetch('/api/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids, name }) }).then((r) => downloadBlob(r, name));

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

// --- Collection locks -------------------------------------------------------
async function postLock(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || `Request failed (HTTP ${res.status}).`);
  return data;
}
export const lockCollection = (id, password) => postLock(`/api/collections/${encodeURIComponent(id)}/lock`, { password });
export const unlockCollection = (id, password) => postLock(`/api/collections/${encodeURIComponent(id)}/unlock`, { password });
export const relockCollection = (id) => postLock(`/api/collections/${encodeURIComponent(id)}/relock`, {});
export const relockAllCollections = () => postLock('/api/collections/relock-all', {});
export const removeCollectionLock = (id, password) => postLock(`/api/collections/${encodeURIComponent(id)}/remove-lock`, { password });
export const forceUnlockCollection = (id, adminPassword) => postLock(`/api/collections/${encodeURIComponent(id)}/force-unlock`, { admin_password: adminPassword });

// --- Agent canvases (rename / hard-delete; mutate the underlying media records) ---
export const renameCanvas = (id, name) => postLock(`/api/canvas/${encodeURIComponent(id)}/rename`, { name });
export const deleteCanvas = (id) => postLock(`/api/canvas/${encodeURIComponent(id)}/delete`, {});

// --- Folder import (upload a local folder into a new collection) ------------
// One file per request with byte-level upload progress (XHR — fetch can't report
// upload progress). Pass an AbortSignal to cancel an in-flight upload.
export function importFile(importId, file, { onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('import_id', importId);
    fd.append('file', file);
    fd.append('rel', file.webkitRelativePath || file.name);
    fd.append('mtime', String(file.lastModified || 0));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/import/file');
    if (onProgress) xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* non-JSON */ }
      if (xhr.status >= 200 && xhr.status < 300 && data.ok) resolve(data);
      else reject(new Error(data.error || `Upload failed (HTTP ${xhr.status}).`));
    };
    xhr.onerror = () => reject(new Error('Network error during upload.'));
    xhr.onabort = () => reject(new DOMException('Aborted', 'AbortError'));
    if (signal) {
      if (signal.aborted) { xhr.abort(); return; }
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }
    xhr.send(fd);
  });
}
export async function importCommit(importId, { name, collectionId } = {}) {
  const res = await fetch('/api/import/commit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ import_id: importId, name, collection_id: collectionId })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) throw new Error(data.error || `Import failed (HTTP ${res.status}).`);
  return data; // { ok, collection_id, name, count }
}
export function importCancel(importId) {
  return fetch('/api/import/cancel', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ import_id: importId })
  }).catch(() => {});
}

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

// --- Library stats ---------------------------------------------------------
// { videos, images, total, bytes } — whole-library totals for the Stats panel.
export const getStats = () => getJSON('/api/stats');

// --- Config + settings -----------------------------------------------------
export const getConfig = () => getJSON('/api/config');
export const postConfig = (curlText) =>
  fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: curlText });
export const getSettings = () => getJSON('/api/settings');
export const postSettings = (body) =>
  fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
export async function fetchProviderModels(body) {
  const res = await fetch('/api/settings/models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Could not load models.');
  return d;
}

// --- Backup / Restore ------------------------------------------------------
// A portable .zip of the durable state (metadata, library, collections, playlists,
// Prompt Studio data, settings). secrets=true also bundles the API keys + Grok
// session. Streams the file to the browser as a download, honouring the server's
// timestamped Content-Disposition filename.
export async function exportBackup(includeSecrets = false) {
  const res = await fetch(`/api/backup/export?secrets=${includeSecrets ? 1 : 0}`);
  if (!res.ok) {
    let msg = 'Backup failed.';
    try { const j = await res.json(); if (j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  const blob = await res.blob();
  let name = 'grokive-backup.zip';
  const m = /filename="?([^";]+)"?/.exec(res.headers.get('Content-Disposition') || '');
  if (m) name = m[1];
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 15000);
}
// Replace the live state with the contents of a backup .zip (destructive — the
// server snapshots the prior state into /data/backups first). Returns the summary.
export async function restoreBackup(file) {
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch('/api/backup/import', { method: 'POST', body: fd });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || `Restore failed (HTTP ${res.status}).`);
  return d; // { ok, restored: string[], includes_secrets, counts }
}

// --- Grok Imagine (xAI) generation -----------------------------------------
// Image generation is synchronous (returns the ingested gallery records). Video
// is async: start a job, then poll status until it finishes.
export async function generateImage(body) {
  const res = await fetch('/api/imagine/image', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || 'Image generation failed.');
  return d; // { ok, session_id, generations: [record, ...] }
}
export async function startImagineVideo(body) {
  const res = await fetch('/api/imagine/video', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || 'Video generation failed to start.');
  return d; // { ok, job_id }
}
export const imagineVideoStatus = (sessionId = '') =>
  getJSON(`/api/imagine/video/status?session=${encodeURIComponent(sessionId)}`);
export const imagineJobsAll = () => getJSON('/api/imagine/jobs');
export const ackImagineVideo = (sessionId = '') =>
  fetch('/api/imagine/video/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sessionId }) }).catch(() => {});
// Staging workspaces: per-session histories of generations; nothing reaches the
// gallery until saveImagineGen() promotes one. Load by session id (scratch, ws:*,
// src:*); list all sessions for the workspace switcher.
export const getImagineSession = (sessionId = '') =>
  getJSON(`/api/imagine/session?id=${encodeURIComponent(sessionId)}`);
export const getImagineSessions = () => getJSON('/api/imagine/sessions');
export async function saveImagineGen(genId) {
  const res = await fetch('/api/imagine/save', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ gen_id: genId })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || 'Save failed.');
  return d; // { ok, item, already? }
}
export async function clearImagineSession(sessionId) {
  const res = await fetch('/api/imagine/session/clear', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sessionId })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || 'Clear failed.');
  return d;
}
export async function discardImagineGen(genId) {
  const res = await fetch('/api/imagine/discard', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ gen_id: genId })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || 'Delete failed.');
  return d;
}
export async function uploadImagineImage(sessionId, file) {
  const fd = new FormData();
  fd.append('session_id', sessionId);
  fd.append('file', file);
  const res = await fetch('/api/imagine/upload', { method: 'POST', body: fd });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || 'Upload failed.');
  return d; // { ok, session_id, generation }
}

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

// Vision — describe an image (by media id) as a ready-to-paste Grok Imagine prompt.
// Needs a multimodal model configured as the vision model in Config → Prompt Studio AI.
export async function describeImage(id) {
  const res = await fetch('/api/prompts/from-image', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Could not describe the image.');
  return d; // { prompt, model }
}

// Enhance one saved prompt into a more descriptive prompt; optionally rewrite only quoted dialogue.
export async function enhancePrompt(prompt, { dialogue_level = 'normal', dialogue_only = false } = {}) {
  const res = await fetch('/api/prompts/enhance', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, dialogue_level, dialogue_only })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Enhance failed.');
  return d; // { prompt, dialogue_level, dialogue_only, model }
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
// Append ONE saved response server-side (read-modify-write) and get the full list back. Use
// this from any context that hasn't loaded the full list (e.g. the lightbox) — the full-list
// POST above would otherwise overwrite the server with whatever the client happens to hold.
export async function addSavedResponseRemote(text, folder = '', starred = false) {
  const res = await fetch('/api/prompts/responses/add', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, folder, starred })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Could not save.');
  return d; // { ok, added, responses }
}
// Star / unstar ONE saved response by id (server read-modify-write) and get the full list back.
// By-id so it never clobbers the user's other saved prompts the way a full-list POST would.
export async function starResponseRemote(id, starred) {
  const res = await fetch('/api/prompts/responses/star', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, starred })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Could not save.');
  return d; // { ok, responses }
}
// Merge media-library prompts (metadata.json) into Saved, server-side: backed up first, deduped,
// can only grow the list. preview:true returns only counts ({missing, library_unique, saved});
// otherwise it imports and returns the full updated list ({added, total, backup, responses}).
export async function importLibraryPrompts({ preview = false, folder } = {}) {
  const res = await fetch('/api/prompts/responses/import-library', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preview, ...(folder ? { folder } : {}) })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || 'Import failed.');
  return d;
}

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
