<script>
  import { selection, filters, setFavorites, setStashed, addPlaylist, setSelectMode, clearSelection, addSelection, removeMedia } from '$lib/state.js';
  import { exportSelection } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { videoIds = [], selectableIds = [], collection = null, onplay = () => {}, oncollections = () => {}, onremovefromcollection = () => {} } = $props();
  let name = $state('');
  let busy = $state(false);
  let confirmingDelete = $state(false);

  function doDelete() {
    const ids = [...$selection];
    confirmingDelete = false;
    if (!ids.length) return;
    removeMedia(ids);
    setSelectMode(false);
  }

  const view = $derived($filters.view);
  const n = $derived($selection.length);
  const visibleIds = $derived((selectableIds || []).map(String).filter(Boolean));
  const unselectedVisibleIds = $derived(visibleIds.filter((id) => !$selection.includes(id)));
  const batchCount = $derived(Math.min(25, unselectedVisibleIds.length));

  function selectIds(ids, label) {
    const list = ids.filter(Boolean);
    if (!list.length) return;
    addSelection(list);
    toast(`${label} selected`, { type: 'success' });
  }

  function selectVisible() {
    selectIds(unselectedVisibleIds, `${unselectedVisibleIds.length} visible item${unselectedVisibleIds.length === 1 ? '' : 's'}`);
  }

  function selectBatch() {
    selectIds(unselectedVisibleIds.slice(0, 25), `${batchCount} item${batchCount === 1 ? '' : 's'}`);
  }

  function favAll() { setFavorites($selection, view !== 'favorites'); clearSelection(); }
  function stashAll() { setStashed($selection, view !== 'archive'); clearSelection(); }
  function save() {
    if (!name.trim() || !videoIds.length) return;
    addPlaylist(name.trim(), videoIds);
    name = '';
    setSelectMode(false);
  }
  async function doExport() {
    if (!videoIds.length) return;
    busy = true;
    try {
      await exportSelection(videoIds);
      toast(`Exported selection (${videoIds.length})`, { type: 'success' });
    } catch (e) {
      toast(e.message || 'Export failed.', { type: 'error' });
    } finally {
      busy = false;
    }
  }
</script>

<!-- Solid (not glass) surface: this is the primary action bar and sits over a dense
     thumbnail grid, so legibility wins over translucency. -->
