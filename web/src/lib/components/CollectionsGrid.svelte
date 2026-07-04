<script>
  import { collections, removeCollection, loadCollections } from '$lib/state.js';
  import { relockCollection, relockAllCollections } from '$lib/api.js';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import SearchField from './SearchField.svelte';
  import CollectionLockModal from './CollectionLockModal.svelte';

  let { onopen = () => {}, onplay = () => {}, onqueue = () => {}, onplayqueue = () => {}, onimport = () => {} } = $props();
  let confirming = $state(null);
  let fileInput = $state(null);
  let lockModal = $state(null); // { collection, mode } -> CollectionLockModal
  function onPick(e) {
    const picked = e.currentTarget.files;
    if (picked?.length) onimport(picked);
    e.currentTarget.value = ''; // allow re-picking the same folder later
  }
  const anyUnlocked = $derived(($collections || []).some((c) => c.locked && c.unlocked));
  async function relockNow(c) { try { await relockCollection(c.id); } finally { loadCollections(); } }
  async function lockAll() { try { await relockAllCollections(); } finally { loadCollections(); } }
  const unlockHoursLeft = (c) => {
    const secs = (c.unlock_expires || 0) - Date.now() / 1000;
    return secs > 0 ? Math.max(1, Math.ceil(secs / 3600)) : 0;
  };

  // Toolbar: filter the grid by name and choose its order.
  let q = $state('');
  let sortBy = $state('recent'); // recent (store/creation order) | updated (last-modified) | name | size
  // Locked (and not unlocked this session) collections are hidden from the grid so they
  // don't clutter it — revealed only when you toggle them on. In-memory: resets to hidden
  // on every load, so they stay out of sight by default. Unlocked ones always show.
  let showLocked = $state(false);
  const isSealed = (c) => c.locked && !c.unlocked;
  const lockedHiddenCount = $derived(($collections || []).filter(isSealed).length);
  const visibleTotal = $derived(($collections || []).filter((c) => showLocked || !isSealed(c)).length);

  // The auto-generated "Beat Montage" collection — pinned to the top of the default
  // (recent) order wherever it sits in the stored list. Same id/name check the server uses.
  const isMontage = (c) => c.id === 'beat-montage' || c.name?.toLowerCase() === 'beat montage';

  const filtered = $derived(
    ($collections || []).filter((c) =>
      (showLocked || !isSealed(c)) &&
      (!q.trim() || (c.name || '').toLowerCase().includes(q.trim().toLowerCase())))
  );
  const shown = $derived.by(() => {
    const list = [...filtered];
    if (sortBy === 'name') list.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    else if (sortBy === 'size') list.sort((a, b) => (b.item_count ?? b.ids?.length ?? 0) - (a.item_count ?? a.ids?.length ?? 0));
    // 'updated' = last-modified first (updated_at bumps when items are added/removed or
    // the collection is renamed) — so a collection you just added clips to floats up,
    // unlike 'recent' which only reflects creation order. Falls back to created_at.
    else if (sortBy === 'updated') list.sort((a, b) => ((b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || '')));
    // 'recent' = stored order, with Beat Montage pinned on top (sort is stable, so the rest stay put).
    else list.sort((a, b) => (isMontage(b) ? 1 : 0) - (isMontage(a) ? 1 : 0));
    // Finally pin only SEALED (still-locked) collections to the top — and, since sealed
    // ones are hidden until "Show locked", this only takes effect once you reveal them,
    // grouping them at the front as a batch you can unlock. The sort is stable, so their
    // relative order (and the rest) is preserved. Once a collection is UNLOCKED this
    // session it is no longer sealed, so it drops back into the chosen order like any
    // other collection — meaning Recently Updated / Name / Largest actually affect it.
    return list.sort((a, b) => (isSealed(b) ? 1 : 0) - (isSealed(a) ? 1 : 0));
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

<input bind:this={fileInput} type="file" webkitdirectory multiple class="hidden" onchange={onPick} aria-hidden="true" tabindex="-1" />

{#if !$collections.length}
  <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
    <div>
      <p class="mb-1 text-lg font-bold text-ink">No collections yet</p>
      <p class="mb-4 text-sm">Select media from Recent or All Media, then add it to a collection — or import a folder from your device.</p>
      <button type="button" onclick={() => fileInput?.click()}
        class="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-bold text-[var(--on-accent)]">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4 4 4-4"/><path d="M4 14.9A6 6 0 0 1 7 4a5 5 0 0 1 9 1 4 4 0 0 1 2 7.7"/></svg>
        Import a folder
      </button>
    </div>
  </div>
{:else}
  <!-- Toolbar: title + count, name filter, ordering. -->
  <div class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
    <span class="text-sm text-muted">{visibleTotal} collection{visibleTotal === 1 ? '' : 's'}</span>
    <div class="ml-auto flex w-full flex-wrap items-center gap-2 sm:w-auto sm:flex-nowrap">
      {#if lockedHiddenCount}
        <button type="button" onclick={() => (showLocked = !showLocked)} aria-pressed={showLocked}
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-semibold transition {showLocked ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)]' : 'border-line bg-[var(--surface-2)] hover:border-[var(--accent)]'}"
          title={showLocked ? 'Hide locked collections' : 'Show locked collections'}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          {showLocked ? 'Hide' : 'Show'} locked ({lockedHiddenCount})
        </button>
        <button type="button" onclick={() => (lockModal = { collection: null, mode: 'unlock-all' })}
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]"
          title="Unlock every collection that shares one password">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
          Unlock all
        </button>
      {/if}
      {#if anyUnlocked}
        <button type="button" onclick={lockAll}
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--accent)] bg-[var(--accent)]/10 px-3 py-1.5 text-sm font-semibold text-[var(--accent)] transition hover:bg-[var(--accent)]/20"
          title="Re-lock all currently unlocked collections now">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          Lock all
        </button>
      {/if}
      <button type="button" onclick={() => fileInput?.click()}
        class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]"
        title="Import a folder of videos/images into a new collection">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4 4 4-4"/><path d="M4 14.9A6 6 0 0 1 7 4a5 5 0 0 1 9 1 4 4 0 0 1 2 7.7"/></svg>
        Import
      </button>
      <SearchField bind:value={q} placeholder="Search collections…" ariaLabel="collection search"
        wrapperClass="order-last w-full min-w-0 sm:order-none sm:w-60 sm:flex-none"
        inputClass="rounded-full border border-line bg-[var(--surface-2)] py-1.5 pl-3.5 pr-10 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
      <select bind:value={sortBy} aria-label="Sort collections" title="Sort collections"
        class="shrink-0 rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm font-semibold">
        <option value="recent">Recent</option>
        <option value="updated">Recently updated</option>
        <option value="name">Name A–Z</option>
        <option value="size">Largest</option>
      </select>
    </div>
  </div>

  {#if !shown.length}
    {#if !showLocked && lockedHiddenCount && !q.trim()}
      <div class="py-16 text-center text-sm text-muted">
        <p class="mb-3">{lockedHiddenCount} locked collection{lockedHiddenCount === 1 ? '' : 's'} hidden.</p>
        <button type="button" onclick={() => (showLocked = true)}
          class="inline-flex items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-4 py-2 font-semibold transition hover:border-[var(--accent)]">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          Show locked
        </button>
      </div>
    {:else}
      <p class="py-16 text-center text-sm text-muted">No collections match “{q.trim()}”.</p>
    {/if}
  {:else}
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {#each shown as c (c.id)}
        {@const sealed = c.locked && !c.unlocked}
        <article class="group relative overflow-hidden rounded-card border border-line bg-[var(--surface-2)] transition-colors hover:border-[var(--accent)] focus-within:border-[var(--accent)]">
          <!-- Cover with title + count overlay. -->
          <div class="relative aspect-[4/3] w-full overflow-hidden bg-[var(--media-bg)]">
            {#if sealed}
              <!-- Sealed: no thumbnails leak. A lock placeholder; click to unlock. -->
              <span class="grid h-full w-full place-items-center text-muted">
                <svg viewBox="0 0 24 24" class="h-10 w-10 opacity-70" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
              </span>
            {:else if c.covers?.length > 1}
              <span class="grid h-full w-full grid-cols-2 grid-rows-2 gap-0.5">
                {#each c.covers.slice(0, 4) as cover (cover)}
                  <img src={cover} alt="" loading="lazy" class="h-full w-full object-cover object-top transition group-hover:scale-[1.03]" />
                {/each}
              </span>
            {:else if c.cover}
              <img src={c.cover} alt="" loading="lazy" class="h-full w-full object-cover object-top transition group-hover:scale-[1.03]" />
            {:else}
              <span class="grid h-full w-full place-items-center text-sm text-muted">No cover</span>
            {/if}

            {#if c.locked && c.unlocked}
              <!-- Unlocked-for-now badge: one tap to re-lock immediately. -->
              <button type="button" class="absolute left-2 top-2 z-20 inline-flex items-center gap-1 rounded-full bg-[var(--accent)]/90 px-2 py-1 text-[11px] font-bold text-[var(--on-accent)] backdrop-blur-sm"
                title={`Unlocked — ${unlockHoursLeft(c)}h left. Click to lock now.`} aria-label="Lock now" onclick={(e) => { e.stopPropagation(); relockNow(c); }}>
                <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
                {unlockHoursLeft(c)}h
              </button>
            {/if}

            <span class="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-[var(--media-scrim-strong)] to-transparent px-3 pb-3 pt-14 text-[var(--media-control-ink)]">
              <span class="block truncate text-base font-extrabold">{c.name}</span>
              <span class="block text-xs opacity-80">{sealed ? 'Locked · ' : ''}{countLabel(c)}</span>
            </span>
          </div>

          <!-- Full-bleed open target sits under the action buttons (which carry higher z).
               A sealed collection opens the unlock prompt instead of its contents. -->
          <button type="button" class="absolute inset-0 z-0" aria-label={sealed ? `Unlock collection ${c.name}` : `Open collection ${c.name}`}
            onclick={() => sealed ? (lockModal = { collection: c, mode: 'unlock' }) : onopen(c)}></button>

          <!-- Secondary actions: top-right, revealed on hover / keyboard focus anywhere in the
               card (group-focus-within), always shown for touch. Hidden while sealed (so a
               lock can't be bypassed by deleting the collection or queueing its videos). -->
          <div class="absolute right-2 top-2 z-10 flex gap-1.5 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100">
            {#if !sealed && ((c.video_count ?? 0) >= 1 || (c.image_count ?? 0) >= 1)}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--media-control-border-hover)] hover:bg-[var(--media-control-bg-hover)]"
                title="Add this collection's videos and photos to the montage queue" aria-label="Add this collection's videos and photos to the montage queue" onclick={() => onqueue(c)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
              </button>
            {/if}
            {#if !c.locked}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--accent)] hover:text-[var(--accent)]"
                title="Lock this collection with a password" aria-label="Lock collection" onclick={() => (lockModal = { collection: c, mode: 'set' })}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
              </button>
            {:else if c.unlocked}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--accent)] hover:text-[var(--accent)]"
                title="Manage lock (re-lock now or remove the password)" aria-label="Manage lock" onclick={() => (lockModal = { collection: c, mode: 'manage' })}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
              </button>
            {/if}
            {#if !sealed}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--danger-hover)] hover:bg-[var(--danger-hover)] hover:text-white"
                title="Delete collection" aria-label="Delete collection" onclick={() => (confirming = c)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
              </button>
            {/if}
          </div>

          <!-- Primary actions: bottom-right, only when accessible and has videos. "Add to
               Play Queue" (compact, secondary) sits to the LEFT of the accent "Play" — queue
               these videos onto the cross-library Play Queue vs. play this collection now. -->
          {#if !sealed && c.video_count}
            <div class="absolute bottom-2 right-2 z-10 flex items-center gap-1.5">
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] opacity-0 shadow-lg backdrop-blur-sm transition hover:border-[var(--accent)] hover:text-[var(--accent)] group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
                title="Add this collection's videos to the Play Queue" aria-label="Add collection videos to play queue" onclick={() => onplayqueue(c)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 12H3"/><path d="M16 6H3"/><path d="M12 18H3"/><path d="m16 12 5 3-5 3v-6Z"/></svg>
              </button>
              <button type="button" class="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-[var(--on-accent)] opacity-0 shadow-lg transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
                title="Play videos" aria-label="Play collection videos" onclick={() => onplay(c)}>
                <span aria-hidden="true">▶</span> Play
              </button>
            </div>
          {:else if sealed}
            <button type="button" class="absolute bottom-2 right-2 z-10 inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-[var(--on-accent)] opacity-0 shadow-lg transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
              title="Unlock collection" aria-label="Unlock collection" onclick={() => (lockModal = { collection: c, mode: 'unlock' })}>
              <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
              Unlock
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

{#if lockModal}
  <CollectionLockModal collection={lockModal.collection} mode={lockModal.mode}
    onclose={() => (lockModal = null)} ondone={() => loadCollections()} />
{/if}
