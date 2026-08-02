<script>
  import { onMount } from 'svelte';
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import { toast } from '$lib/toast.js';
  import { fetchMedia, fetchFacets, fetchLibrary, mediaByIds, renameCanvas, deleteCanvas } from '$lib/api.js';
  import {
    filters, mode, favorites, stashed, deleted, applyLibrary,
    selectMode, setSelectMode, selection, toggleSelection, clearSelection,
    loadPlaylists, loadCollections, loadSettings, resetAll, hasActiveFilters,
    collections, collectionGroups, activeCollectionId, updateCollection, removeFromCollection, collectionsSettled, ensureMoviePolling, movieChip,
    galleryReload, basket, enqueueBasket, montageMode, isMontageSource, isMontageQueueable,
    playQueue, enqueuePlayQueue, shuffled
  } from '$lib/state.js';
  import TopBar from '$lib/components/TopBar.svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import JustifiedGrid from '$lib/components/JustifiedGrid.svelte';
  import EditorialList from '$lib/components/EditorialList.svelte';
  import CollectionGroups from '$lib/components/CollectionGroups.svelte';
  import Lightbox from '$lib/components/Lightbox.svelte';
  import SelectBar from '$lib/components/SelectBar.svelte';
  import ExportOrderModal from '$lib/components/ExportOrderModal.svelte';
  import PlaylistEditor from '$lib/components/PlaylistEditor.svelte';
  import LibraryView from '$lib/components/LibraryView.svelte';
  import MediaTypeTabs from '$lib/components/MediaTypeTabs.svelte';
  import SearchField from '$lib/components/SearchField.svelte';
  import SortSelect from '$lib/components/SortSelect.svelte';
  import CollectionPickerModal from '$lib/components/CollectionPickerModal.svelte';
  import GenerateMovie from '$lib/components/GenerateMovie.svelte';
  import FiltersModal from '$lib/components/FiltersModal.svelte';
  import PlaySplitButton from '$lib/components/PlaySplitButton.svelte';
  import MontageStatusChip from '$lib/components/MontageStatusChip.svelte';
  import MontageBasketChip from '$lib/components/MontageBasketChip.svelte';
  import PlayQueueChip from '$lib/components/PlayQueueChip.svelte';
  import ImportModal from '$lib/components/ImportModal.svelte';
  import ScrollToTop from '$lib/components/ScrollToTop.svelte';
  import PromptStudio from '$lib/components/PromptStudio.svelte';
  import ImagineStudio from '$lib/components/ImagineStudio.svelte';
  import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
  import Toaster from '$lib/components/Toaster.svelte';

  const PAGE_SIZE = 120;

  let items = $state([]);
  let total = $state(0);
  let page = $state(1);
  let loading = $state(false);
  let facets = $state({ tags: [], models: [], canvases: [] });

  let lb = $state(null); // { list, index, autoAdvance, title }
  // Id of the item the Lightbox is currently showing (via onitemchange) — feeds the
  // Montage-queue panel's "now viewing" row highlight while it triages above the viewer.
  let previewItemId = $state(null);
  let editing = $state(null); // playlist being edited
  // activeCollectionId now lives in the shared store (state.js) so the TopBar can read collection context.
  let collectionItems = $state([]);
  let collectionTotal = $state(0);
  let collectionName = $state('');
  let collectionGroupName = $state('');
  let activeCanvasName = $state('');
  let canvasName = $state('');          // bound to the drilled-in canvas rename input
  let confirmingCanvas = $state(null);  // the canvas pending delete-confirmation
  let canvasSig = '';                   // tracks the open canvas so we only reseed the input on change
  // Canvases landing toolbar (name filter + ordering) — mirrors the Collections grid.
  // Plain instance state is enough here: drilling into a canvas only swaps the {#if}
  // branch inside THIS component, so it survives the round-trip back to the landing.
  let canvasQuery = $state('');
  let canvasSort = $state('updated'); // updated (newest item) | recent (newest canvas) | name | size
  let showCollectionPicker = $state(false);
  let groupByBase = $state(false); // collection view: cluster items by their base image
  let showMovie = $state(false);
  let movieVideoIds = $state([]); // video ids fed to the Montage panel (selection or a collection)
  let exportOrder = $state(null); // { items, name } — drives the reorder-before-merge modal (null = closed)
  let importFiles = $state(null); // FileList from a folder Import picker (drives ImportModal)
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
  const existingCollectionGroups = $derived.by(() => {
    const byKey = {};
    for (const c of $collections || []) {
      const group = String(c.group || '').trim();
      if (group && !byKey[group.toLowerCase()]) byKey[group.toLowerCase()] = group;
    }
    for (const g of $collectionGroups || []) {
      const name = String(g.name || '').trim();
      if (name && !byKey[name.toLowerCase()]) byKey[name.toLowerCase()] = name;
    }
    return Object.values(byKey).sort((a, b) => a.localeCompare(b));
  });
  // The collections landing grid (not drilled into a collection) — chrome adapts to this.
  const onCollectionsLanding = $derived($filters.view === 'collections' && !activeCollection);
  const activeCanvas = $derived((facets.canvases || []).find((c) => c.id === $filters.canvas) || null);
  const activeCanvasTitle = $derived(activeCanvas?.name || activeCanvasName || 'Canvas');
  // Canvases landing: filter by name, then order. `updated_at`/`created_at` come from the
  // canvas's own media (newest / oldest item — see db.facets), so "Recently updated" floats
  // a canvas you just added to, while "Recent" is newest-canvas-first. ISO strings compare
  // lexicographically; a canvas missing timestamps sorts last rather than jumping to the top.
  const shownCanvases = $derived.by(() => {
    const needle = canvasQuery.trim().toLowerCase();
    const list = (facets.canvases || []).filter((c) => !needle || (c.name || '').toLowerCase().includes(needle));
    const byTime = (key) => (a, b) => (b[key] || '').localeCompare(a[key] || '');
    if (canvasSort === 'name') return [...list].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    if (canvasSort === 'size') return [...list].sort((a, b) => (b.count || 0) - (a.count || 0));
    if (canvasSort === 'recent') return [...list].sort(byTime('created_at'));
    return [...list].sort(byTime('updated_at'));
  });
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
  // Images-only ZIP export: the Export button bundles these when no video is selected
  // (a mixed selection keeps exporting the videos as an MP4 — see SelectBar).
  const imageSelection = $derived($selection.filter((id) => byId.get(id)?.media_type === 'image'));
  // Inputs for the Montage queue/panel: videos AND still images (never other montages).
  // Independent of the current mode — queuing or montaging an image ENTERS picture-video
  // mode (see the actions below), so an all-image selection must still enable +Queue /
  // Montage. videoSelection itself keeps montages — still valid to play/export/playlist.
  const montageSelectionIds = $derived($selection.filter((id) => isMontageQueueable(byId.get(id))));
  const selectionHasImage = $derived(montageSelectionIds.some((id) => byId.get(id)?.media_type === 'image'));

  async function load(reset) {
    // A reset (view/filter change) must SUPERSEDE an in-flight load, not be dropped —
    // otherwise switching to e.g. All Media while a prior fetch is still pending silently
    // skips its page-1 fetch (the sig effect never retries), leaving stale items/total and
    // dead infinite scroll until a reload. Append calls (reset=false) still coalesce.
    if (loading && !reset) return;
    const mine = ++reqId; // bump first, so any superseded in-flight load self-discards at the mine!==reqId guard
    loading = true;
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
      // The server resolves `collection` to its ids from collections.json, so a reload
      // triggered by a local mutation (e.g. Remove from collection) must wait for that
      // mutation's save to land — otherwise it reads the pre-save list and the change
      // appears to revert. settled() is already-resolved for a plain open, so no delay.
      await collectionsSettled();
      if (mine !== collReq) return;
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

  // Select mode is scoped to the current browsing context: switching view (the
  // All Media / Favorites / Library tabs), opening a canvas, or drilling into — or
  // backing out of — a collection exits select mode entirely, so the bottom action
  // bar auto-hides instead of lingering over a place you've navigated away from
  // (setSelectMode(false) also clears the selection). Within-view refinement (search,
  // tags, models) is NOT a context change — it keeps both the selection and the bar.
  let ctxSig = '';
  $effect(() => {
    const next = `${$filters.view}|${$filters.canvas ?? ''}|${$activeCollectionId ?? ''}`;
    if (ctxSig !== '' && next !== ctxSig) setSelectMode(false);
    ctxSig = next;
  });

  // Drilling into a collection/canvas swaps the page content in place (no route
  // change). The drilled view starts nearly empty (items load async), so the swap
  // shrinks the document and the browser clamps the window scroll toward 0 — the
  // landing's offset is destroyed, and Back used to dump you at the top of the
  // list. So: capture the landing's offset BEFORE the swap ($effect.pre — a
  // post-flush read would see the already-clamped value), reset to top when
  // ENTERING a drilled view, and restore the saved offset when the drill clears
  // back to the SAME view. An exit that also switches tabs lands on unrelated
  // content, so it doesn't restore. For COLLECTIONS the restore sticks: the
  // landing re-renders its full height synchronously from the $collections store
  // (aspect-ratio cards, no async pop-in). For CANVASES it's best-effort only —
  // that landing rebuilds from facets.canvases, which the drilled-in refetch
  // collapsed to one row, so the restore usually clamps to ~0 until the exit
  // refetch lands (same top-of-list landing as before this fix, not a regression).
  const drillKey = $derived($activeCollectionId
    ? `c:${$activeCollectionId}`
    : ($filters.canvas ? `v:${$filters.canvas}` : ''));
  let landingScroll = 0;
  let landingView = '';
  let preDrill = '';
  let preDrillView = '';
  $effect.pre(() => {
    const drill = drillKey;
    const view = $filters.view;
    if (drill && !preDrill && typeof window !== 'undefined') {
      // Only a drill entered FROM its own landing has a position worth returning
      // to. Entering from elsewhere (a Lightbox "In" chip on Recent switches view
      // and drills in one flush) saves top — that offset belongs to another view.
      landingScroll = preDrillView === view ? window.scrollY : 0;
      landingView = view;
    }
    preDrill = drill;
    preDrillView = view;
  });
  let prevDrill = '';
  $effect(() => {
    const drill = drillKey;
    if (drill !== prevDrill && typeof window !== 'undefined') {
      if (drill) window.scrollTo(0, 0);
      else if ($filters.view === landingView) window.scrollTo(0, landingScroll);
    }
    prevDrill = drill;
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
      // Invalidate any in-flight collection fetch: it shares `page`/`loading` with
      // load(), so letting it resolve after leaving the view would clobber the new
      // view's pagination (e.g. a tag chip jumping to All Media mid-append).
      collReq += 1;
      return;
    }
    // Clear an orphaned id (its collection was deleted here or on another device) so the
    // store reflects "landing", keeping the page and the TopBar's chrome logic in sync.
    if (!activeCollection) { $activeCollectionId = null; return; }
    const next = JSON.stringify({
      id: activeCollection.id,
      ids: activeCollection.ids || [],
      group: activeCollection.group || '',
      filters: $filters
    });
    if (next === collSig) return;
    collSig = next;
    collectionName = activeCollection.name;
    collectionGroupName = activeCollection.group || '';
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

  // Self-heal for the "missed first transition" gap. IntersectionObserver only fires on
  // intersection CHANGES, and recent/all/favorites/archive share ONE sentinel node (so the
  // observer isn't recreated on a view switch). If a reset load() lands while the sentinel
  // is already within the rootMargin — e.g. a view switch with the viewport parked, which
  // collapses the page back to one screen — that single became-intersecting event can be
  // swallowed (the callback's `loading` guard), and scroll goes inert until a reload. This
  // effect re-runs whenever a load settles (it reads loading/items/total/sentinel) and pulls
  // the next page if the sentinel is still in range; the observer keeps handling real
  // scrolling. Mirrors the group-by-base drain below; scoped to the media views (the
  // collection branch has its own loader/sentinel).
  $effect(() => {
    if (loading || activeCollection || !sentinel || items.length >= total) return;
    if (sentinel.getBoundingClientRect().top < window.innerHeight + 900) load(false);
  });

  // Group-by-base mode clusters the WHOLE collection and its families' Export/Montage
  // act on every clip — so a >500-item collection (which otherwise loads page-by-page
  // on scroll) must be drained up front, or families and their exports would be
  // silently truncated. Pull the next page whenever grouping is on and more remain;
  // bounded by collectionTotal, so it stops once everything is loaded.
  $effect(() => {
    if (!groupByBase || !activeCollection || loading) return;
    if (collectionItems.length < collectionTotal) loadCollectionItems(activeCollection, false);
  });

  function openLightbox(item, list) {
    lb = { list, index: list.findIndex((x) => x.id === item.id), autoAdvance: false, title: '' };
  }
  function openRelatedLightbox(list, index = 0, title = '') {
    lb = { list, index, autoAdvance: false, title };
  }
  function openCanvas(c) {
    activeCanvasName = c.name || 'Canvas';
    // Same clean-slate rule as entering a collection: don't carry any refinement (query,
    // tags, models, resolutions, media type, period) from the previous canvas — or
    // wherever you came from — into this one.
    filters.update((f) => ({ ...f, view: 'canvases', canvas: c.id, query: '', tags: [], models: [], resolutions: [], mediaType: 'all', period: 'all' }));
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
  // Seed the rename input from the canvas you're viewing, but only when the canvas
  // itself changes — so a facets refresh (e.g. right after a rename) doesn't clobber
  // what you're typing. Mirrors how collectionName is reseeded on collection change.
  $effect(() => {
    const cid = $filters.canvas;
    if (!cid) { canvasSig = ''; return; }
    if (cid === canvasSig) return;
    canvasSig = cid;
    canvasName = activeCanvasTitle;
  });
  async function saveCanvasName() {
    const cid = $filters.canvas;
    if (!cid) return;
    const name = canvasName.trim();
    const current = activeCanvas?.name || activeCanvasName || '';
    if (!name || name === current) { canvasName = current; return; } // empty/unchanged → revert
    try {
      await renameCanvas(cid, name);
      activeCanvasName = name; // keep the title/fallback in step before facets reload
      toast('Canvas renamed', { type: 'success' });
      await refreshFacets();
    } catch (e) {
      canvasName = current; // restore the input so it never shows an unsaved name
      toast(e?.message || 'Rename failed', { type: 'error' });
    }
  }
  async function deleteCanvasConfirmed(c) {
    const cid = c?.id || $filters.canvas;
    confirmingCanvas = null;
    if (!cid) return;
    try {
      const r = await deleteCanvas(cid);
      toast(`Canvas deleted · ${r.deleted ?? 0} item${r.deleted === 1 ? '' : 's'} removed`, { type: 'success' });
      if ($filters.canvas === cid) closeCanvas(); // drop back to the canvas landing if we were inside it
      await refreshFacets();
    } catch (e) {
      toast(e?.message || 'Delete failed', { type: 'error' });
    }
  }
  async function playPlaylist(pl) {
    const list = (await mediaByIds(pl.ids)).filter((v) => v.media_type === 'video');
    if (!list.length) { toast('No playable videos in this playlist.', { type: 'error' }); return; }
    lb = { list, index: 0, autoAdvance: true, title: pl.name };
  }
  // Entering a collection starts a clean filter slate: no refinement — query, tags,
  // models, resolutions, media type or period — from wherever you came from carries in
  // and silently filters the new collection. (Mirrors setView / openCanvas; sort is kept.)
  // Sets view too: the Lightbox's "In" chips enter from ANY view (Recent, All Media…),
  // and without it the orphan-clear effect would instantly null the id again.
  function enterCollection(id) {
    filters.update((f) => ({ ...f, view: 'collections', canvas: null, query: '', tags: [], models: [], resolutions: [], mediaType: 'all', period: 'all' }));
    $activeCollectionId = id;
  }
  function openCollection(c) {
    enterCollection(c.id);
  }
  // After a folder import lands its new collection, refresh the list and (if the user
  // clicked "Open collection") drill straight into it. The Library view is already
  // active — the Import picker lives on the collections landing — so no view switch.
  async function onImportCreated(result, open) {
    await loadCollections();
    if (open && result?.collection_id) enterCollection(result.collection_id);
  }
  // `orderedItems` = play EXACTLY this order (the drilled-in "Play videos" button hands
  // over the grid, so it honours whatever SortSelect is showing). The collection CARD has
  // no visible order to honour, and c.ids is INSERTION order — oldest-added first (see
  // server.py _collection_summaries) — so Play used to start you on whatever you added
  // longest ago. Screen newest-first there instead: same created_at desc the card's own
  // cover/mosaic already uses. Undated rows keep insertion order at the end.
  // `shuffle` ignores orderedItems on purpose and resolves the FULL id list: the drilled-in
  // grid is paginated (<=500 a page, see loadCollectionItems), so shuffling what's loaded
  // would silently randomize a slice of a big collection while claiming to span it. Ordered
  // play keeps honouring the grid — there the visible order IS the promise.
  async function playCollection(c, orderedItems = null, { shuffle = false } = {}) {
    const explicitOrder = !shuffle && Array.isArray(orderedItems);
    const source = explicitOrder ? orderedItems : await mediaByIds(c.ids);
    const videos = source.filter((v) => v.media_type === 'video');
    const list = explicitOrder
      ? videos
      : shuffle
        ? shuffled(videos)
        : [...videos].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    if (!list.length) { toast('No playable videos in this collection.', { type: 'error' }); return; }
    lb = { list, index: 0, autoAdvance: true, title: `${c.name}${shuffle ? ' · Random' : ''}` };
  }
  // Photo slideshow of a collection's images (videos skipped). Resolves the full id list
  // — not just the loaded grid — so the slideshow spans every photo, not only what's
  // scrolled into view.
  async function slideshowCollection(c) {
    const list = (await mediaByIds(c.ids)).filter((v) => v.media_type === 'image');
    if (!list.length) { toast('No photos in this collection to show.', { type: 'error' }); return; }
    lb = { list, index: 0, autoAdvance: false, autoSlideshow: true, title: c.name };
  }
  async function enqueueCollectionToBasket(c) {
    // The collection card's music icon pours every montage-eligible source in the
    // collection into the cross-library queue (basket) — so several collections can
    // be stacked into one montage. Launch happens from the basket chip. Even a single
    // source is worth adding (you may add more from elsewhere); the launch enforces >=2.
    // Videos always; still images too — queuing any image enters picture-video mode.
    const list = (await mediaByIds(c.ids)).filter((v) => isMontageQueueable(v));
    if (!list.length) { toast('This collection has no montage-eligible media.', { type: 'error' }); return; }
    enqueueBasket(list.map((v) => v.id));
    if (list.some((v) => v.media_type === 'image')) montageMode.set('picture-video');
    const noun = list.some((v) => v.media_type === 'image') ? 'item' : 'video';
    toast(`Added ${list.length} ${noun}${list.length === 1 ? '' : 's'} to the montage queue`, { type: 'success' });
  }
  // The collection card's "Add to Play Queue" button pours every VIDEO in the collection
  // onto the cross-library Play Queue — so several collections can be stacked into one
  // playback run. Videos only (stills don't play); the montage queue owns the picture path.
  async function enqueueCollectionToPlayQueue(c) {
    const list = (await mediaByIds(c.ids)).filter((v) => v.media_type === 'video');
    if (!list.length) { toast('This collection has no videos to queue.', { type: 'error' }); return; }
    enqueuePlayQueue(list.map((v) => v.id));
    toast(`Added ${list.length} video${list.length === 1 ? '' : 's'} to the Play Queue`, { type: 'success' });
  }
  // Play the cross-library Play Queue in order. Resolves ids server-side (NOT the grid-scoped
  // `byId` map) so videos queued from collections we've since left are still found; hands the
  // resolved list to the same autoAdvance Lightbox a collection/playlist uses. `startId` (a row's
  // "play from here") sets the first item; otherwise it starts at the top. The queue is left intact.
  async function playPlayQueueNow(startId = null) {
    const list = (await mediaByIds($playQueue)).filter((v) => v.media_type === 'video');
    if (!list.length) { toast('No playable videos in the Play Queue.', { type: 'error' }); return; }
    const start = startId != null ? list.findIndex((v) => String(v.id) === String(startId)) : 0;
    lb = { list, index: start < 0 ? 0 : start, autoAdvance: true, title: `Play Queue (${list.length})` };
  }
  // Pour the current video multi-select onto the Play Queue, without leaving select mode —
  // so you can keep selecting elsewhere and add more (mirrors enqueueSelectionToBasket).
  function enqueueSelectionToPlayQueue() {
    const ids = videoSelection;
    if (!ids.length) { toast('Select some videos to queue for playback.', { type: 'error' }); return; }
    enqueuePlayQueue(ids);
    toast(`Added ${ids.length} to the Play Queue`, { type: 'success' });
  }
  // Fire a montage from the cross-library queue (basket). Resolves ids server-side
  // (NOT the grid-scoped `byId` map) so clips queued from collections we've since
  // navigated away from are still found. Hands off to the montage panel the same way
  // a collection does; the basket itself is left intact so an aborted launch keeps the picks.
  // View/play a queued item from the basket panel: the panel already resolved the
  // queue via mediaByIds, so its list goes straight to the Lightbox (no refetch).
  // No autoAdvance — triage is a per-item decision, not a screening. The panel stays
  // open above the viewer (z-60 vs z-50); its rows and the viewer's own queue-toggle
  // button both remove items while previewing.
  function openBasketPreview(list, index) {
    lb = { list, index, autoAdvance: false, title: `Montage queue (${list.length})` };
  }
  async function launchBasketMontage() {
    const list = (await mediaByIds($basket)).filter((v) => isMontageSource(v, $montageMode));
    if (list.length < 2) {
      const noun = $montageMode === 'picture-video' ? 'photos or videos' : 'non-montage videos';
      toast(`A montage needs at least 2 ${noun} in the queue.`, { type: 'error' }); return;
    }
    movieVideoIds = list.map((v) => v.id);
    showMovie = true;
  }
  // Pour the current (montage-eligible) multi-select into the cross-library queue,
  // without leaving select mode — so you can keep selecting elsewhere and add more.
  function enqueueSelectionToBasket() {
    const ids = montageSelectionIds;
    if (!ids.length) { toast('Select some photos or videos to queue for montage.', { type: 'error' }); return; }
    enqueueBasket(ids);
    if (selectionHasImage) montageMode.set('picture-video'); // queuing a photo enters picture-video
    toast(`Added ${ids.length} to montage queue`, { type: 'success' });
  }
  function saveCollectionName() {
    if (!activeCollection) return;
    const name = collectionName.trim();
    if (name && name !== activeCollection.name) updateCollection(activeCollection.id, { name });
  }
  function canonicalCollectionGroup(value) {
    const clean = String(value || '').trim();
    if (!clean) return '';
    return existingCollectionGroups.find((g) => g.toLowerCase() === clean.toLowerCase()) || clean;
  }
  function saveCollectionGroup() {
    if (!activeCollection) return;
    const next = canonicalCollectionGroup(collectionGroupName);
    const current = String(activeCollection.group || '').trim();
    if (next.toLowerCase() === current.toLowerCase()) {
      collectionGroupName = current;
      return;
    }
    collectionGroupName = next;
    updateCollection(activeCollection.id, { group: next });
  }
  function removeSelectionFromCollection() {
    if (!activeCollection || !$selection.length) return;
    // Drop from the visible grid immediately so the action feels instant; the reactive
    // reload (which now awaits the save) reconciles against the server afterwards.
    const drop = new Set($selection.map(String));
    const before = collectionItems.length;
    collectionItems = collectionItems.filter((it) => !drop.has(String(it.id)));
    // Keep the total in step with the optimistic splice so the group-mode "fully loaded"
    // gate (loaded >= total) doesn't briefly flip false and flash its banner before the
    // reconciling refetch lands. The refetch corrects it to the true server count anyway.
    collectionTotal = Math.max(0, collectionTotal - (before - collectionItems.length));
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
  // Merging is order-sensitive, but selections/families arrive in the grid's sort order
  // (newest-first), not a chosen sequence — so open the reorder step and let the user
  // arrange the clips before the concat. Single-video "merges" skip it (nothing to order).
  function openExportOrder(videoItems, name) {
    const list = (videoItems || []).filter((v) => v && v.media_type === 'video');
    if (!list.length) return;
    exportOrder = { items: list, name: name || 'selection' };
  }
  function reorderSelectionExport() {
    openExportOrder(videoSelection.map((id) => byId.get(id)).filter(Boolean), activeCollection?.name || 'selection');
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
  // Photo slideshow of a canvas's images (videos skipped). Same bounded-pagination shape
  // as playCanvas, but fetches images and opens the lightbox in slideshow mode.
  async function slideshowCanvas(c) {
    const canvasFilters = {
      ...$filters, view: 'canvases', canvas: c.id, mediaType: 'image',
      query: '', tags: [], models: [], resolutions: [], period: 'all'
    };
    const CANVAS_PAGE = 500;
    let nextPage = 1;
    let loaded = [];
    let expected = Infinity;
    while (loaded.length < expected) {
      const res = await fetchMedia(canvasFilters, nextPage, CANVAS_PAGE);
      const batch = res.items || [];
      loaded = [...loaded, ...batch];
      expected = res.total || loaded.length;
      nextPage += 1;
      if (batch.length < CANVAS_PAGE) break;
      if (nextPage > Math.ceil(expected / CANVAS_PAGE) + 1) break;
    }
    const list = loaded.filter((it) => it.media_type === 'image');
    if (!list.length) { toast('No photos in this canvas to show.', { type: 'error' }); return; }
    lb = { list, index: 0, autoAdvance: false, autoSlideshow: true, title: c.name || 'Canvas' };
  }
  // Gather EVERY video matching a filter set, page by page. Same bounded-pagination shape
  // as playCanvas: stop at `total`, bail on a short page, and hard-cap pages so a
  // mismatched `total` can't spin into an endless fetch. Shared by the view Play menu and
  // the top-bar Play so their reach is identical — only the ordering differs.
  async function gatherVideos(filterSet) {
    const PAGE = 500;
    let nextPage = 1;
    let loaded = [];
    let expected = Infinity;
    while (loaded.length < expected) {
      const res = await fetchMedia(filterSet, nextPage, PAGE);
      const batch = res.items || [];
      loaded = [...loaded, ...batch];
      expected = res.total || loaded.length;
      nextPage += 1;
      if (batch.length < PAGE) break;
      if (nextPage > Math.ceil(expected / PAGE) + 1) break;
    }
    return loaded.filter((it) => it.media_type === 'video');
  }
  async function playCurrentView({ shuffle = false } = {}) {
    // Play every video in the CURRENT media view (Recent / All Media / Favorites /
    // Archive), honoring the active search + filters; scoped by $filters.view server-side
    // (favorites/archive stay within their set). Default order is the grid's own sort — so
    // a search's results play as a queue. `shuffle` (the Play menu's "Play random")
    // randomizes instead, but only AFTER the full gather: the pages come back in a stable
    // server-side sort, and a random ORDER BY across pages would dupe and drop rows.
    const list = await gatherVideos({ ...$filters, canvas: null, mediaType: 'video' });
    if (!list.length) { toast('No videos to play here.', { type: 'error' }); return; }
    const scope = $filters.query
      ? `“${$filters.query}”`
      : ($filters.view === 'favorites' ? 'Favorites' : $filters.view === 'archive' ? 'Archive' : $filters.view === 'all' ? 'All Media' : 'Recent');
    lb = {
      list: shuffle ? shuffled(list) : list,
      index: 0,
      autoAdvance: true,
      title: `${scope}${shuffle ? ' · Random' : ''} (${list.length})`
    };
  }
  async function playRandomLibrary() {
    // "Play" in the top bar: shuffle every video on disk into a random queue and hand
    // it to the same Lightbox we use for collections. view 'all' deliberately includes
    // archived (db.query_media), so this spans the whole library. Deliberately CLEARS the
    // refinements — that's what separates it from the view toolbar's "Play random", which
    // shuffles only what your filters/search currently match.
    const list = await gatherVideos({
      ...$filters,
      view: 'all',
      canvas: null,
      mediaType: 'video',
      query: '',
      tags: [],
      models: [],
      resolutions: [],
      period: 'all'
    });
    if (!list.length) { toast('No videos to play yet.', { type: 'error' }); return; }
    lb = { list: shuffled(list), index: 0, autoAdvance: true, title: `Random (${list.length})` };
  }
</script>

<svelte:head><title>Grokive</title></svelte:head>

<TopBar onrefresh={() => { activeCollection ? loadCollectionItems(activeCollection, true) : load(true); refreshFacets(); }} onfilters={() => (showFilters = true)} onmenu={() => (menuOpen = true)} onplay={playRandomLibrary} />

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
        onopencollection={openCollection} onplaycollection={playCollection} onqueuecollection={enqueueCollectionToBasket}
        onplayqueuecollection={enqueueCollectionToPlayQueue}
        onimportcollection={(files) => (importFiles = files)}
        onplayplaylist={playPlaylist} oneditplaylist={(pl) => (editing = pl)} />
    {:else if $filters.view === 'collections' && activeCollection}
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-sm font-semibold transition hover:border-[var(--accent)]" onclick={() => ($activeCollectionId = null)}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
          Back
        </button>
        <!-- Title-styled name (transparent until hovered/focused) with a count pill snug
             beside it — reads as a heading on one line, not an empty form box. The input
             auto-hugs its text via a hidden grid "sizer" span (cross-browser, unlike
             field-sizing which iOS Safari lacks): both stack in one grid cell whose width
             is the text's, so the pill sits right after the name and follows live renames.
             min-w-0 + overflow-hidden let it shrink and scroll for very long names. -->
        <!-- Below sm the Group field drops to its own line (order-last, after the pill):
             it can't shrink under 8rem, so sharing a phone-width line with the name
             chopped the name off. Wrap is mobile-only — on sm+ the name's shrink keeps
             everything on one line, and enabling wrap there would kick Group + pill to
             a new line for long names instead. -->
        <div class="flex min-w-0 flex-1 flex-wrap items-center gap-2 sm:flex-nowrap">
          <label class="relative grid min-w-0 max-w-full items-center overflow-hidden">
            <span aria-hidden="true" class="name-sizer invisible whitespace-pre rounded-lg border border-transparent px-1.5 py-1 text-base font-extrabold sm:text-lg">{collectionName || ' '}</span>
            <input class="absolute inset-0 h-full w-full rounded-lg border border-transparent bg-transparent px-1.5 py-1 text-base font-extrabold text-ink outline-none transition hover:border-line focus:border-[var(--accent)] focus:bg-[var(--surface-2)] sm:text-lg"
              aria-label="Collection name" title="Rename collection" bind:value={collectionName} maxlength="80" onblur={saveCollectionName} onkeydown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }} />
          </label>
          <label class="order-last flex w-full min-w-[8rem] max-w-48 shrink items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1 text-sm sm:order-none sm:w-auto">
            <span class="text-muted">Group</span>
            <input class="min-w-0 flex-1 bg-transparent font-semibold text-ink outline-none placeholder:text-muted"
              aria-label="Collection group" title="Collection group" placeholder="None" list="collection-group-options"
              bind:value={collectionGroupName} maxlength="120" onblur={saveCollectionGroup} onkeydown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }} />
          </label>
          <datalist id="collection-group-options">
            {#each existingCollectionGroups as group (group)}
              <option value={group}></option>
            {/each}
          </datalist>
          <span class="shrink-0 rounded-full border border-line bg-[var(--surface-2)] px-2.5 py-0.5 text-sm font-semibold text-muted tabular-nums" title={`${collectionTotal.toLocaleString()} items`}>{collectionTotal.toLocaleString()}</span>
        </div>
        <!-- One control cluster: right-aligned on desktop, a tidy full-width wrapping strip
             on mobile — instead of ml-auto on the tabs alone (which stranded them hard-right
             with a dead gap once the row wrapped). -->
        <div class="flex w-full flex-wrap items-center gap-2 sm:ml-auto sm:w-auto">
          <MediaTypeTabs />
          <SortSelect />
          <!-- Cluster the collection's clips by the base image each was generated from,
               so related videos (and their source still) gather into one family you can
               merge-export or montage in a click. -->
          <button type="button" aria-pressed={groupByBase}
            class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-2 text-sm font-semibold transition {groupByBase ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
            title="Group related videos by their base image" onclick={() => (groupByBase = !groupByBase)}>
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
            Group
          </button>
          <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-bold transition hover:border-[var(--accent)] disabled:opacity-50"
            disabled={!currentGridItems.some((it) => it.media_type === 'image')}
            title="Play a photo slideshow of this collection's images" onclick={() => slideshowCollection(activeCollection)}>Slideshow</button>
          <!-- Ordered play hands over the grid (honours SortSelect); random resolves the
               collection's full id list instead, so it spans everything, not the loaded page. -->
          <PlaySplitButton
            disabled={!currentGridItems.some((it) => it.media_type === 'video')}
            title="Play this collection's videos"
            orderHint="In the order shown"
            randomHint="Shuffle the whole collection"
            onorder={() => playCollection(activeCollection, currentGridItems)}
            onrandom={() => playCollection(activeCollection, null, { shuffle: true })} />
        </div>
      </div>

      {#if currentGridItems.length === 0}
        <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
          <div>
            <p class="mb-1 text-lg font-bold text-ink">This collection is empty</p>
            <p class="text-sm">Select media from Recent or All Media to add it here.</p>
          </div>
        </div>
      {:else if groupByBase}
        <CollectionGroups items={currentGridItems} mode={$mode} {targetHeight} {gap}
          selectMode={$selectMode} loaded={collectionItems.length} total={collectionTotal}
          onopen={openLightbox} ontoggleselect={(it) => toggleSelection(it.id)}
          onplay={(videos, title) => playResolved(videos, title)}
          onexport={(videos, label) => openExportOrder(videos, label)}
          onmontage={(ids) => { movieVideoIds = ids; showMovie = true; }} />
      {:else if $mode === 'editorial'}
        <EditorialList items={currentGridItems} onopen={openLightbox} />
      {:else}
        <JustifiedGrid items={currentGridItems} {targetHeight} {gap}
          virtualize={currentGridItems.length >= 300}
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
          <input class="w-full rounded-lg border border-transparent bg-transparent px-1.5 py-1 text-base font-extrabold text-ink outline-none transition hover:border-line focus:border-[var(--accent)] focus:bg-[var(--surface-2)] sm:text-lg"
            aria-label="Canvas name" title="Rename canvas" bind:value={canvasName} maxlength="120"
            onblur={saveCanvasName} onkeydown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }} />
          <p class="px-1.5 text-sm text-muted">{displayTotal.toLocaleString()} items</p>
        </div>
        <MediaTypeTabs class="ml-auto" />
        <SortSelect />
        {#if hasCanvasRefinements}
          <button class="rounded-full border border-line px-3 py-1 text-xs font-semibold hover:border-[var(--accent)]" onclick={clearCanvasRefinements}>Reset filters ✕</button>
        {/if}
        <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-bold transition hover:border-[var(--accent)] disabled:opacity-50"
          disabled={!displayItems.some((it) => it.media_type === 'image')}
          title="Play a photo slideshow of this canvas's images"
          onclick={() => slideshowCanvas(activeCanvas || { id: $filters.canvas, name: activeCanvasTitle })}>Slideshow</button>
        <button type="button" class="rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-[var(--on-accent)] disabled:opacity-50"
          disabled={!(activeCanvas?.videos || displayItems.some((it) => it.media_type === 'video'))}
          onclick={() => playCanvas(activeCanvas || { id: $filters.canvas, name: activeCanvasTitle, videos: displayTotal })}>Play videos</button>
        <button type="button" class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-line transition hover:border-[var(--danger-hover)] hover:bg-[var(--danger-hover)] hover:text-white"
          title="Delete canvas" aria-label="Delete this canvas"
          onclick={() => (confirmingCanvas = activeCanvas || { id: $filters.canvas, name: activeCanvasTitle })}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
        </button>
      </div>

      {#if displayItems.length === 0 && !loading}
        <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
          <div>
            <p class="mb-1 text-lg font-bold text-ink">Nothing here yet</p>
            <p class="text-sm">Try adjusting your search or filters.</p>
          </div>
        </div>
      {:else if $mode === 'editorial'}
        <EditorialList items={displayItems} onopen={openLightbox} filterable />
      {:else}
        <JustifiedGrid items={displayItems} {targetHeight} {gap}
          selectMode={$selectMode}
          onopen={openLightbox} ontoggleselect={(it) => toggleSelection(it.id)} />
      {/if}

      <div bind:this={sentinel} class="h-10"></div>
      {#if loading}<p class="py-6 text-center text-sm text-muted">Loading…</p>{/if}
    {:else if $filters.view === 'canvases'}
      <!-- Toolbar: count, name filter, ordering — same shape as the Collections landing. -->
      <div class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
        <span class="text-sm text-muted">{shownCanvases.length} canvas{shownCanvases.length === 1 ? '' : 'es'}</span>
        <div class="ml-auto flex w-full flex-wrap items-center gap-2 sm:w-auto sm:flex-nowrap">
          <SearchField bind:value={canvasQuery} placeholder="Search canvases…" ariaLabel="canvas search"
            wrapperClass="order-last w-full min-w-0 sm:order-none sm:w-60 sm:flex-none"
            inputClass="rounded-full border border-line bg-[var(--surface-2)] py-1.5 pl-3.5 pr-10 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
          <select bind:value={canvasSort} aria-label="Sort canvases" title="Sort canvases"
            class="shrink-0 rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm font-semibold">
            <option value="updated">Recently updated</option>
            <option value="recent">Recent</option>
            <option value="name">Name A–Z</option>
            <option value="size">Largest</option>
          </select>
        </div>
      </div>

      {#if !shownCanvases.length && canvasQuery.trim()}
        <p class="py-16 text-center text-sm text-muted">No canvases match “{canvasQuery.trim()}”.</p>
      {/if}
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {#each shownCanvases as c (c.id)}
          <article class="group relative overflow-hidden rounded-card border border-line bg-[var(--surface-2)]">
            <button type="button" class="relative block aspect-square w-full overflow-hidden bg-[var(--media-bg)] text-left" onclick={() => openCanvas(c)}>
              {#if c.cover}<img src={c.cover} alt="" loading="lazy" class="h-full w-full object-cover object-top transition group-hover:scale-105" />{/if}
              <span class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-[var(--media-scrim)] to-transparent px-3 pb-2.5 pt-8 text-[var(--media-control-ink)]">
                <span class="block truncate text-sm font-bold" title={c.name}>{c.name}</span>
                <span class="block text-xs opacity-80">{c.count} items · {c.videos} video</span>
              </span>
            </button>
            <!-- Hover/focus (always-on for touch): delete the whole canvas. -->
            <div class="absolute right-2 top-2 z-10 flex gap-1.5 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100">
              <button type="button" class="grid h-8 w-8 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--danger-hover)] hover:bg-[var(--danger-hover)] hover:text-white"
                title="Delete canvas" aria-label={`Delete canvas ${c.name}`} onclick={(e) => { e.stopPropagation(); confirmingCanvas = c; }}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
              </button>
            </div>
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
        <SortSelect />
        <!-- Primary click plays the view in its current sort; the caret adds "Play random",
             which shuffles the same filtered/searched set (not the whole library — that's
             what the top-bar Play is for). -->
        <PlaySplitButton
          disabled={!displayItems.some((it) => it.media_type === 'video')}
          title={$filters.query ? 'Play videos matching your search' : 'Play all videos in this view'}
          orderHint={$filters.sort === 'old' ? 'Oldest first' : $filters.sort === 'new' ? 'Newest first' : 'Current sort order'}
          randomHint="Shuffle everything this view matches"
          onorder={() => playCurrentView()}
          onrandom={() => playCurrentView({ shuffle: true })} />
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
        <EditorialList items={displayItems} onopen={openLightbox} filterable />
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
  <SelectBar videoIds={videoSelection} imageIds={imageSelection} montageIds={montageSelectionIds} {selectableIds} collection={activeCollection}
    onplay={playSelection}
    onreorderexport={reorderSelectionExport}
    oncollections={() => (showCollectionPicker = true)}
    onmovie={() => { movieVideoIds = montageSelectionIds; if (selectionHasImage) montageMode.set('picture-video'); showMovie = true; }}
    onbasket={enqueueSelectionToBasket}
    onplayqueue={enqueueSelectionToPlayQueue}
    onremovefromcollection={removeSelectionFromCollection} />
{/if}

{#if lb}
  <Lightbox list={lb.list} index={lb.index} autoAdvance={lb.autoAdvance} autoSlideshow={lb.autoSlideshow} title={lb.title}
    onopenrelated={openRelatedLightbox} onopencollection={enterCollection} onitemchange={(id) => (previewItemId = id)}
    onclose={() => { lb = null; previewItemId = null; }} />
{/if}

{#if editing}
  <PlaylistEditor playlist={editing} onclose={() => (editing = null)} onplay={playResolved} />
{/if}

{#if exportOrder}
  <ExportOrderModal items={exportOrder.items} name={exportOrder.name} onclose={() => (exportOrder = null)} />
{/if}

{#if showCollectionPicker}
  <CollectionPickerModal ids={$selection} currentCollection={activeCollection} onclose={() => (showCollectionPicker = false)} />
{/if}

{#if confirmingCanvas}
  <ConfirmDialog title="Delete canvas?"
    message={`"${confirmingCanvas.name}"${confirmingCanvas.count ? ` and its ${confirmingCanvas.count} item${confirmingCanvas.count === 1 ? '' : 's'}` : ' and all its media'} will be permanently deleted from your library — the files are removed, not just the grouping. This can't be undone.`}
    confirmLabel="Delete canvas"
    onconfirm={() => deleteCanvasConfirmed(confirmingCanvas)}
    oncancel={() => (confirmingCanvas = null)} />
{/if}

{#if showMovie}
  <!-- Leaving the montage panel exits select mode AND clears the selection so the
       bar doesn't linger and stale picks don't resurface on re-entry. The render
       keeps going and stays reachable via the chip (which uses the job, not the
       live selection). -->
  <GenerateMovie videoIds={movieVideoIds} onclose={() => { showMovie = false; setSelectMode(false); clearSelection(); }} />
{/if}

{#if importFiles}
  <ImportModal files={importFiles} onclose={() => (importFiles = null)} oncreated={onImportCreated} />
{/if}

<!-- Always-on background-task indicator for the montage render: persists across
     views/select-mode until the result is committed or dismissed; click reopens. -->
<MontageStatusChip onopen={() => { movieVideoIds = montageSelectionIds; showMovie = true; }} />

<!-- Cross-library Montage queue: floating chip (bottom-left) that surfaces clips
     gathered across collections/views and launches the montage from them. -->
<MontageBasketChip onmontage={launchBasketMontage} onview={openBasketPreview} previewing={!!lb} previewId={previewItemId} />

<!-- Cross-library Play Queue: playback sibling of the Montage basket. Floating chip
     (bottom-left, stacked above the montage chip when both are present) that surfaces
     videos gathered across collections/views and plays them in the Lightbox. -->
<PlayQueueChip onplay={playPlayQueueNow} />

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