<div class="select-shell fixed inset-x-0 bottom-0 z-40 px-3 py-3"
     class:is-exporting={busy}
     style="padding-bottom: max(0.75rem, env(safe-area-inset-bottom))">
  <div class="select-dock mx-auto flex items-center overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
    <div class="select-count">
      <span class="select-badge">{n}</span>
      <span class="select-count-label">selected</span>
    </div>

    <div class="select-cluster">
      <details class="group relative">
        <summary class="select-btn cursor-pointer list-none [&::-webkit-details-marker]:hidden">
          Select…
        </summary>
        <div class="absolute bottom-[calc(100%+0.65rem)] left-0 z-50 min-w-44 overflow-hidden rounded-lg border border-line bg-[var(--surface-solid)] p-1 shadow-[0_16px_40px_rgba(0,0,0,0.45)]">
          <button type="button" class="block w-full rounded-md px-3 py-2 text-left text-sm font-semibold hover:bg-[var(--surface-2)] disabled:opacity-45"
            disabled={!unselectedVisibleIds.length} onclick={(e) => { selectVisible(); e.currentTarget.closest('details').open = false; }}>Visible ({unselectedVisibleIds.length})</button>
          <button type="button" class="block w-full rounded-md px-3 py-2 text-left text-sm font-semibold hover:bg-[var(--surface-2)] disabled:opacity-45"
            disabled={!batchCount} onclick={(e) => { selectBatch(); e.currentTarget.closest('details').open = false; }}>{unselectedVisibleIds.length <= 25 ? 'Remaining' : 'Next 25'} ({batchCount})</button>
        </div>
      </details>
      <button class="select-btn select-primary"
              disabled={!videoIds.length} onclick={() => onplay()} title="Play selected videos in order">
        <span class="text-xs">▶</span> Play{videoIds.length > 1 ? ` (${videoIds.length})` : ''}
      </button>
    </div>

    <div class="select-cluster">
      <button class="select-btn" onclick={favAll}>{view === 'favorites' ? 'Unfavorite' : 'Favorite'}</button>
      <button class="select-btn" onclick={() => oncollections()} title="Add to Collection">+ Collection</button>
      {#if collection}
        <button class="select-btn" onclick={() => onremovefromcollection()} title="Remove from Collection">Remove</button>
      {/if}
      <button class="select-btn" onclick={stashAll}>{view === 'archive' ? 'Restore' : 'Archive'}</button>
    </div>

    <div class="select-cluster">
      <details class="playlist-menu relative">
        <summary class="select-btn cursor-pointer list-none [&::-webkit-details-marker]:hidden">
          Playlist
        </summary>
        <div class="playlist-popover">
          <input class="playlist-input" placeholder="Playlist name" bind:value={name} maxlength="80" />
          <button class="select-btn select-primary" disabled={!name.trim() || !videoIds.length} onclick={save} title="Save playlist">Save</button>
        </div>
      </details>
      <button class="select-btn export-btn" class:export-active={busy}
              disabled={!videoIds.length || busy} onclick={doExport} aria-busy={busy}>
        {#if busy}
          <span class="export-orbit" aria-hidden="true"></span>
          <span>Preparing MP4</span>
        {:else}
          <span class="text-xs">⇩</span>
          <span>Export</span>
        {/if}
      </button>
    </div>

    <div class="select-cluster">
      <button class="select-btn select-danger" disabled={!n} onclick={() => (confirmingDelete = true)}>Delete</button>
      <button class="select-btn" onclick={() => clearSelection()}>Clear</button>
      <button class="select-btn" onclick={() => setSelectMode(false)}>Done</button>
    </div>
  </div>
</div>

{#if confirmingDelete}
  <ConfirmDialog title={`Delete ${n} item${n === 1 ? '' : 's'}?`}
    message="The files are permanently removed from disk and won't be re-downloaded on future syncs."
    confirmLabel="Delete" onconfirm={doDelete} oncancel={() => (confirmingDelete = false)} />
{/if}

<style>
  .select-shell {
    background: linear-gradient(
      to top,
      var(--bg) 0%,
      color-mix(in srgb, var(--bg) 92%, transparent) 62%,
      transparent 100%
    );
    pointer-events: none;
  }

  .select-dock {
    background: color-mix(in srgb, var(--surface-solid) 88%, #111 12%);
    border: 1px solid color-mix(in srgb, var(--line) 86%, transparent);
    border-radius: var(--r-2xl);
    box-shadow:
      0 18px 42px rgba(0, 0, 0, 0.42),
      inset 0 1px 0 color-mix(in srgb, white 7%, transparent);
    gap: 0.35rem;
    max-width: min(100%, 74rem);
    padding: 0.45rem;
    pointer-events: auto;
    position: relative;
    width: max-content;
  }

  .is-exporting .select-dock::before {
    animation: export-sheen 1.25s linear infinite;
    background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 34%, transparent), transparent);
    content: '';
    inset: 0;
    pointer-events: none;
    position: absolute;
    transform: translateX(-100%);
  }

  .select-count,
  .select-cluster {
    align-items: center;
    display: flex;
    flex: 0 0 auto;
  }

  .select-count {
    background: color-mix(in srgb, var(--accent) 13%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent) 24%, transparent);
    border-radius: 999px;
    gap: 0.45rem;
    min-height: 2.35rem;
    padding: 0.25rem 0.8rem 0.25rem 0.25rem;
  }

  .select-count-label {
    color: var(--ink);
    font-size: 0.8125rem;
    font-weight: 850;
    white-space: nowrap;
  }

  .select-cluster {
    gap: 0.25rem;
    padding-left: 0.35rem;
  }

  .select-cluster + .select-cluster {
    border-left: 1px solid color-mix(in srgb, var(--line) 66%, transparent);
    margin-left: 0.25rem;
    padding-left: 0.6rem;
  }

  .select-badge {
    background: var(--accent);
    border-radius: 999px;
    color: white;
    display: grid;
    font-size: 0.75rem;
    font-weight: 900;
    height: 1.75rem;
    min-width: 1.75rem;
    padding: 0 0.375rem;
    place-items: center;
  }

  .select-btn,
  .playlist-input {
    border: 1px solid transparent;
    border-radius: var(--r-lg);
    flex: 0 0 auto;
    font-size: 0.8125rem;
    font-weight: 750;
    min-height: 2.25rem;
    padding: 0.52rem 0.72rem;
    transition: border-color 160ms ease, background 160ms ease, color 160ms ease, opacity 160ms ease;
    white-space: nowrap;
  }

  .select-btn {
    align-items: center;
    display: inline-flex;
    gap: 0.4rem;
    justify-content: center;
    line-height: 1;
  }

  .select-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--surface-2) 72%, transparent);
    border-color: color-mix(in srgb, var(--line) 86%, transparent);
  }

  .select-btn:disabled {
    cursor: default;
    opacity: 0.48;
  }

  .select-primary {
    background: var(--accent);
    border-color: transparent;
    color: white;
  }

  .select-danger {
    border-color: color-mix(in srgb, #ef4444 52%, transparent);
    color: #f87171;
  }

  .select-danger:hover:not(:disabled) {
    background: color-mix(in srgb, #ef4444 12%, transparent);
    border-color: #ef4444;
  }

  .playlist-input {
    background: color-mix(in srgb, var(--surface-2) 92%, black);
    border-color: var(--line);
    color: var(--ink);
    font-weight: 600;
    outline: none;
    width: 12rem;
  }

  .playlist-popover {
    align-items: center;
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: var(--r-xl);
    bottom: calc(max(0.75rem, env(safe-area-inset-bottom)) + 4.5rem);
    box-shadow: 0 18px 48px rgba(0, 0, 0, 0.45);
    display: flex;
    gap: 0.45rem;
    left: 50%;
    padding: 0.5rem;
    position: fixed;
    transform: translateX(-50%);
    z-index: 55;
  }

  .playlist-menu:not([open]) .playlist-popover {
    display: none;
  }

  .export-btn {
    min-width: 7rem;
    overflow: hidden;
    position: relative;
  }

  .export-active {
    animation: export-pulse 900ms ease-in-out infinite alternate;
    background: linear-gradient(135deg, var(--accent), #14b8a6);
    border-color: transparent;
    color: white;
  }

  .export-orbit {
    animation: export-spin 800ms linear infinite;
    border: 2px solid color-mix(in srgb, white 35%, transparent);
    border-top-color: white;
    border-radius: 999px;
    height: 0.9rem;
    width: 0.9rem;
  }

  @keyframes export-sheen {
    to { transform: translateX(100%); }
  }

  @keyframes export-spin {
    to { transform: rotate(360deg); }
  }

  @keyframes export-pulse {
    from { box-shadow: 0 0 0 color-mix(in srgb, var(--accent) 0%, transparent); }
    to { box-shadow: 0 0 24px color-mix(in srgb, var(--accent) 42%, transparent); }
  }

  @media (min-width: 1536px) {
    .select-shell {
      left: 20rem;
    }
    
    .select-dock {
      max-width: min(100%, 82rem);
    }
  }

  @media (max-width: 640px) {
    .select-shell {
      padding-left: 0.5rem;
      padding-right: 0.5rem;
    }

    .select-dock {
      border-radius: var(--r-xl);
      max-width: 100%;
      width: 100%;
    }

    .playlist-input {
      width: 10rem;
    }

  }
</style>
