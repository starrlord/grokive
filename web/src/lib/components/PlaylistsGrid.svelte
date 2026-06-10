<script>
  // Playlists on the Library page — the full-width sibling of the Collections grid. Playlists
  // are ordered video queues: play, export to a single MP4, edit/reorder, delete. (They used to
  // live cramped in the filter sidebar; here they get room and sit next to Collections, the
  // other kind of saved grouping.)
  import { playlists, removePlaylist } from '$lib/state.js';
  import { exportPlaylist } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import SearchField from './SearchField.svelte';

  let { onplay = () => {}, onedit = () => {} } = $props();

  let q = $state('');
  let confirming = $state(null);
  let exporting = $state(new Set());

  const shown = $derived(
    ($playlists || []).filter((p) => !q.trim() || (p.name || '').toLowerCase().includes(q.trim().toLowerCase()))
  );

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

{#if !$playlists.length}
  <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
    <div>
      <p class="mb-1 text-lg font-bold text-ink">No playlists yet</p>
      <p class="text-sm">Use <b class="text-ink">Select</b> to choose videos, then save them as a playlist.</p>
    </div>
  </div>
{:else}
  <div class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
    <span class="text-sm text-muted">{$playlists.length} playlist{$playlists.length === 1 ? '' : 's'}</span>
    <div class="ml-auto flex w-full items-center gap-2 sm:w-auto">
      <SearchField bind:value={q} placeholder="Search playlists…" ariaLabel="playlist search"
        wrapperClass="min-w-0 flex-1 sm:w-60 sm:flex-none"
        inputClass="rounded-full border border-line bg-[var(--surface-2)] py-1.5 pl-3.5 pr-10 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
    </div>
  </div>

  {#if !shown.length}
    <p class="py-16 text-center text-sm text-muted">No playlists match “{q.trim()}”.</p>
  {:else}
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {#each shown as pl (pl.id)}
        {@const busy = exporting.has(pl.id)}
        <article class="flex items-center gap-2 rounded-card border border-line bg-[var(--surface-2)] p-2.5 transition hover:border-[var(--accent)]">
          <button type="button" class="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-[var(--accent)] text-[var(--on-accent)] disabled:opacity-45"
            title="Play" aria-label={`Play ${pl.name}`} disabled={!pl.ids.length} onclick={() => onplay(pl)}>▶</button>
          <button type="button" class="min-w-0 flex-1 text-left" title="Edit playlist" aria-label={`Edit playlist ${pl.name}`} onclick={() => onedit(pl)}>
            <span class="block truncate text-sm font-bold text-ink hover:underline">{pl.name}</span>
            <span class="block text-xs text-muted">{pl.ids.length} video{pl.ids.length === 1 ? '' : 's'}</span>
          </button>
          <button type="button" class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-[var(--success-border)] text-[var(--success-ink)] transition hover:border-[var(--success-ink)] hover:bg-[var(--success-bg)] disabled:cursor-default"
            title={busy ? 'Exporting…' : 'Export MP4'} aria-label={busy ? 'Exporting playlist' : 'Export playlist as MP4'} aria-busy={busy} disabled={busy} onclick={() => doExport(pl)}>
            {#if busy}
              <svg viewBox="0 0 24 24" class="h-4 w-4 animate-spin" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
            {:else}
              <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>
            {/if}
          </button>
          <button type="button" class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-line text-muted transition hover:border-[var(--danger-border-strong)] hover:bg-[var(--danger-bg)] hover:text-[var(--danger-ink)]"
            title="Delete playlist" aria-label={`Delete ${pl.name}`} onclick={() => (confirming = pl)}>
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
          </button>
        </article>
      {/each}
    </div>
  {/if}
{/if}

{#if confirming}
  <ConfirmDialog title="Delete playlist?" message={`“${confirming.name}” will be permanently removed. This can't be undone.`}
    confirmLabel="Delete" onconfirm={() => { removePlaylist(confirming.id); confirming = null; }} oncancel={() => (confirming = null)} />
{/if}
