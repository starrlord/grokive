<script>
  import { onMount } from 'svelte';
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { fetchMedia, fetchFacets, fetchLibrary, mediaByIds } from '$lib/api.js';
  import {
    filters, mode, favorites, stashed, deleted, applyLibrary,
    selectMode, selection, toggleSelection, clearSelection,
    loadPlaylists, loadCollections, loadSettings, resetAll, hasActiveFilters,
    collections, updateCollection, removeFromCollection
  } from '$lib/state.js';
  import TopBar from '$lib/components/TopBar.svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import JustifiedGrid from '$lib/components/JustifiedGrid.svelte';
  import EditorialList from '$lib/components/EditorialList.svelte';
  import Lightbox from '$lib/components/Lightbox.svelte';
  import SelectBar from '$lib/components/SelectBar.svelte';
  import PlaylistEditor from '$lib/components/PlaylistEditor.svelte';
  import CollectionsGrid from '$lib/components/CollectionsGrid.svelte';
  import CollectionPickerModal from '$lib/components/CollectionPickerModal.svelte';
  import FiltersModal from '$lib/components/FiltersModal.svelte';
  import Toaster from '$lib/components/Toaster.svelte';

  const PAGE_SIZE = 120;

  let items = $state([]);
  let total = $state(0);
  let page = $state(1);
  let loading = $state(false);
  let facets = $state({ tags: [], models: [], canvases: [] });

  let lb = $state(null); // { list, index, autoAdvance, title }
  let editing = $state(null); // playlist being edited
  let activeCollectionId = $state(null);
  let collectionItems = $state([]);
  let collectionTotal = $state(0);
  let collectionName = $state('');
  let activeCanvasName = $state('');
  let showCollectionPicker = $state(false);
  let showFilters = $state(false);
  let menuOpen = $state(false); // mobile sidebar drawer
  let sentinel = $state(null);
  let reqId = 0;

  const targetHeight = $derived($mode === 'editorial' ? 360 : 240);
  const gap = $derived($mode === 'editorial' ? 22 : 10);

  // Client-side overlay of favorites/archive so toggles feel instant; the server
  // applies the same rules on fetch for correctness across pagination.
  const displayItems = $derived.by(() => {
    const live = (it) => !$deleted.has(it.id);
    if ($filters.view === 'favorites') return items.filter((it) => $favorites.has(it.id) && live(it));
    if ($filters.view === 'archive') return items.filter((it) => $stashed.has(it.id) && live(it));
    if ($filters.view === 'all' || $filters.view === 'canvases') return items.filter(live);
    return items.filter((it) => !$stashed.has(it.id) && live(it));
  });
  const activeCollection = $derived(($collections || []).find((c) => c.id === activeCollectionId) || null);
  const activeCanvas = $derived((facets.canvases || []).find((c) => c.id === $filters.canvas) || null);
  const activeCanvasTitle = $derived(activeCanvas?.name || activeCanvasName || 'Canvas');
  const hasCanvasRefinements = $derived(!!(
    $filters.query ||
    $filters.tags.length ||
    $filters.models.length ||
    $filters.resolutions.length ||
    $filters.mediaType !== 'all' ||
    $filters.period !== 'all'
  ));
  const currentGridItems = $derived(activeCollection ? collectionItems.filter((it) => !$deleted.has(it.id)) : displayItems);
  const selectableIds = $derived(currentGridItems.map((it) => it.id));
  const byId = $derived(new Map([...items, ...collectionItems].map((it) => [it.id, it])));
  const selectionSet = $derived(new Set($selection));
  const videoSelection = $derived($selection.filter((id) => byId.get(id)?.media_type === 'video'));

  async function load(reset) {
    if (loading) return;
    loading = true;
    const mine = ++reqId;
    const nextPage = reset ? 1 : page + 1;
    try {
      const res = await fetchMedia($filters, nextPage, PAGE_SIZE);
      if (mine !== reqId) return;
      total = res.total;
      page = res.page;
      items = reset ? res.items : [...items, ...res.items];
    } catch (e) {
      console.error('media load failed', e);
    } finally {
      if (mine === reqId) loading = false;
    }
  }

  async function loadCollectionItems(collection, reset = true) {
    if (!collection) return;
    loading = true;
    const mine = ++collReq;
    const nextPage = reset ? 1 : page + 1;
    const pageSize = Math.max(PAGE_SIZE, Math.min(collection.ids?.length || PAGE_SIZE, 500));
    try {
      const res = await fetchMedia($filters, nextPage, pageSize, collection.id);
      if (mine !== collReq) return;
      collectionTotal = res.total;
      page = res.page;
      collectionItems = reset ? res.items : [...collectionItems, ...res.items];
    } catch (e) {
      console.error('collection media load failed', e);
    } finally {
      if (mine === collReq) loading = false;
    }
  }

  let sig = $state('');
  $effect(() => {
    const next = JSON.stringify($filters);
    if (next !== sig) {
      sig = next;
      if (!activeCollection) load(true);
      if (!activeCollection || $filters.view !== 'collections') refreshFacets();
    }
  });
  async function refreshFacets() {
    try { facets = await fetchFacets($filters, activeCollectionId); } catch {}
  }

  onMount(async () => {
    applyLibrary(await fetchLibrary());
    loadPlaylists();
    loadCollections();
    loadSettings();
    await refreshFacets();
  });

  let collReq = 0;
  let collSig = $state('');
  $effect(() => {
    if ($filters.view !== 'collections') {
      activeCollectionId = null;
      collectionItems = [];
      collectionTotal = 0;
      collSig = '';
      return;
    }
    if (!activeCollection) return;
    const next = JSON.stringify({
      id: activeCollection.id,
      ids: activeCollection.ids || [],
      filters: $filters
    });
    if (next === collSig) return;
    collSig = next;
    collectionName = activeCollection.name;
    refreshFacets();
    loadCollectionItems(activeCollection, true);
  });

  // Re-attach the infinite-scroll observer whenever the sentinel mounts/remounts.
  // The sentinel is destroyed and recreated as the user switches views, so keying
  // the effect on `sentinel` re-observes the fresh node and disconnects the stale
  // observer. Item counts are read inside the callback, so this only re-runs when
  // the sentinel element itself changes.
  $effect(() => {
    if (!sentinel) return;
    const io = new IntersectionObserver((entries) => {
      if (!entries[0].isIntersecting || loading) return;
      if (activeCollection) {
        if (collectionItems.length < collectionTotal) loadCollectionItems(activeCollection, false);
      } else if (items.length < total) {
        load(false);
      }
    }, { rootMargin: '900px' });
    io.observe(sentinel);
    return () => io.disconnect();
  });

  function openLightbox(item, list) {
    lb = { list, index: list.findIndex((x) => x.id === item.id), autoAdvance: false, title: '' };
  }
  function openCanvas(c) {
    activeCanvasName = c.name || 'Canvas';
    filters.update((f) => ({ ...f, view: 'canvases', canvas: c.id }));
  }
  function closeCanvas() {
    activeCanvasName = '';
    filters.update((f) => ({ ...f, view: 'canvases', canvas: null }));
  }
  function clearCanvasRefinements() {
    filters.update((f) => ({
      ...f,
      query: '',
      tags: [],
      models: [],
      resolutions: [],
      mediaType: 'all',
      period: 'all'
    }));
  }
  async function playPlaylist(pl) {
    const list = (await mediaByIds(pl.ids)).filter((v) => v.media_type === 'video');
    if (!list.length) { alert('No playable videos in this playlist.'); return; }
    lb = { list, index: 0, autoAdvance: true, title: pl.name };
  }
  function openCollection(c) {
    activeCollectionId = c.id;
  }
  async function playCollection(c) {
    const list = (await mediaByIds(c.ids)).filter((v) => v.media_type === 'video');
    if (!list.length) { alert('No playable videos in this collection.'); return; }
    lb = { list, index: 0, autoAdvance: true, title: c.name };
  }
  function saveCollectionName() {
    if (!activeCollection) return;
    const name = collectionName.trim();
    if (name && name !== activeCollection.name) updateCollection(activeCollection.id, { name });
  }
  function removeSelectionFromCollection() {
    if (!activeCollection || !$selection.length) return;
    removeFromCollection(activeCollection.id, $selection);
    clearSelection();
  }
  function playResolved(videos, title) {
    if (!videos.length) return;
    editing = null;
    lb = { list: videos, index: 0, autoAdvance: true, title };
  }
  function playSelection() {
    const list = videoSelection.map((id) => byId.get(id)).filter(Boolean);
    if (!list.length) return;
    lb = { list, index: 0, autoAdvance: true, title: `Selection (${list.length})` };
  }
  async function playCanvas(c) {
    const canvasFilters = {
      ...$filters,
      view: 'canvases',
      canvas: c.id,
      mediaType: 'video',
      query: '',
      tags: [],
      models: [],
      resolutions: [],
      period: 'all'
    };
    let nextPage = 1;
    let loaded = [];
    let expected = c.videos || PAGE_SIZE;
    do {
      const res = await fetchMedia(canvasFilters, nextPage, 500);
      loaded = [...loaded, ...(res.items || [])];
      expected = res.total || loaded.length;
      nextPage += 1;
    } while (loaded.length < expected);
    const list = loaded.filter((it) => it.media_type === 'video');
    if (!list.length) { alert('No playable videos in this canvas.'); return; }
    lb = { list, index: 0, autoAdvance: true, title: c.name || 'Canvas' };
  }
