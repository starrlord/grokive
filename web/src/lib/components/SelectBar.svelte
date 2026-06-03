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
<div class="fixed inset-x-0 bottom-0 z-40 flex flex-wrap items-center justify-center gap-2 border-t border-line bg-[var(--surface-solid)] px-3 py-2.5 shadow-[0_-10px_30px_rgba(0,0,0,0.35)] backdrop-blur-sm"
     style="padding-bottom: max(0.625rem, env(safe-area-inset-bottom))">
  <span class="px-1 text-sm font-bold whitespace-nowrap">{n} selected</span>
  <details class="group relative">
    <summary class="cursor-pointer list-none rounded-lg border border-line px-3 py-2 text-sm font-semibold hover:border-[var(--accent)] [&::-webkit-details-marker]:hidden">
      Select…
    </summary>
    <div class="absolute bottom-[calc(100%+0.5rem)] left-0 z-50 min-w-44 overflow-hidden rounded-lg border border-line bg-[var(--surface-solid)] p-1 shadow-[0_16px_40px_rgba(0,0,0,0.45)]">
      <button type="button" class="block w-full rounded-md px-3 py-2 text-left text-sm font-semibold hover:bg-[var(--surface-2)] disabled:opacity-45"
        disabled={!unselectedVisibleIds.length} onclick={(e) => { selectVisible(); e.currentTarget.closest('details').open = false; }}>Visible ({unselectedVisibleIds.length})</button>
      <button type="button" class="block w-full rounded-md px-3 py-2 text-left text-sm font-semibold hover:bg-[var(--surface-2)] disabled:opacity-45"
        disabled={!batchCount} onclick={(e) => { selectBatch(); e.currentTarget.closest('details').open = false; }}>{unselectedVisibleIds.length <= 25 ? 'Remaining' : 'Next 25'} ({batchCount})</button>
    </div>
  </details>
  <button class="flex items-center gap-1.5 rounded-lg border border-transparent bg-[var(--accent)] px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
          disabled={!videoIds.length} onclick={() => onplay()} title="Play selected videos in order">
    <span class="text-xs">▶</span> Play{videoIds.length > 1 ? ` (${videoIds.length})` : ''}
  </button>
  <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={favAll}>{view === 'favorites' ? 'Unfavorite' : 'Favorite'}</button>
  <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={() => oncollections()}>Add to Collection</button>
  {#if collection}
    <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={() => onremovefromcollection()}>Remove from Collection</button>
  {/if}
  <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={stashAll}>{view === 'archive' ? 'Restore' : 'Archive'}</button>
  <span class="mx-1 h-6 w-px bg-line"></span>
  <input class="w-40 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none" placeholder="Playlist name" bind:value={name} maxlength="80" />
  <button class="rounded-lg border border-transparent bg-[var(--accent)] px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
          disabled={!name.trim() || !videoIds.length} onclick={save}>Save playlist</button>
  <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold disabled:opacity-50"
          disabled={!videoIds.length || busy} onclick={doExport}>{busy ? 'Exporting…' : 'Export'}</button>
  <button class="rounded-lg border border-red-500/50 px-3 py-2 text-sm font-semibold text-red-400 transition hover:bg-red-500/10 disabled:opacity-50"
          disabled={!n} onclick={() => (confirmingDelete = true)}>Delete</button>
  <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={() => clearSelection()}>Clear</button>
  <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={() => setSelectMode(false)}>Done</button>
</div>

{#if confirmingDelete}
  <ConfirmDialog title={`Delete ${n} item${n === 1 ? '' : 's'}?`}
    message="The files are permanently removed from disk and won't be re-downloaded on future syncs."
    confirmLabel="Delete" onconfirm={doDelete} oncancel={() => (confirmingDelete = false)} />
{/if}
