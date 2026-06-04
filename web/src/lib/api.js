// Thin client for the Flask JSON API (same origin).

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
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

export async function fetchLibrary() {
  try { return await getJSON('/api/library'); } catch { return { favorites: [], stashed: [] }; }
}
export const saveLibrary = (library) =>
  fetch('/api/library', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(library) }).catch(() => {});

// --- Playlists -------------------------------------------------------------
export async function fetchPlaylists() {
  try { return (await getJSON('/api/playlists')).playlists || []; } catch { return []; }
}
export const savePlaylists = (playlists) =>
  fetch('/api/playlists', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ playlists }) }).catch(() => {});

// --- Collections -----------------------------------------------------------
export async function fetchCollections() {
  try { return (await getJSON('/api/collections')).collections || []; } catch { return []; }
}
export const saveCollections = (collections) =>
  fetch('/api/collections', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ collections }) }).catch(() => {});

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
