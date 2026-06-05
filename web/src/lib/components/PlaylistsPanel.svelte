<script>
  import { playlists, removePlaylist } from '$lib/state.js';
  import { exportPlaylist } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { onplay = () => {}, onedit = () => {} } = $props();
  let confirming = $state(null);
  // Per-playlist export state: the server merges/encodes the clips before
  // streaming the MP4 back, which takes a few seconds — show a spinner so it's
  // clear something's happening (and block double-clicks).
  let exporting = $state(new Set());

  async function doExport(pl) {
    if (exporting.has(pl.id)) return;
    exporting = new Set(exporting).add(pl.id);
    try {
      await exportPlaylist(pl.id, pl.name);
      toast(`Exported “${pl.name}”`, { type: 'success' });
    } catch (e) {
      toast(e.message || 'Export failed.', { type: 'error' });
    } finally {
      const next = new Set(exporting);
      next.delete(pl.id);
      exporting = next;
    }
  }
</script>

<div class="mb-5">
  <div class="mb-2 flex items-center justify-between text-xs font-bold uppercase tracking-wider text-muted">
    <span>Playlists</span><span>{$playlists.length || ''}</span>
  </div>
  {#if !$playlists.length}
    <p class="text-xs leading-relaxed text-muted">Use <b>Select</b> to choose videos and save a playlist.</p>
  {:else}
    <div class="flex flex-col gap-1">
      {#each $playlists as pl (pl.id)}
        {@const busy = exporting.has(pl.id)}
        <div class="flex items-center gap-1.5 rounded-lg border border-line p-1.5">
          <button class="grid h-7 w-7 shrink-0 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-sm bg-[var(--accent)] text-[var(--on-accent)]" title="Play" onclick={() => onplay(pl)}>▶</button>
          <button class="min-w-0 flex-1 truncate text-left text-sm hover:underline" title="Edit" onclick={() => onedit(pl)}>{pl.name}</button>
          <span class="text-xs text-muted">{pl.ids.length}</span>
          <button class="grid h-7 w-7 shrink-0 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-sm border border-[var(--success-border)] text-[var(--success-ink)] transition hover:border-[var(--success-ink)] hover:bg-[var(--success-bg)] disabled:cursor-default disabled:hover:bg-transparent" title={busy ? 'Exporting…' : 'Export MP4'} onclick={() => doExport(pl)} disabled={busy} aria-label={busy ? 'Exporting playlist' : 'Export playlist'} aria-busy={busy}>
            {#if busy}
              <svg viewBox="0 0 24 24" class="h-4 w-4 animate-spin" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
            {:else}
              <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>
            {/if}
          </button>
          <button class="grid h-7 w-7 shrink-0 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-sm border border-line text-muted transition hover:border-[var(--danger-border-strong)] hover:bg-[var(--danger-bg)] hover:text-[var(--danger-ink)]" title="Delete" onclick={() => (confirming = pl)} aria-label="Delete playlist">
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
          </button>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if confirming}
  <ConfirmDialog title="Delete playlist?" message={`“${confirming.name}” will be permanently removed. This can't be undone.`}
    confirmLabel="Delete" onconfirm={() => { removePlaylist(confirming.id); confirming = null; }} oncancel={() => (confirming = null)} />
{/if}
