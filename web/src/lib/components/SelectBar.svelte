<script>
  import { selection, filters, setFavorites, setStashed, addPlaylist, setSelectMode, clearSelection, removeMedia } from '$lib/state.js';
  import { exportSelection } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { videoIds = [], onplay = () => {} } = $props();
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

  function favAll() { setFavorites($selection, view !== 'favorites'); clearSelection(); }
  function stashAll() { setStashed($selection, view !== 'stashed'); clearSelection(); }
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
  <button class="flex items-center gap-1.5 rounded-lg border border-transparent bg-[var(--accent)] px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
          disabled={!videoIds.length} onclick={() => onplay()} title="Play selected videos in order">
    <span class="text-xs">▶</span> Play{videoIds.length > 1 ? ` (${videoIds.length})` : ''}
  </button>
  <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={favAll}>{view === 'favorites' ? 'Unfavorite' : 'Favorite'}</button>
  <button class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={stashAll}>{view === 'stashed' ? 'Unstash' : 'Stash'}</button>
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
