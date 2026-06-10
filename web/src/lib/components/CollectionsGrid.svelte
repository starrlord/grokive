<script>
  import { collections, removeCollection } from '$lib/state.js';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import SearchField from './SearchField.svelte';

  let { onopen = () => {}, onplay = () => {}, onmovie = () => {} } = $props();
  let confirming = $state(null);

  // Toolbar: filter the grid by name and choose its order.
  let q = $state('');
  let sortBy = $state('recent'); // recent (store order) | name | size

  // The auto-generated "Beat Montage" collection — pinned to the top of the default
  // (recent) order wherever it sits in the stored list. Same id/name check the server uses.
  const isMontage = (c) => c.id === 'beat-montage' || c.name?.toLowerCase() === 'beat montage';

  const filtered = $derived(
    ($collections || []).filter((c) => !q.trim() || (c.name || '').toLowerCase().includes(q.trim().toLowerCase()))
  );
  const shown = $derived.by(() => {
    const list = [...filtered];
    if (sortBy === 'name') return list.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    if (sortBy === 'size') return list.sort((a, b) => (b.item_count ?? b.ids?.length ?? 0) - (a.item_count ?? a.ids?.length ?? 0));
    // 'recent' = stored order, with Beat Montage pinned on top (sort is stable, so the rest stay put).
    return list.sort((a, b) => (isMontage(b) ? 1 : 0) - (isMontage(a) ? 1 : 0));
  });

  // Count line for the cover, dropping any zero segments (e.g. "26 items · 26 videos").
  const countLabel = (c) => {
    const total = c.item_count ?? c.ids?.length ?? 0;
    const videos = c.video_count ?? 0;
    const images = c.image_count ?? 0;
    const parts = [`${total} item${total === 1 ? '' : 's'}`];
    if (videos) parts.push(`${videos} video${videos === 1 ? '' : 's'}`);
    if (images) parts.push(`${images} image${images === 1 ? '' : 's'}`);
    return parts.join(' · ');
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
  <!-- Toolbar: title + count, name filter, ordering. -->
  <div class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
    <span class="text-sm text-muted">{$collections.length} collection{$collections.length === 1 ? '' : 's'}</span>
    <div class="ml-auto flex w-full items-center gap-2 sm:w-auto">
      <SearchField bind:value={q} placeholder="Search collections…" ariaLabel="collection search"
        wrapperClass="min-w-0 flex-1 sm:w-60 sm:flex-none"
        inputClass="rounded-full border border-line bg-[var(--surface-2)] py-1.5 pl-3.5 pr-10 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
      <select bind:value={sortBy} aria-label="Sort collections" title="Sort collections"
        class="shrink-0 rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm font-semibold">
        <option value="recent">Recent</option>
        <option value="name">Name A–Z</option>
        <option value="size">Largest</option>
      </select>
    </div>
  </div>

  {#if !shown.length}
    <p class="py-16 text-center text-sm text-muted">No collections match “{q.trim()}”.</p>
  {:else}
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {#each shown as c (c.id)}
        <article class="group relative overflow-hidden rounded-card border border-line bg-[var(--surface-2)] transition-colors hover:border-[var(--accent)] focus-within:border-[var(--accent)]">
          <!-- Cover with title + count overlay. -->
          <div class="relative aspect-[4/3] w-full overflow-hidden bg-[var(--media-bg)]">
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
            <span class="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-[var(--media-scrim-strong)] to-transparent px-3 pb-3 pt-14 text-[var(--media-control-ink)]">
              <span class="block truncate text-base font-extrabold">{c.name}</span>
              <span class="block text-xs opacity-80">{countLabel(c)}</span>
            </span>
          </div>

          <!-- Full-bleed open target sits under the action buttons (which carry higher z). -->
          <button type="button" class="absolute inset-0 z-0" aria-label={`Open collection ${c.name}`} onclick={() => onopen(c)}></button>

          <!-- Secondary actions: top-right, revealed on hover / keyboard focus anywhere in the
               card (group-focus-within), always shown for touch. -->
          <div class="absolute right-2 top-2 z-10 flex gap-1.5 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100">
            {#if (c.video_count ?? 0) >= 2}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--media-control-border-hover)] hover:bg-[var(--media-control-bg-hover)]"
                title="Beat montage from these videos" aria-label="Create a beat montage from this collection's videos" onclick={() => onmovie(c)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
              </button>
            {/if}
            <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--danger-hover)] hover:bg-[var(--danger-hover)] hover:text-white"
              title="Delete collection" aria-label="Delete collection" onclick={() => (confirming = c)}>
              <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
            </button>
          </div>

          <!-- Primary action: Play videos, bottom-right, only when the collection has any. -->
          {#if c.video_count}
            <button type="button" class="absolute bottom-2 right-2 z-10 inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-[var(--on-accent)] opacity-0 shadow-lg transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
              title="Play videos" aria-label="Play collection videos" onclick={() => onplay(c)}>
              <span aria-hidden="true">▶</span> Play
            </button>
          {/if}
        </article>
      {/each}
    </div>
  {/if}
{/if}

{#if confirming}
  <ConfirmDialog title="Delete collection?"
    message={`"${confirming.name}" will be removed. The media files stay in your library.`}
    confirmLabel="Delete"
    onconfirm={() => { removeCollection(confirming.id); confirming = null; }}
    oncancel={() => (confirming = null)} />
{/if}