</script>

<svelte:head><title>Grokive</title></svelte:head>

<TopBar onrefresh={() => { load(true); refreshFacets(); }} onfilters={() => (showFilters = true)} onmenu={() => (menuOpen = true)} />

<div class="flex">
  <aside class="hidden w-80 shrink-0 overflow-y-auto border-r border-line lg:block" style="height: calc(100vh - 56px)">
    <Sidebar {facets} onplay={playPlaylist} onedit={(pl) => (editing = pl)} onbrowse={() => (showFilters = true)} />
  </aside>

  <main class="min-w-0 flex-1 p-3 sm:p-4" style="padding-bottom: {$selectMode ? '5rem' : 'max(1rem, env(safe-area-inset-bottom))'}">
    {#if $filters.view === 'collections' && !activeCollection}
      <CollectionsGrid onopen={openCollection} onplay={playCollection} />
    {:else if $filters.view === 'collections' && activeCollection}
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={() => (activeCollectionId = null)}>Back</button>
        <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-base font-extrabold outline-none"
          bind:value={collectionName} maxlength="80" onblur={saveCollectionName} onkeydown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }} />
        <span class="text-sm text-muted">{collectionTotal.toLocaleString()} items</span>
        <button type="button" class="rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-white disabled:opacity-50"
          disabled={!currentGridItems.some((it) => it.media_type === 'video')} onclick={() => playCollection(activeCollection)}>Play videos</button>
      </div>

      {#if currentGridItems.length === 0}
        <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
          <div>
            <p class="mb-1 text-lg font-bold text-ink">This collection is empty</p>
            <p class="text-sm">Select media from Recent or All Media to add it here.</p>
          </div>
        </div>
      {:else if $mode === 'editorial'}
        <EditorialList items={currentGridItems} onopen={openLightbox} />
      {:else}
        <JustifiedGrid items={currentGridItems} {targetHeight} {gap}
          selectMode={$selectMode} selection={selectionSet}
          onopen={openLightbox} ontoggleselect={(it) => toggleSelection(it.id)} />
      {/if}
      <div bind:this={sentinel} class="h-10"></div>
      {#if loading}<p class="py-6 text-center text-sm text-muted">Loading…</p>{/if}
    {:else if $filters.view === 'canvases' && $filters.canvas}
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={closeCanvas}>Back</button>
        <div class="min-w-0 flex-1">
          <h1 class="truncate text-base font-extrabold text-ink sm:text-lg">{activeCanvasTitle}</h1>
          <p class="text-sm text-muted">{total.toLocaleString()} items</p>
        </div>
        {#if hasCanvasRefinements}
          <button class="rounded-full border border-line px-3 py-1 text-xs font-semibold hover:border-[var(--accent)]" onclick={clearCanvasRefinements}>Reset filters ✕</button>
        {/if}
      </div>

      {#if displayItems.length === 0 && !loading}
        <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
          <div>
            <p class="mb-1 text-lg font-bold text-ink">Nothing here yet</p>
            <p class="text-sm">Try adjusting your search or filters.</p>
          </div>
        </div>
      {:else if $mode === 'editorial'}
        <EditorialList items={displayItems} onopen={openLightbox} />
      {:else}
        <JustifiedGrid items={displayItems} {targetHeight} {gap}
          selectMode={$selectMode} selection={selectionSet}
          onopen={openLightbox} ontoggleselect={(it) => toggleSelection(it.id)} />
      {/if}

      <div bind:this={sentinel} class="h-10"></div>
      {#if loading}<p class="py-6 text-center text-sm text-muted">Loading…</p>{/if}
    {:else if $filters.view === 'canvases'}
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {#each facets.canvases || [] as c (c.id)}
          <article class="group overflow-hidden rounded-card border border-line bg-[var(--surface-2)]">
            <button type="button" class="relative block aspect-square w-full overflow-hidden bg-black text-left" onclick={() => openCanvas(c)}>
              {#if c.cover}<img src={c.cover} alt="" loading="lazy" class="h-full w-full object-cover transition group-hover:scale-105" />{/if}
              <span class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 to-transparent px-3 pb-2.5 pt-8 text-white">
                <span class="block truncate text-sm font-bold">{c.name}</span>
                <span class="block text-xs opacity-80">{c.count} items · {c.videos} video</span>
              </span>
            </button>
            <div class="flex items-center gap-2 p-2">
              <button type="button" class="grid h-8 w-8 shrink-0 place-items-center rounded-sm bg-[var(--accent)] text-white disabled:opacity-45"
                title="Play videos" aria-label={`Play ${c.name} videos`} disabled={!c.videos} onclick={() => playCanvas(c)}>▶</button>
              <button type="button" class="min-w-0 flex-1 truncate text-left text-sm font-semibold hover:underline" onclick={() => openCanvas(c)}>{c.name}</button>
              <span class="text-xs text-muted">{c.videos || 0}</span>
            </div>
          </article>
        {/each}
      </div>
    {:else}
      <div class="mb-3 flex flex-wrap items-center gap-3">
        <p class="text-sm text-muted">{total.toLocaleString()} {$filters.view === 'favorites' ? 'favorites' : $filters.view === 'archive' ? 'archived' : $filters.view === 'all' ? 'items' : 'recent items'}</p>
        {#if hasActiveFilters($filters)}
          <button class="rounded-full border border-line px-3 py-1 text-xs font-semibold hover:border-[var(--accent)]" onclick={resetAll}>Reset filters ✕</button>
        {/if}
      </div>

      {#if displayItems.length === 0 && !loading}
        <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
          <div>
            <p class="mb-1 text-lg font-bold text-ink">Nothing here yet</p>
            <p class="text-sm">
              {#if $filters.view === 'favorites'}Tap the ♥ on any item to add it.
              {:else if $filters.view === 'archive'}Archived items stay saved but are hidden from Recent.
              {:else}Try adjusting your search or filters.{/if}
            </p>
          </div>
        </div>
      {:else if $mode === 'editorial'}
        <EditorialList items={displayItems} onopen={openLightbox} />
      {:else}
        <JustifiedGrid items={displayItems} {targetHeight} {gap}
          selectMode={$selectMode} selection={selectionSet}
          onopen={openLightbox} ontoggleselect={(it) => toggleSelection(it.id)} />
      {/if}

      <div bind:this={sentinel} class="h-10"></div>
      {#if loading}<p class="py-6 text-center text-sm text-muted">Loading…</p>{/if}
    {/if}
  </main>
</div>

{#if $selectMode}
  <SelectBar videoIds={videoSelection} {selectableIds} collection={activeCollection}
    onplay={playSelection}
    oncollections={() => (showCollectionPicker = true)}
    onremovefromcollection={removeSelectionFromCollection} />
{/if}

{#if lb}
  <Lightbox list={lb.list} index={lb.index} autoAdvance={lb.autoAdvance} title={lb.title} onclose={() => (lb = null)} />
{/if}

{#if editing}
  <PlaylistEditor playlist={editing} onclose={() => (editing = null)} onplay={playResolved} />
{/if}

{#if showCollectionPicker}
  <CollectionPickerModal ids={$selection} onclose={() => (showCollectionPicker = false)} />
{/if}

{#if showFilters}
  <FiltersModal {facets} onclose={() => (showFilters = false)} />
{/if}

<!-- Mobile navigation drawer: the desktop sidebar (filters + playlists + models)
     slides in from the left. lg:hidden — desktop shows the static <aside> instead. -->
<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && menuOpen) menuOpen = false; }} />
{#if menuOpen}
  <div use:portal class="fixed inset-0 z-50 lg:hidden">
    <div class="absolute inset-0 bg-black/65 backdrop-blur-sm" role="presentation"
         transition:fade={{ duration: 150 }} onclick={() => (menuOpen = false)}></div>
    <div class="absolute inset-y-0 left-0 flex w-[86vw] max-w-sm flex-col bg-[var(--surface-solid)] shadow-[8px_0_40px_rgba(0,0,0,0.5)]"
         role="dialog" aria-modal="true" aria-label="Menu" tabindex="-1"
         style="padding-top: max(0.5rem, env(safe-area-inset-top)); padding-bottom: env(safe-area-inset-bottom); padding-left: env(safe-area-inset-left)"
         transition:fly={{ x: -360, duration: 220 }}>
      <div class="flex items-center justify-between border-b border-line px-4 py-3">
        <span class="text-lg font-extrabold tracking-tight">Grokive</span>
        <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line" aria-label="Close menu" onclick={() => (menuOpen = false)}>✕</button>
      </div>
      <div class="min-h-0 flex-1 overflow-y-auto">
        <Sidebar {facets}
          onplay={(pl) => { menuOpen = false; playPlaylist(pl); }}
          onedit={(pl) => { menuOpen = false; editing = pl; }}
          onbrowse={() => { menuOpen = false; showFilters = true; }} />
      </div>
    </div>
  </div>
{/if}

<Toaster />
