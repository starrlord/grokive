<script>
  import { collections, removeCollection } from '$lib/state.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { onopen = () => {}, onplay = () => {}, onmovie = () => {} } = $props();
  let confirming = $state(null);

  // Pin the auto-generated "Beat Montage" collection to the top, wherever it sits in
  // the stored order (new collections prepend, which would otherwise push it down).
  // Same id/name check the server uses (server.py _commit_montage). Array.sort is
  // stable, so every other collection keeps its existing relative order.
  const isMontage = (c) => c.id === 'beat-montage' || c.name?.toLowerCase() === 'beat montage';
  const sorted = $derived(
    [...$collections].sort((a, b) => (isMontage(b) ? 1 : 0) - (isMontage(a) ? 1 : 0))
  );

  const countLabel = (c) => {
    const total = c.item_count ?? c.ids?.length ?? 0;
    const videos = c.video_count ?? 0;
    const images = c.image_count ?? 0;
    return `${total} item${total === 1 ? '' : 's'} · ${videos} video${videos === 1 ? '' : 's'} · ${images} image${images === 1 ? '' : 's'}`;
  };
</script>

{#if !$collections.length}
  <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
    <div>
      <p class="mb-1 text-lg font-bold text-ink">No collections yet</p>
      <p class="text-sm">Select media from Recent or All Media, then add it to a collection.</p>
    </div>
  </div>
{:else}
  <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
    {#each sorted as c (c.id)}
      <article class="group overflow-hidden rounded-card border border-line bg-[var(--surface-2)]">
        <button type="button" class="relative block aspect-[4/3] w-full overflow-hidden bg-[var(--media-bg)] text-left" onclick={() => onopen(c)}>
          {#if c.covers?.length > 1}
            <span class="grid h-full w-full grid-cols-2 grid-rows-2 gap-0.5">
              {#each c.covers.slice(0, 4) as cover (cover)}
                <img src={cover} alt="" loading="lazy" class="h-full w-full object-cover transition group-hover:scale-[1.03]" />
              {/each}
            </span>
          {:else if c.cover}
            <img src={c.cover} alt="" loading="lazy" class="h-full w-full object-cover transition group-hover:scale-[1.03]" />
          {:else}
            <span class="grid h-full w-full place-items-center text-sm text-muted">No cover</span>
          {/if}
          <span class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-[var(--media-scrim-strong)] to-transparent px-3 pb-3 pt-14 text-[var(--media-control-ink)]">
            <span class="block truncate text-base font-extrabold">{c.name}</span>
            <span class="block text-xs opacity-80">{countLabel(c)}</span>
          </span>
        </button>
        <div class="flex items-center gap-2 p-2">
          <button type="button" class="grid h-8 w-8 shrink-0 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-sm bg-[var(--accent)] text-[var(--on-accent)] disabled:opacity-45"
            title="Play videos" aria-label="Play collection videos" disabled={!c.video_count} onclick={() => onplay(c)}>▶</button>
          <button type="button" class="grid h-8 w-8 shrink-0 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-sm border border-line text-muted transition hover:border-[var(--accent)] hover:text-[var(--accent)] disabled:opacity-45"
            title="Beat montage from these videos" aria-label="Create a beat montage from this collection's videos"
            disabled={(c.video_count ?? 0) < 2} onclick={() => onmovie(c)}>
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          </button>
          <button type="button" class="min-w-0 flex-1 truncate text-left text-sm font-semibold hover:underline" onclick={() => onopen(c)}>{c.name}</button>
          <span class="text-xs text-muted">{c.item_count ?? c.ids?.length ?? 0}</span>
          <button type="button" class="grid h-8 w-8 shrink-0 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-sm border border-line text-muted transition hover:border-[var(--danger-border-strong)] hover:bg-[var(--danger-bg)] hover:text-[var(--danger-ink)]"
            title="Delete collection" aria-label="Delete collection" onclick={() => (confirming = c)}>
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
          </button>
        </div>
      </article>
    {/each}
  </div>
{/if}

{#if confirming}
  <ConfirmDialog title="Delete collection?"
    message={`"${confirming.name}" will be removed. The media files stay in your library.`}
    confirmLabel="Delete"
    onconfirm={() => { removeCollection(confirming.id); confirming = null; }}
    oncancel={() => (confirming = null)} />
{/if}
