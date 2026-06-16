import { writable, derived, get } from 'svelte/store';
import { SvelteSet } from 'svelte/reactivity';
import {
  saveLibrary, fetchPlaylists, savePlaylists,
  fetchCollections, saveCollections,
  getSettings, deleteMedia, movieStatus, dismissMovie,
  fetchSavedResponses, saveSavedResponses, addSavedResponseRemote, importLibraryPrompts,
  getImagineSessions, imagineJobsAll
} from './api.js';
import { toast } from './toast.js';

const LS = (key, fallback) => {
  if (typeof localStorage === 'undefined') return fallback;
  try {
    const v = localStorage.getItem(key);
    return v == null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
};
const persist = (key, value) => {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
};

// --- Display preferences (persisted) ---------------------------------------
export const THEMES = [
  { id: 'violet', label: 'Violet', preview: ['#15131c', '#211f2b', '#8b5cf6', '#4bb3a8'] },
  { id: 'aurora', label: 'Obsidian Aurora', preview: ['#070b13', '#101827', '#22d3ee', '#84cc16'] },
  { id: 'cobalt', label: 'Cobalt Mirage', preview: ['#070a1b', '#1c2543', '#4367ff', '#ffd95a'] },
  { id: 'nocturne', label: 'Neon Nocturne', preview: ['#090817', '#191431', '#7c5cff', '#e879f9'] },
  { id: 'graphite', label: 'Graphite Atelier', preview: ['#090a0c', '#14161a', '#7dd3c7', '#f3d6a3'] },
  { id: 'rainforest', label: 'Rainforest Noir', preview: ['#06100d', '#10201a', '#34d399', '#c084fc'] },
  { id: 'ember', label: 'Ember Glass', preview: ['#100d0c', '#1d1816', '#ff9f7a', '#76e4f7'] },
  { id: 'arctic', label: 'Arctic Alloy', preview: ['#070b10', '#111923', '#a7f3ff', '#b8f7d4'] },
  { id: 'classic', label: 'Classic', preview: ['#0c0d0a', '#161711', '#8b5cf6', '#4bb3a8'] },
  { id: 'light', label: 'Light', preview: ['#f6f5f2', '#ffffff', '#7c3aed', '#4bb3a8'] }
];
// Migrate the old dark/light flag: 'dark' was the warm cinematic palette.
const savedTheme = (() => {
  const t = LS('ga.theme', 'violet');
  return t === 'dark' ? 'classic' : t;
})();
export const theme = writable(savedTheme);
export const mode = writable(LS('ga.mode', 'cinematic')); // cinematic | editorial

let prevDark = savedTheme === 'light' ? 'violet' : savedTheme;
theme.subscribe((v) => {
  persist('ga.theme', v);
  if (v !== 'light') prevDark = v;
  if (typeof document !== 'undefined') document.documentElement.dataset.theme = v;
});
export const setTheme = (id) => theme.set(id);
export const toggleLight = () => theme.update((t) => (t === 'light' ? prevDark : 'light'));
mode.subscribe((v) => {
  persist('ga.mode', v);
  if (typeof document !== 'undefined') document.documentElement.dataset.mode = v;
});

// --- Filter / view state ----------------------------------------------------
export const filters = writable({
  view: 'recent', // recent | all | collections | favorites | archive | canvases
  query: '',
  tags: [],
  models: [],
  resolutions: [], // selected "<shorter-side>-<orientation>" buckets, e.g. ['720-landscape', '720-portrait']
  canvas: null,
  mediaType: 'all',
  period: 'all', // all | hour1 | hour4 | hour8 | today | yesterday | last7 | last14 | last30 | month | year
  sort: 'new'
});

export function setView(view) {
  // Any top-level nav click exits an open collection — including re-clicking the section
  // you're already in (e.g. tapping "Library" while inside a collection returns to its root).
  activeCollectionId.set(null);
  filters.update((f) => {
    const changed = f.view !== view;
    return {
      ...f,
      view,
      tags: changed ? [] : f.tags,
      models: changed ? [] : f.models,
      resolutions: changed ? [] : f.resolutions,
      canvas: null
    };
  });
}
export function setQuery(query) {
  filters.update((f) => ({ ...f, query }));
}

// Which Prompt Studio sub-tab is active. Lifted out of the component so the top-bar
// island can jump straight to a tab (the "Prompts" → Saved button) and reflect which
// one is showing. compose | scene | freeform are the authoring tabs; 'saved' is the library.
export const studioTab = writable('compose');
// Enter Prompt Studio. Pass a tab to land on it ('saved' powers the Prompts button);
// with no arg, keep your current authoring tab but never land on Saved — so the ✦ Studio
// button always opens the composer/authoring area, not the library.
export function openStudio(tab) {
  if (tab) studioTab.set(tab);
  else studioTab.update((t) => (t === 'saved' ? 'compose' : t));
  setView('studio');
}
export function toggleTag(tag) {
  filters.update((f) => ({
    ...f,
    tags: f.tags.includes(tag) ? f.tags.filter((t) => t !== tag) : [...f.tags, tag]
  }));
}
export function toggleModel(model) {
  filters.update((f) => ({
    ...f,
    models: f.models.includes(model) ? f.models.filter((m) => m !== model) : [...f.models, model]
  }));
}
export function toggleResolution(key) {
  filters.update((f) => ({
    ...f,
    resolutions: f.resolutions.includes(key)
      ? f.resolutions.filter((k) => k !== key)
      : [...f.resolutions, key]
  }));
}
export function setMediaType(mediaType) {
  filters.update((f) => ({ ...f, mediaType }));
}
export function setSort(sort) {
  filters.update((f) => ({ ...f, sort }));
}
export function setPeriod(period) {
  filters.update((f) => ({ ...f, period }));
}
export function clearFilters() {
  filters.update((f) => ({ ...f, query: '', tags: [], models: [], resolutions: [], canvas: null, mediaType: 'all', period: 'all' }));
}
// Full reset, including the active view -> back to Recent.
export function resetAll() {
  filters.update((f) => ({ ...f, view: 'recent', query: '', tags: [], models: [], resolutions: [], canvas: null, mediaType: 'all', period: 'all' }));
}
export function hasActiveFilters(f) {
  return !!(f.query || f.tags.length || f.models.length || f.resolutions.length || f.canvas || f.mediaType !== 'all' || f.period !== 'all' || f.view !== 'recent');
}

// --- Library: favorites + archive (stored as stashed for backward compatibility)
export const favorites = writable(new Set());
export const stashed = writable(new Set());

export function applyLibrary(lib) {
  favorites.set(new Set((lib.favorites || []).map(String)));
  stashed.set(new Set((lib.stashed || []).map(String)));
}
function pushLibrary() {
  saveLibrary({ favorites: [...get(favorites)], stashed: [...get(stashed)] });
}
export function toggleFavorite(id) {
  favorites.update((s) => {
    const n = new Set(s);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });
  pushLibrary();
}
export function setStashed(ids, on) {
  stashed.update((s) => {
    const n = new Set(s);
    for (const id of ids) (on ? n.add(id) : n.delete(id));
    return n;
  });
  pushLibrary();
}

export function setFavorites(ids, on) {
  favorites.update((s) => {
    const n = new Set(s);
    for (const id of ids) (on ? n.add(id) : n.delete(id));
    return n;
  });
  pushLibrary();
}

export const counts = derived([favorites, stashed], ([$f, $s]) => ({
  favorites: $f.size,
  archived: $s.size,
  stashed: $s.size
}));

// --- Movie montage job (global) --------------------------------------------
// The render runs in a server-side background thread, so its status is global,
// not owned by the Generate Movie panel. Tracking it here lets the Montage
// button in the selection bar animate while a render is in flight (and lets the
// panel reconnect to a live — or just-finished — job when reopened). One shared
// poll feeds both, so the panel doesn't need its own interval.
const IDLE_MOVIE = { running: false, status: 'idle', job_id: null, progress: 0, detail: '', error: null, result: null };
export const movieJob = writable(IDLE_MOVIE);
let _moviePollTimer = null;

export async function refreshMovieStatus() {
  try {
    const s = await movieStatus();
    movieJob.set(s);
    if (!s.running) stopMoviePolling();
    return s;
  } catch {
    return get(movieJob);
  }
}
function stopMoviePolling() {
  if (_moviePollTimer) { clearInterval(_moviePollTimer); _moviePollTimer = null; }
}
// Begin (or keep) polling the render's progress. Safe to call repeatedly; it
// no-ops if already polling and stops itself once the job is no longer running.
export function ensureMoviePolling() {
  if (typeof setInterval === 'undefined') return;
  refreshMovieStatus();
  if (!_moviePollTimer) _moviePollTimer = setInterval(refreshMovieStatus, 1500);
}
// Optimistic local update when the panel kicks off a render, so the button lights
// up instantly without waiting for the first poll.
export function markMovieStarted(jobId) {
  movieJob.set({ ...IDLE_MOVIE, running: true, status: 'queued', detail: 'Queued…', job_id: jobId });
  movieAck.set(null);
  ensureMoviePolling();
}

// The status chip stays up from render start until the result is dealt with
// (committed, "Make another", or dismissed). `movieAck` records the job id the
// user has acknowledged so the chip — and a fresh panel open — can hide it.
export const movieAck = writable(null);
export function acknowledgeMovie(jobId) {
  if (!jobId) return;
  movieAck.set(jobId);          // hide the chip now, this session
  dismissMovie();               // and durably, so a reload doesn't resurrect it
}

// The job to surface in the chip/panel, or null when there's nothing pending: a
// running render, or a finished/failed one not yet dealt with. "Dealt with" is
// either the in-memory ack (this session) or the server's durable `acknowledged`
// flag (set on dismiss/commit) — the latter is what keeps the chip gone across
// reloads, since `movieAck` resets to null on a fresh load.
export const movieChip = derived([movieJob, movieAck], ([$j, $ack]) => {
  const finished = ($j.status === 'done' && $j.result) || $j.status === 'error';
  const dealtWith = $j.acknowledged || $j.job_id === $ack;
  const pending = $j.running || (finished && $j.job_id && !dealtWith);
  return pending ? $j : null;
});

// --- Delete (hard-delete: removes files on disk + blocklists from future syncs)
// `deleted` hides items instantly in the current session; the server also drops
// them from the index, so subsequent fetches won't return them anyway.
export const deleted = writable(new Set());

export async function removeMedia(ids) {
  const list = (Array.isArray(ids) ? ids : [ids]).map(String).filter(Boolean);
  if (!list.length) return;
  const idSet = new Set(list);
  // Optimistically hide so the UI reacts immediately.
  deleted.update((s) => new Set([...s, ...list]));
  try {
    await deleteMedia(list);
    // The server already purged these from library/playlists/collections; mirror it in memory
    // so counts/UI stay correct without a refetch.
    favorites.update((s) => new Set([...s].filter((id) => !idSet.has(id))));
    stashed.update((s) => new Set([...s].filter((id) => !idSet.has(id))));
    playlists.update((p) => p.map((pl) => ({ ...pl, ids: pl.ids.filter((id) => !idSet.has(String(id))) })));
    collections.update((c) => c.map((coll) => ({
      ...coll,
      ids: coll.ids.filter((id) => !idSet.has(String(id))),
      cover_id: idSet.has(String(coll.cover_id)) ? '' : coll.cover_id
    })));
    selection.update((sel) => sel.filter((id) => !idSet.has(String(id))));
    basket.update((b) => b.filter((id) => !idSet.has(String(id))));
    toast(list.length === 1 ? 'Deleted' : `Deleted ${list.length} items`, { type: 'success' });
  } catch (e) {
    deleted.update((s) => new Set([...s].filter((id) => !idSet.has(id)))); // revert hide
    toast(e?.message || 'Delete failed.', { type: 'error' });
  }
}

// --- Multi-select -----------------------------------------------------------
export const selectMode = writable(false);
export const selection = writable([]); // ordered ids (selection order matters for export)

export function setSelectMode(on) {
  selectMode.set(on);
  if (!on) selection.set([]);
}
export function toggleSelection(id) {
  selection.update((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
}
export function addSelection(ids) {
  selection.update((s) => {
    const next = [...s];
    const seen = new Set(next);
    for (const raw of ids || []) {
      const id = String(raw);
      if (!id || seen.has(id)) continue;
      seen.add(id);
      next.push(id);
    }
    return next;
  });
}
// Idempotent set/clear of one id, preserving selection order (new ids append).
// Used by drag-to-select so painting over a card repeatedly doesn't toggle it.
export function setSelection(id, on) {
  selection.update((s) => {
    const has = s.includes(id);
    if (on === has) return s;
    return on ? [...s, id] : s.filter((x) => x !== id);
  });
}
export function clearSelection() {
  selection.set([]);
}

// Fine-grained membership mirror of `selection`, kept in sync with the array above.
// The grid reads THIS for "is this card selected?" instead of a freshly-rebuilt Set:
// `SvelteSet.has(id)` is a per-key reactive dependency, so painting one card only
// re-renders that card — a new Set's changed identity would invalidate every cell on
// every paint, which is what made drag-select sluggish on long, already-loaded lists.
// `selection` stays the source of truth (it carries export order); we only diff here.
export const selectionMembers = new SvelteSet();
selection.subscribe((ids) => {
  const next = new Set(ids);
  for (const id of selectionMembers) if (!next.has(id)) selectionMembers.delete(id);
  for (const id of ids) selectionMembers.add(id); // add() no-ops on existing keys
});

// --- Montage basket (cross-library queue) -----------------------------------
// A SECOND ordered-id store, deliberately separate from `selection`: nothing in
// setSelectMode / clearSelection / the page's context-scoped clearing effect ever
// touches it, so videos gathered across different collections, canvases and views
// survive navigation. That separation IS the feature — it's solved by construction,
// not by a guard that could rot. Add-order is the montage order. localStorage-backed
// (same LS/persist tier as theme/mode) so an accidental reload mid-gather doesn't
// lose the picks; it's a scratch tray, so it intentionally isn't server-synced.
export const basket = writable(LS('basket', []));
basket.subscribe((ids) => persist('basket', ids));

// Per-card membership mirror — same per-key reactive trick as selectionMembers, so
// toggling one card only re-renders that card, not every visible cell.
export const basketMembers = new SvelteSet();
basket.subscribe((ids) => {
  const next = new Set(ids.map(String));
  for (const id of basketMembers) if (!next.has(id)) basketMembers.delete(id);
  for (const id of next) basketMembers.add(id);
});

export function toggleBasket(id) {
  const s = String(id);
  basket.update((b) => (b.includes(s) ? b.filter((x) => x !== s) : [...b, s]));
}
// Add many ids at once (e.g. pouring an in-context multi-select into the basket),
// preserving order and skipping dupes — same shape as addSelection.
export function enqueueBasket(ids) {
  basket.update((b) => {
    const next = [...b];
    const seen = new Set(next);
    for (const raw of ids || []) {
      const id = String(raw);
      if (!id || seen.has(id)) continue;
      seen.add(id);
      next.push(id);
    }
    return next;
  });
}
export function clearBasket() {
  basket.set([]);
}

// --- Playlists --------------------------------------------------------------
export const playlists = writable([]);
const rid = () => 'pl-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);

export async function loadPlaylists() {
  playlists.set(await fetchPlaylists());
}
function persistPlaylists() {
  savePlaylists(get(playlists));
}
export function addPlaylist(name, ids) {
  playlists.update((p) => [
    { id: rid(), name, ids, created_at: new Date().toISOString().slice(0, 10) },
    ...p
  ]);
  persistPlaylists();
}
export function updatePlaylist(id, patch) {
  playlists.update((p) => p.map((pl) => (pl.id === id ? { ...pl, ...patch } : pl)));
  persistPlaylists();
}
export function removePlaylist(id) {
  playlists.update((p) => p.filter((pl) => pl.id !== id));
  persistPlaylists();
}

// --- Collections -----------------------------------------------------------
export const collections = writable([]);
// Which collection is open in the Collections view (null = the landing grid). Lifted to a
// store so the top bar can tell "collections landing" from "inside a collection" and adapt
// its controls. Owned by the page: set on open, cleared on Back / when leaving the view.
export const activeCollectionId = writable(null);
const cid = () => 'co-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
const today = () => new Date().toISOString().slice(0, 10);

export async function loadCollections() {
  collections.set(await fetchCollections());
}
function persistCollections() {
  saveCollections(get(collections));
}
function uniqueIds(ids) {
  const seen = new Set();
  const out = [];
  for (const raw of ids || []) {
    const id = String(raw);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}
export function addCollection(name, ids = []) {
  const cleanIds = uniqueIds(ids);
  const created = today();
  const coll = {
    id: cid(),
    name,
    ids: cleanIds,
    cover_id: cleanIds[0] || '',
    created_at: created,
    updated_at: created
  };
  collections.update((c) => [coll, ...c]);
  persistCollections();
  return coll.id;
}
export function updateCollection(id, patch) {
  collections.update((c) => c.map((coll) => (
    coll.id === id ? { ...coll, ...patch, updated_at: today() } : coll
  )));
  persistCollections();
}
export function removeCollection(id) {
  collections.update((c) => c.filter((coll) => coll.id !== id));
  persistCollections();
}
export function addToCollection(id, ids) {
  const incoming = uniqueIds(ids);
  if (!incoming.length) return;
  collections.update((c) => c.map((coll) => {
    if (coll.id !== id) return coll;
    const next = uniqueIds([...(coll.ids || []), ...incoming]);
    return { ...coll, ids: next, cover_id: coll.cover_id || next[0] || '', updated_at: today() };
  }));
  persistCollections();
}
export function removeFromCollection(id, ids) {
  const remove = new Set((ids || []).map(String));
  collections.update((c) => c.map((coll) => {
    if (coll.id !== id) return coll;
    const next = (coll.ids || []).filter((mid) => !remove.has(String(mid)));
    return { ...coll, ids: next, cover_id: remove.has(String(coll.cover_id)) ? (next[0] || '') : coll.cover_id, updated_at: today() };
  }));
  persistCollections();
}
export function setCollectionCover(id, mediaId) {
  updateCollection(id, { cover_id: String(mediaId || '') });
}

// --- Settings (Whisper / burn) ---------------------------------------------
export const settings = writable({
  whisper_configured: false,
  whisper_server_url: '',
  whisper_env_locked: false,
  burn_subtitles: false,
  embed_configured: false,
  embed_server_url: '',
  embed_model: '',
  embed_env_locked: false,
  embed_model_env_locked: false,
  embed_provider: 'local',
  embed_api_key_configured: false,
  embed_api_key_env_locked: false,
  llm_configured: false,
  llm_server_url: '',
  llm_model: '',
  llm_env_locked: false,
  llm_model_env_locked: false,
  llm_provider: 'local',
  llm_api_key_configured: false,
  llm_api_key_env_locked: false,
  xai_api_key_configured: false,
  xai_api_key_env_locked: false,
  xai_image_model: 'grok-imagine-image-quality',
  xai_video_model: 'grok-imagine-video',
  xai_image_resolution: '1k',
  xai_image_aspect_ratio: '1:1',
  xai_video_resolution: '480p',
  xai_video_aspect_ratio: '16:9',
  xai_video_duration: 6
});
export async function loadSettings() {
  try { settings.set(await getSettings()); } catch {}
}

// --- Grok Imagine: multi-session workspaces --------------------------------
// Each workspace is a server-side staging session keyed by id: `src:<galleryId>`
// (rooted on a gallery image), `scratch`, or `ws:<rand>` (an ad-hoc text workspace).
// `activeImagineSession` is the one the Imagine view shows; sessions persist and
// coexist, so switching never overwrites another.
export const activeImagineSession = writable('scratch');

// "Use as source" on a gallery image opens (or resumes) that image's workspace and
// jumps to the Imagine view. The session id is all that's needed — the server fills
// in the source thumbnail/prompt/dimensions when the session loads.
export function sendToImagine(item) {
  if (!item?.id) return;
  activeImagineSession.set(`src:${item.id}`);
  filters.update((f) => ({ ...f, view: 'imagine' }));
}
export function newImagineWorkspace() {
  const id = 'ws:' + Math.random().toString(36).slice(2, 10);
  activeImagineSession.set(id);
  return id;
}

// The switcher's list of sessions (refreshed after generate/clear/delete).
export const imagineSessions = writable([]);
export async function refreshImagineSessions() {
  try {
    const d = await getImagineSessions();
    imagineSessions.set(Array.isArray(d.sessions) ? d.sessions : []);
  } catch {}
}

// Per-session video jobs, polled globally so progress survives switching sessions/
// views and drives the switcher's render spinners. Keyed by session_id.
export const imagineJobs = writable({});
let _imaginePollTimer = null;
export async function refreshImagineJobs() {
  try {
    const d = await imagineJobsAll();
    const map = {};
    for (const j of d.jobs || []) map[j.session_id] = j;
    imagineJobs.set(map);
    if (!(d.jobs || []).some((j) => j.running)) stopImaginePolling();
  } catch {}
}
function stopImaginePolling() {
  if (_imaginePollTimer) { clearInterval(_imaginePollTimer); _imaginePollTimer = null; }
}
// Begin (or keep) polling jobs; safe to call repeatedly. Stops itself once nothing
// is running (a freshly-started job re-arms it).
export function ensureImaginePolling() {
  if (typeof setInterval === 'undefined') return;
  refreshImagineJobs();
  if (!_imaginePollTimer) _imaginePollTimer = setInterval(refreshImagineJobs, 2500);
}

// Bumped after a generation is SAVED into the gallery so the gallery refetches.
export const galleryReload = writable(0);
export function requestGalleryReload() {
  galleryReload.update((n) => n + 1);
}

// --- Saved Prompt Studio responses (starred outputs, server-persisted) ------
export const savedResponses = writable([]);
export async function loadSavedResponses() {
  savedResponses.set(await fetchSavedResponses());
}
function persistSavedResponses() {
  saveSavedResponses(get(savedResponses));
}
export function addSavedResponse(text, { folder = '' } = {}) {
  const t = String(text || '').trim();
  if (!t) return;
  let added = false;
  savedResponses.update((r) => {
    if (r.some((x) => x.text === t)) return r; // dedupe exact text
    added = true;
    return [{ id: 'rs-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5), text: t, created_at: new Date().toISOString().slice(0, 10), folder: String(folder || ''), tags: [] }, ...r];
  });
  if (added) { persistSavedResponses(); toast('Saved', { type: 'success' }); }
  else toast('Already saved', { type: 'info' });
  return added;
}
// Safe one-off save for callers that may NOT have loaded the full list (e.g. the lightbox's
// "Describe for Grok"): the server appends to the on-disk file and returns the full list, so
// it can't overwrite the user's other saved prompts with an empty/stale local store. Always
// use this — NOT addSavedResponse — from outside Prompt Studio.
export async function saveResponseToStudio(text, { folder = '' } = {}) {
  const t = String(text || '').trim();
  if (!t) return false;
  try {
    const d = await addSavedResponseRemote(t, folder);
    if (Array.isArray(d.responses)) savedResponses.set(d.responses);
    toast(d.added === false ? 'Already saved' : 'Saved', { type: d.added === false ? 'info' : 'success' });
    return true;
  } catch {
    toast("Couldn't save to Prompt Studio — check your connection.", { type: 'error' });
    return false;
  }
}
// Import every library prompt not already saved (server-side merge, backed up + deduped) and
// refresh the store from the authoritative list. Returns { added, total, backup }.
export async function importLibraryIntoSaved({ folder = 'Library' } = {}) {
  const d = await importLibraryPrompts({ folder });
  if (Array.isArray(d.responses)) savedResponses.set(d.responses);
  return d;
}
// Merge a partial patch ({ folder } / { tags }) into one saved response and persist.
export function updateSavedResponse(id, patch) {
  savedResponses.update((r) => r.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  persistSavedResponses();
}
// Replace the whole list in a new order (drag-to-reorder) and persist.
export function setSavedResponses(list) {
  savedResponses.set(list);
  persistSavedResponses();
}
export function removeSavedResponse(id) {
  savedResponses.update((r) => r.filter((x) => x.id !== id));
  persistSavedResponses();
}
