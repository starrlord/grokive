import { writable, derived, get } from 'svelte/store';
import {
  saveLibrary, fetchPlaylists, savePlaylists,
  fetchCollections, saveCollections,
  getSettings, deleteMedia
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
  { id: 'violet', label: 'Violet' },
  { id: 'classic', label: 'Classic' },
  { id: 'light', label: 'Light' }
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
  resolutions: [], // selected shorter-side heights, e.g. [720, 1080]
  canvas: null,
  mediaType: 'all',
  period: 'all', // all | hour1 | hour4 | hour8 | today | yesterday | last7 | last14 | last30 | month | year
  sort: 'new'
});

export function setView(view) {
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
export function toggleResolution(height) {
  filters.update((f) => ({
    ...f,
    resolutions: f.resolutions.includes(height)
      ? f.resolutions.filter((h) => h !== height)
      : [...f.resolutions, height]
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
  burn_subtitles: false
});
export async function loadSettings() {
  try { settings.set(await getSettings()); } catch {}
}
