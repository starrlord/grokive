<script>
  import { onMount } from 'svelte';
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import { toast } from '$lib/toast.js';
  import { fetchMedia, fetchFacets, fetchLibrary, mediaByIds } from '$lib/api.js';
  import {
    filters, mode, favorites, stashed, deleted, applyLibrary,
    selectMode, setSelectMode, selection, toggleSelection, clearSelection,
    loadPlaylists, loadCollections, loadSettings, resetAll, hasActiveFilters,
    collections, activeCollectionId, updateCollection, removeFromCollection, ensureMoviePolling, movieChip,
    galleryReload
  } from '$lib/state.js';
  import TopBar from '$lib/components/TopBar.svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import JustifiedGrid from '$lib/components/JustifiedGrid.svelte';
  import EditorialList from '$lib/components/EditorialList.svelte';
  import Lightbox from '$lib/components/Lightbox.svelte';
  import SelectBar from '$lib/components/SelectBar.svelte';
  import PlaylistEditor from '$lib/components/PlaylistEditor.svelte';
  import LibraryView from '$lib/components/LibraryView.svelte';
  import MediaTypeTabs from '$lib/components/MediaTypeTabs.svelte';
  import CollectionPickerModal from '$lib/components/CollectionPickerModal.svelte';
  import GenerateMovie from '$lib/components/GenerateMovie.svelte';
  import FiltersModal from '$lib/components/FiltersModal.svelte';
  import MontageStatusChip from '$lib/components/MontageStatusChip.svelte';
  import ScrollToTop from '$lib/components/ScrollToTop.svelte';
  import PromptStudio from '$lib/components/PromptStudio.svelte';
  import ImagineStudio from '$lib/components/ImagineStudio.svelte';
  import Toaster from '$lib/components/Toaster.svelte';

  const PAGE_SIZE = 120;

  let items = $state([]);
  let total = $state(0);
  let page = $state(1);
  let loading = $state(false);
  let facets = $state({ tags: [], models: [], canvases: [] });

  let lb = $state(null); // { list, index, autoAdvance, title }
  let editing = $state(null); // playlist being edited
  // activeCollectionId now lives in the shared store (state.js) so the TopBar can read collection context.
  let collectionItems = $state([]);
  let collectionTotal = $state(0);
  let collectionName = $state('');
  let activeCanvasName = $state('');
  let showCollectionPicker = $state(false);
  let showMovie = $state(false);
  let movieVideoIds = $state([]); // video ids fed to the Montage panel (selection or a collection)
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
  // The server `total` is the count it returned at fetch time. Client-side overlay
  // actions (archive, favorite, delete) hide loaded items instantly without a
  // refetch, so the header count goes stale (e.g. "2 recent items" after archiving
  // both). Subtract the loaded items the overlay now hides to keep it honest — only
  // loaded/visible items can be acted on, so unloaded ones are unaffected.
  const displayTotal = $derived(Math.max(displayItems.length, total - (items.length - displayItems.length)));
  const activeCollection = $derived(($collections || []).find((c) => c.id === $activeCollectionId) || null);
  // The collections landing grid (not drilled into a collection) — chrome adapts to this.
  const onCollectionsLanding = $derived($filters.view === 'collections' && !activeCollection);
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
  const videoSelection = $derived($selection.filter((id) => byId.get(id)?.media_type === 'video'));
  // Inputs for the Montage panel: videos only, and never other montages (a montage
  // can't be a source clip). videoSelection itself keeps montages — they're still
  // valid to play / export / add to a playlist.
  const montageVideoIds = $derived(videoSelection.filter((id) => byId.get(id)?.model !== 'Beat Montage'));

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
      // Studio is an authoring tool, not a media view — no fetch to make.
      if ($filters.view === 'studio' || $filters.view === 'imagine') return;
      if (!activeCollection) load(true);
      if (!activeCollection || $filters.view !== 'collections') refreshFacets();
    }
  });

  // Selection is scoped to the current browsing context: switching view, opening a
  // canvas, or drilling into a collection clears it so the action bar never carries
  // items from a place you've navigated away from. Within-view refinement (search,
  // tags, models) keeps the selection. Select mode itself stays on.
  let ctxSig = '';
  $effect(() => {
    const next = `${$filters.view}|${$filters.canvas ?? ''}|${$activeCollectionId ?? ''}`;
    if (ctxSig !== '' && next !== ctxSig) clearSelection();
    ctxSig = next;
  });
  async function refreshFacets() {
    try { facets = await fetchFacets($filters, $activeCollectionId); } catch {}
  }

  // Refetch the current media view after a Grok Imagine generation ingests new
  // media (the index is rebuilt server-side first, so a fresh fetch includes it).
  let _reloadSig = 0;
  $effect(() => {
    const n = $galleryReload;
    if (n === _reloadSig) return;
    _reloadSig = n;
    if (n === 0 || $filters.view === 'studio' || $filters.view === 'imagine') return;
    if (activeCollection) loadCollectionItems(activeCollection, true);
    else load(true);
    refreshFacets();
  });

  onMount(async () => {
    applyLibrary(await fetchLibrary());
    loadPlaylists();
    loadCollections();
    loadSettings();
    // Detect any montage render already in flight (e.g. started in another tab or
    // before a reload) so the Montage button animates and the panel can reconnect.
    ensureMoviePolling();
    await refreshFacets();
  });

  // Server-owned lists (collections, playlists) are fetched once at startup and held in stores, so a
  // change made on another device while this tab is backgrounded (e.g. a collection renamed on mobile)
  // would otherwise stay stale until a full reload. Re-pull them when the tab regains focus/visibility.
  let lastSharedRefresh = 0;
  function refreshSharedLists() {
    const now = Date.now();
    if (now - lastSharedRefresh < 4000) return; // throttle rapid focus/blur flaps
    lastSharedRefresh = now;
    loadCollections();
    loadPlaylists();
  }
  const onVisible = () => { if (document.visibilityState === 'visible') refreshSharedLists(); };

  let collReq = 0;
  let collSig = $state('');
  $effect(() => {
    if ($filters.view !== 'collections') {
      $activeCollectionId = null;
      collectionItems = [];
      collectionTotal = 0;
      collSig = '';
      return;
    }
    // Clear an orphaned id (its collection was deleted here or on another device) so the
    // store reflects "landing", keeping the page and the TopBar's chrome logic in sync.
    if (!activeCollection) { $activeCollectionId = null; return; }
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
  function openRelatedLightbox(list, index = 0, title = '') {
    lb = { list, index, autoAdvance: false, title };
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
    if (!list.length) { toast('No playable videos in this playlist.', { type: 'error' }); return; }
    lb = { list, index: 0, autoAdvance: true, title: pl.name };
  }
  function openCollection(c) {
    $activeCollectionId = c.id;
  }
  async function playCollection(c, orderedItems = null) {
    const source = Array.isArray(orderedItems) ? orderedItems : await mediaByIds(c.ids);
    const list = source.filter((v) => v.media_type === 'video');
    if (!list.length) { toast('No playable videos in this collection.', { type: 'error' }); return; }
    lb = { list, index: 0, autoAdvance: true, title: c.name };
  }
  async function montageCollection(c) {
    // Feed every video in the collection to the Montage panel — same as selecting
    // them all, but without entering select mode.
    const list = (await mediaByIds(c.ids)).filter((v) => v.media_type === 'video' && v.model !== 'Beat Montage');
    if (list.length < 2) { toast('A montage needs at least 2 non-montage videos in the collection.', { type: 'error' }); return; }
    movieVideoIds = list.map((v) => v.id);
    showMovie = true;
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
    const CANVAS_PAGE = 500;
    let nextPage = 1;
    let loaded = [];
    let expected = c.videos || PAGE_SIZE;
    // Bound the loop: stop once we have everything `total` promised, but also bail
    // on an empty page and cap total pages so a server that reports a `total` higher
    // than the rows it returns can never spin this into an infinite fetch.
    while (loaded.length < expected) {
      const res = await fetchMedia(canvasFilters, nextPage, CANVAS_PAGE);
      const batch = res.items || [];
      loaded = [...loaded, ...batch];
      expected = res.total || loaded.length;
      nextPage += 1;
      if (batch.length < CANVAS_PAGE) break; // last (or short/empty) page — done
      if (nextPage > Math.ceil(expected / CANVAS_PAGE) + 1) break; // hard safety cap
    }
    const list = loaded.filter((it) => it.media_type === 'video');
    if (!list.length) { toast('No playable videos in this canvas.', { type: 'error' }); return; }
    lb = { list, index: 0, autoAdvance: true, title: c.name || 'Canvas' };
  }
</script>

<svelte:head><title>Grokive</title></svelte:head>

<TopBar onrefresh={() => { load(true); refreshFacets(); }} onfilters={() => (showFilters = true)} onmenu={() => (menuOpen = true)} />

<div class="flex">
  <!-- Studio is its own full-width workspace; the media-browsing sidebar (filters,
       playlists) doesn't apply there, so hide it for that view. -->
  {#if $filters.view !== 'studio' && $filters.view !== 'imagine' && !onCollectionsLanding}
    <aside class="hidden w-80 shrink-0 overflow-y-auto border-r border-line lg:block" style="height: calc(100dvh - 56px)">
      <Sidebar {facets} onbrowse={() => (showFilters = true)} />
    </aside>
  {/if}

  <main class="min-w-0 flex-1 p-3 sm:p-4" style="padding-bottom: {$selectMode ? '5rem' : 'max(1rem, env(safe-area-inset-bottom))'}">
    {#if $filters.view === 'collections' && !activeCollection}
      <LibraryView
        onopencollection={openCollection} onplaycollection={playCollection} onmoviecollection={montageCollection}
        onplayplaylist={playPlaylist} oneditplaylist={(pl) => (editing = pl)} />
    {:else if $filters.view === 'collections' && activeCollection}
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-sm font-semibold transition hover:border-[var(--accent)]" onclick={() => ($activeCollectionId = null)}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
          Back
        </button>
        <div class="min-w-[12rem] flex-1 sm:max-w-md">
          <input class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-base font-extrabold outline-none"
            aria-label="Collection name" bind:value={collectionName} maxlength="80" onblur={saveCollectionName} onkeydown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }} />
        </div>
        <span class="whitespace-nowrap text-sm text-muted">{collectionTotal.toLocaleString()} items</span>
        <MediaTypeTabs class="ml-auto" />
        <button type="button" class="rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-[var(--on-accent)] disabled:opacity-50"
          disabled={!currentGridItems.some((it) => it.media_type === 'video')} onclick={() => playCollection(activeCollection, currentGridItems)}>Play videos</button>
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
          selectMode={$selectMode}
          onopen={openLightbox} ontoggleselect={(it) => toggleSelection(it.id)} />
      {/if}
      <div bind:this={sentinel} class="h-10"></div>
      {#if loading}<p class="py-6 text-center text-sm text-muted">Loading…</p>{/if}
    {:else if $filters.view === 'canvases' && $filters.canvas}
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-sm font-semibold transition hover:border-[var(--accent)]" onclick={closeCanvas}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
          Back
        </button>
        <div class="min-w-0 flex-1">
          <h1 class="line-clamp-2 text-base font-extrabold text-ink sm:text-lg" title={activeCanvasTitle}>{activeCanvasTitle}</h1>
          <p class="text-sm text-muted">{displayTotal.toLocaleString()} items</p>
        </div>
        <MediaTypeTabs class="ml-auto" />
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
          selectMode={$selectMode}
          onopen={openLightbox} ontoggleselect={(it) => toggleSelection(it.id)} />
      {/if}

      <div bind:this={sentinel} class="h-10"></div>
      {#if loading}<p class="py-6 text-center text-sm text-muted">Loading…</p>{/if}
    {:else if $filters.view === 'canvases'}
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {#each facets.canvases || [] as c (c.id)}
          <article class="group overflow-hidden rounded-card border border-line bg-[var(--surface-2)]">
            <button type="button" class="relative block aspect-square w-full overflow-hidden bg-[var(--media-bg)] text-left" onclick={() => openCanvas(c)}>
              {#if c.cover}<img src={c.cover} alt="" loading="lazy" class="h-full w-full object-cover transition group-hover:scale-105" />{/if}
              <span class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-[var(--media-scrim)] to-transparent px-3 pb-2.5 pt-8 text-[var(--media-control-ink)]">
                <span class="block truncate text-sm font-bold" title={c.name}>{c.name}</span>
                <span class="block text-xs opacity-80">{c.count} items · {c.videos} video</span>
              </span>
            </button>
            <div class="flex items-center gap-2 p-2">
              <button type="button" class="grid h-8 w-8 shrink-0 place-items-center rounded-sm bg-[var(--accent)] text-[var(--on-accent)] disabled:opacity-45"
                title="Play videos" aria-label={`Play ${c.name} videos`} disabled={!c.videos} onclick={() => playCanvas(c)}>▶</button>
              <button type="button" class="min-w-0 flex-1 truncate text-left text-sm font-semibold hover:underline" title={c.name} onclick={() => openCanvas(c)}>{c.name}</button>
              <span class="text-xs text-muted">{c.videos || 0}</span>
            </div>
          </article>
        {/each}
      </div>
    {:else if $filters.view === 'studio'}
      <PromptStudio />
    {:else if $filters.view === 'imagine'}
      <ImagineStudio />
    {:else}
      <div class="mb-3 flex flex-wrap items-center gap-3">
        <p class="text-sm text-muted">{displayTotal.toLocaleString()} {$filters.view === 'favorites' ? 'favorites' : $filters.view === 'archive' ? 'archived' : $filters.view === 'all' ? 'items' : 'recent items'}</p>
        {#if hasActiveFilters($filters)}
          <button class="rounded-full border border-line px-3 py-1 text-xs font-semibold hover:border-[var(--accent)]" onclick={resetAll}>Reset filters ✕</button>
        {/if}
        <MediaTypeTabs class="ml-auto" />
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
          selectMode={$selectMode}
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
    onmovie={() => { movieVideoIds = montageVideoIds; showMovie = true; }}
    onremovefromcollection={removeSelectionFromCollection} />
{/if}

{#if lb}
  <Lightbox list={lb.list} index={lb.index} autoAdvance={lb.autoAdvance} title={lb.title}
    onopenrelated={openRelatedLightbox} onclose={() => (lb = null)} />
{/if}

{#if editing}
  <PlaylistEditor playlist={editing} onclose={() => (editing = null)} onplay={playResolved} />
{/if}

{#if showCollectionPicker}
  <CollectionPickerModal ids={$selection} onclose={() => (showCollectionPicker = false)} />
{/if}

{#if showMovie}
  <!-- Leaving the montage panel exits select mode AND clears the selection so the
       bar doesn't linger and stale picks don't resurface on re-entry. The render
       keeps going and stays reachable via the chip (which uses the job, not the
       live selection). -->
  <GenerateMovie videoIds={movieVideoIds} onclose={() => { showMovie = false; setSelectMode(false); clearSelection(); }} />
{/if}

<!-- Always-on background-task indicator for the montage render: persists across
     views/select-mode until the result is committed or dismissed; click reopens. -->
<MontageStatusChip onopen={() => { movieVideoIds = montageVideoIds; showMovie = true; }} />

<!-- Back-to-top: appears after scrolling down a long grid. Lifted above the bottom
     SelectBar (select mode) and the montage chip so it never sits under either. -->
<ScrollToTop lift={$selectMode || !!$movieChip} />

{#if showFilters}
  <FiltersModal {facets} onclose={() => (showFilters = false)} />
{/if}

<!-- Mobile navigation drawer: the desktop sidebar (filters + playlists + models)
     slides in from the left. lg:hidden — desktop shows the static <aside> instead. -->
<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && menuOpen) menuOpen = false; }} onfocus={refreshSharedLists} />
<svelte:document onvisibilitychange={onVisible} />
{#if menuOpen}
  <div use:portal class="fixed inset-0 z-50 lg:hidden">
    <div class="absolute inset-0 bg-[var(--overlay)] backdrop-blur-sm" role="presentation"
         transition:fade={{ duration: 150 }} onclick={() => (menuOpen = false)}></div>
    <div class="absolute inset-y-0 left-0 flex w-[86vw] max-w-sm flex-col bg-[var(--surface-solid)] shadow-[var(--shadow-drawer)]"
         role="dialog" aria-modal="true" aria-label="Menu" tabindex="-1"
         style="padding-top: max(0.5rem, env(safe-area-inset-top)); padding-bottom: env(safe-area-inset-bottom); padding-left: env(safe-area-inset-left)"
         use:trapFocus transition:fly={{ x: -360, duration: 220 }}>
      <div class="flex items-center justify-between border-b border-line px-4 py-3">
        <span class="text-lg font-extrabold tracking-tight">Grokive</span>
        <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line" aria-label="Close menu" onclick={() => (menuOpen = false)}>✕</button>
      </div>
      <div class="min-h-0 flex-1 overflow-y-auto">
        <Sidebar {facets}
          onbrowse={() => { menuOpen = false; showFilters = true; }} />
      </div>
    </div>
  </div>
{/if}

<Toaster />
