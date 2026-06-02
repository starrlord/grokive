<script>
  import { onMount } from 'svelte';
  import { fetchMedia, fetchFacets, fetchLibrary, mediaByIds } from '$lib/api.js';
  import {
    filters, mode, favorites, stashed, deleted, applyLibrary,
    selectMode, selection, toggleSelection,
    loadPlaylists, loadSettings, resetAll, hasActiveFilters
  } from '$lib/state.js';
  import TopBar from '$lib/components/TopBar.svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import JustifiedGrid from '$lib/components/JustifiedGrid.svelte';
  import EditorialList from '$lib/components/EditorialList.svelte';
  import Lightbox from '$lib/components/Lightbox.svelte';
  import SelectBar from '$lib/components/SelectBar.svelte';
  import PlaylistEditor from '$lib/components/PlaylistEditor.svelte';
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
  let showFilters = $state(false);
  let sentinel = $state(null);
  let reqId = 0;

  const targetHeight = $derived($mode === 'editorial' ? 360 : 240);
  const gap = $derived($mode === 'editorial' ? 22 : 10);

  // Client-side overlay of favorites/stashed so toggles feel instant; the server
  // applies the same rules on fetch for correctness across pagination.
  const displayItems = $derived.by(() => {
    const live = (it) => !$deleted.has(it.id);
    if ($filters.view === 'favorites') return items.filter((it) => $favorites.has(it.id) && live(it));
    if ($filters.view === 'stashed') return items.filter((it) => $stashed.has(it.id) && live(it));
    return items.filter((it) => !$stashed.has(it.id) && live(it));
  });
  const byId = $derived(new Map(items.map((it) => [it.id, it])));
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

  let sig = $state('');
  $effect(() => {
    const next = JSON.stringify($filters);
    if (next !== sig) {
      sig = next;
      load(true);
      if ($filters.view !== 'canvases') refreshFacets();
    }
  });
  async function refreshFacets() {
    try { facets = await fetchFacets(); } catch {}
  }

  onMount(async () => {
    applyLibrary(await fetchLibrary());
    loadPlaylists();
    loadSettings();
    await refreshFacets();
  });

  // Re-attach the infinite-scroll observer whenever the sentinel mounts/remounts.
  // The sentinel only exists outside the Canvases view, so it's destroyed and
  // recreated as the user switches views — keying the effect on `sentinel` ($state)
  // re-observes the fresh node and disconnects the stale observer. `items`/`total`/
  // `loading` are read inside the callback (not tracked), so this only re-runs when
  // the sentinel element itself changes.
  $effect(() => {
    if (!sentinel) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && items.length < total && !loading) load(false);
    }, { rootMargin: '900px' });
    io.observe(sentinel);
    return () => io.disconnect();
  });

  function openLightbox(item, list) {
    lb = { list, index: list.findIndex((x) => x.id === item.id), autoAdvance: false, title: '' };
  }
  function openCanvas(c) {
    filters.update((f) => ({ ...f, view: 'files', canvas: c.id }));
  }
  async function playPlaylist(pl) {
    const list = (await mediaByIds(pl.ids)).filter((v) => v.media_type === 'video');
    if (!list.length) { alert('No playable videos in this playlist.'); return; }
    lb = { list, index: 0, autoAdvance: true, title: pl.name };
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
</script>

<svelte:head><title>Grokive</title></svelte:head>

<TopBar onrefresh={() => { load(true); refreshFacets(); }} onfilters={() => (showFilters = true)} />

<div class="flex">
  <Sidebar {facets} onplay={playPlaylist} onedit={(pl) => (editing = pl)} onbrowse={() => (showFilters = true)} />

  <main class="min-w-0 flex-1 p-3 sm:p-4" style="padding-bottom: {$selectMode ? '5rem' : 'max(1rem, env(safe-area-inset-bottom))'}">
    {#if $filters.view === 'canvases'}
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {#each facets.canvases || [] as c (c.id)}
          <button type="button" class="group relative aspect-square overflow-hidden rounded-card bg-surface-2 text-left" onclick={() => openCanvas(c)}>
            {#if c.cover}<img src={c.cover} alt="" loading="lazy" class="h-full w-full object-cover transition group-hover:scale-105" />{/if}
            <span class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 to-transparent px-3 pb-2.5 pt-8 text-white">
              <span class="block truncate text-sm font-bold">{c.name}</span>
              <span class="block text-xs opacity-80">{c.count} files · {c.videos} video</span>
            </span>
          </button>
        {/each}
      </div>
    {:else}
      <div class="mb-3 flex flex-wrap items-center gap-3">
        <p class="text-sm text-muted">{total.toLocaleString()} {$filters.view === 'favorites' ? 'favorites' : $filters.view === 'stashed' ? 'stashed' : 'items'}</p>
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
              {:else if $filters.view === 'stashed'}Select items and Stash them to tuck them away here.
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
  <SelectBar videoIds={videoSelection} onplay={playSelection} />
{/if}

{#if lb}
  <Lightbox list={lb.list} index={lb.index} autoAdvance={lb.autoAdvance} title={lb.title} onclose={() => (lb = null)} />
{/if}

{#if editing}
  <PlaylistEditor playlist={editing} onclose={() => (editing = null)} onplay={playResolved} />
{/if}

{#if showFilters}
  <FiltersModal {facets} onclose={() => (showFilters = false)} />
{/if}

<Toaster />
