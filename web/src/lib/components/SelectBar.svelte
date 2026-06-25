<script>
  import { selection, filters, setFavorites, setStashed, addPlaylist, setSelectMode, clearSelection, addSelection, removeMedia, movieJob } from '$lib/state.js';
  import { exportSelection, exportImagesZip } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import ParticleField from './ParticleField.svelte';

  // A montage render runs server-side, so its progress is global. While it's in
  // flight the Montage button animates (and stays clickable regardless of the
  // current selection) so it doubles as a live indicator + a way back into the panel.
  const movieRunning = $derived($movieJob.running);
  const moviePct = $derived(Math.round(($movieJob.progress || 0) * 100));

  let { videoIds = [], imageIds = [], selectableIds = [], collection = null, onplay = () => {}, oncollections = () => {}, onremovefromcollection = () => {}, onmovie = () => {}, onbasket = () => {} } = $props();
  let name = $state('');
  let busy = $state(false);
  let confirmingDelete = $state(false);

  // Both dropdowns are position:fixed so they escape the dock's overflow-x clip —
  // which means they have to be placed by hand. `left: 50%` pinned them to the
  // viewport centre (nowhere near the button); instead, measure the trigger when the
  // menu opens and anchor the popover just above it, clamped so a wide one never
  // spills off the right edge. Null when closed (the popover is display:none then).
  let selectPos = $state(null);
  let playlistPos = $state(null);
  function anchorAbove(detailsEl, estWidth) {
    const r = detailsEl.querySelector('summary').getBoundingClientRect();
    const left = Math.max(8, Math.min(Math.round(r.left), window.innerWidth - estWidth - 8));
    return { left, bottom: Math.round(window.innerHeight - r.top + 8) };
  }

  function doDelete() {
    const ids = [...$selection];
    confirmingDelete = false;
    if (!ids.length) return;
    removeMedia(ids);
    setSelectMode(false);
  }

  const view = $derived($filters.view);
  const n = $derived($selection.length);
  const visibleIds = $derived((selectableIds || []).map(String).filter(Boolean));
  const unselectedVisibleIds = $derived(visibleIds.filter((id) => !$selection.includes(id)));
  const batchCount = $derived(Math.min(25, unselectedVisibleIds.length));

  function selectIds(ids, label) {
    const list = ids.filter(Boolean);
    if (!list.length) return;
    addSelection(list);
    toast(`${label} selected`, { type: 'success' });
  }

  function selectVisible() {
    selectIds(unselectedVisibleIds, `${unselectedVisibleIds.length} visible item${unselectedVisibleIds.length === 1 ? '' : 's'}`);
  }

  function selectBatch() {
    selectIds(unselectedVisibleIds.slice(0, 25), `${batchCount} item${batchCount === 1 ? '' : 's'}`);
  }

  function favAll() { setFavorites($selection, view !== 'favorites'); clearSelection(); }
  function stashAll() { setStashed($selection, view !== 'archive'); clearSelection(); }
  function save() {
    if (!name.trim() || !videoIds.length) return;
    addPlaylist(name.trim(), videoIds);
    name = '';
    setSelectMode(false);
  }
  // Images-only selection exports a store-only ZIP; any video present keeps the
  // existing MP4-merge path (the images are ignored, exactly as before).
  const imagesOnly = $derived(!videoIds.length && imageIds.length > 0);
  async function doExport() {
    if (!videoIds.length && !imageIds.length) return;
    busy = true;
    try {
      if (videoIds.length) {
        await exportSelection(videoIds);
        toast(`Exported selection (${videoIds.length})`, { type: 'success' });
      } else {
        await exportImagesZip(imageIds, collection?.name || '');
        toast(`Exported ${imageIds.length} image${imageIds.length === 1 ? '' : 's'} (ZIP)`, { type: 'success' });
      }
    } catch (e) {
      toast(e.message || 'Export failed.', { type: 'error' });
    } finally {
      busy = false;
    }
  }
</script>

<!-- Solid (not glass) surface: this is the primary action bar and sits over a dense
     thumbnail grid, so legibility wins over translucency. -->
<div class="select-shell fixed inset-x-0 bottom-0 z-40 px-3 py-3"
     class:is-exporting={busy}
     style="padding-bottom: max(0.75rem, env(safe-area-inset-bottom))">
  <div class="select-dock mx-auto flex items-center overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
    <div class="select-count">
      <span class="select-badge">{n}</span>
      <span class="select-count-label">selected</span>
    </div>

    <div class="select-cluster">
      <details class="select-menu relative"
               ontoggle={(e) => { selectPos = e.currentTarget.open ? anchorAbove(e.currentTarget, 208) : null; }}>
        <summary class="select-btn select-trigger cursor-pointer list-none [&::-webkit-details-marker]:hidden"
                 title="Select visible or batch items">
          <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="m9 12 2 2 4-4"/></svg>
          <span>Select</span>
          <svg viewBox="0 0 24 24" class="select-caret h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
        </summary>
        <!-- Fixed (not absolute) so it escapes the dock's overflow-x clipping;
             anchored to the trigger via anchorAbove() on open (see script). -->
        <div class="select-popover" style={selectPos ? `left:${selectPos.left}px;bottom:${selectPos.bottom}px` : ''}>
          <button type="button" class="block w-full rounded-md px-3 py-2 text-left text-sm font-semibold hover:bg-[var(--surface-2)] disabled:opacity-45"
            disabled={!unselectedVisibleIds.length} onclick={(e) => { selectVisible(); e.currentTarget.closest('details').open = false; }}>Visible ({unselectedVisibleIds.length})</button>
          <button type="button" class="block w-full rounded-md px-3 py-2 text-left text-sm font-semibold hover:bg-[var(--surface-2)] disabled:opacity-45"
            disabled={!batchCount} onclick={(e) => { selectBatch(); e.currentTarget.closest('details').open = false; }}>{unselectedVisibleIds.length <= 25 ? 'Remaining' : 'Next 25'} ({batchCount})</button>
        </div>
      </details>
      <button class="select-btn select-primary"
              disabled={!videoIds.length} onclick={() => onplay()} title="Play selected videos in order">
        <span class="text-xs">▶</span> Play{videoIds.length > 1 ? ` (${videoIds.length})` : ''}
      </button>
    </div>

    <div class="select-cluster">
      <button class="select-btn" onclick={favAll}>{view === 'favorites' ? 'Unfavorite' : 'Favorite'}</button>
      <button class="select-btn" onclick={() => oncollections()} title="Add to Collection">+ Collection</button>
      {#if collection}
        <button class="select-btn" onclick={() => onremovefromcollection()} title="Remove from Collection">Remove</button>
      {/if}
      <button class="select-btn" onclick={stashAll}>{view === 'archive' ? 'Restore' : 'Archive'}</button>
    </div>

    <div class="select-cluster">
      <details class="playlist-menu relative"
               ontoggle={(e) => { playlistPos = e.currentTarget.open ? anchorAbove(e.currentTarget, 280) : null; }}>
        <summary class="select-btn cursor-pointer list-none [&::-webkit-details-marker]:hidden">
          Playlist
        </summary>
        <div class="playlist-popover" style={playlistPos ? `left:${playlistPos.left}px;bottom:${playlistPos.bottom}px` : ''}>
          <input class="playlist-input" placeholder="Playlist name" bind:value={name} maxlength="80" />
          <button class="select-btn select-primary" disabled={!name.trim() || !videoIds.length} onclick={save} title="Save playlist">Save</button>
        </div>
      </details>
      <button class="select-btn export-btn" class:export-active={busy}
              disabled={busy || (!videoIds.length && !imageIds.length)} onclick={doExport} aria-busy={busy}
              title={imagesOnly ? 'Download the selected images as a .zip' : 'Merge & download the selected videos as one MP4'}>
        {#if busy}
          <span class="export-orbit" aria-hidden="true"></span>
          <span>{imagesOnly ? 'Zipping…' : 'Preparing MP4'}</span>
        {:else}
          <span class="text-xs">⇩</span>
          <span>{imagesOnly ? 'Export ZIP' : 'Export'}</span>
        {/if}
      </button>
      <button class="select-btn" disabled={!videoIds.length} onclick={() => onbasket()}
              title="Add the selected videos to the cross-library Montage queue">
        <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
        + Queue
      </button>
      <button class="select-btn montage-btn" class:montage-running={movieRunning}
              disabled={videoIds.length < 2 && !movieRunning} onclick={() => onmovie()}
              title={movieRunning ? `Montage rendering — ${moviePct}%. Click to view progress.` : 'Generate a beat-synced montage from the selected videos'}>
        {#if movieRunning}
          <ParticleField active={false} count={9} scale={0.42} layers={1} class="montage-particles" />
        {/if}
        <span class="montage-label">
          <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          {movieRunning ? `Rendering ${moviePct}%` : 'Montage'}
        </span>
      </button>
    </div>

    <div class="select-cluster">
      <button class="select-btn select-danger" disabled={!n} onclick={() => (confirmingDelete = true)}>Delete</button>
      <button class="select-btn" onclick={() => clearSelection()}>Clear</button>
      <button class="select-btn" onclick={() => setSelectMode(false)}>Done</button>
    </div>
  </div>
</div>

{#if confirmingDelete}
  <ConfirmDialog title={`Delete ${n} item${n === 1 ? '' : 's'}?`}
    message="The files are permanently removed from disk and won't be re-downloaded on future syncs."
    confirmLabel="Delete" onconfirm={doDelete} oncancel={() => (confirmingDelete = false)} />
{/if}

<style>
  .select-shell {
    background: linear-gradient(
      to top,
      var(--bg) 0%,
      color-mix(in srgb, var(--bg) 92%, transparent) 62%,
      transparent 100%
    );
    pointer-events: none;
  }

  .select-dock {
    background: color-mix(in srgb, var(--surface-solid) 88%, var(--surface-tint) 12%);
    border: 1px solid color-mix(in srgb, var(--line) 86%, transparent);
    border-radius: var(--r-2xl);
    box-shadow:
      var(--shadow-dock),
      inset 0 1px 0 var(--surface-highlight);
    gap: 0.35rem;
    max-width: min(100%, 74rem);
    padding: 0.45rem;
    pointer-events: auto;
    position: relative;
    width: max-content;
    -webkit-overflow-scrolling: touch;
  }

  .is-exporting .select-dock::before {
    animation: export-sheen 1.25s linear infinite;
    background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 34%, transparent), transparent);
    content: '';
    inset: 0;
    pointer-events: none;
    position: absolute;
    transform: translateX(-100%);
  }

  .select-count,
  .select-cluster {
    align-items: center;
    display: flex;
    flex: 0 0 auto;
  }

  .select-count {
    background: color-mix(in srgb, var(--accent) 13%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent) 24%, transparent);
    border-radius: 999px;
    gap: 0.45rem;
    min-height: 2.35rem;
    padding: 0.25rem 0.8rem 0.25rem 0.25rem;
  }

  .select-count-label {
    color: var(--ink);
    font-size: 0.8125rem;
    font-weight: 850;
    white-space: nowrap;
  }

  .select-cluster {
    gap: 0.25rem;
    padding-left: 0.35rem;
  }

  .select-cluster + .select-cluster {
    border-left: 1px solid color-mix(in srgb, var(--line) 66%, transparent);
    margin-left: 0.25rem;
    padding-left: 0.6rem;
  }

  .select-badge {
    background: var(--accent);
    border-radius: 999px;
    color: var(--on-accent);
    display: grid;
    font-size: 0.75rem;
    font-weight: 900;
    height: 1.75rem;
    min-width: 1.75rem;
    padding: 0 0.375rem;
    place-items: center;
  }

  .select-btn,
  .playlist-input {
    border: 1px solid transparent;
    border-radius: var(--r-lg);
    flex: 0 0 auto;
    font-size: 0.8125rem;
    font-weight: 750;
    min-height: 2.25rem;
    padding: 0.52rem 0.72rem;
    transition: border-color 160ms ease, background 160ms ease, color 160ms ease, opacity 160ms ease;
    white-space: nowrap;
  }

  .select-btn {
    align-items: center;
    display: inline-flex;
    gap: 0.4rem;
    justify-content: center;
    line-height: 1;
  }

  .select-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--surface-2) 72%, transparent);
    border-color: color-mix(in srgb, var(--line) 86%, transparent);
  }

  .select-btn:disabled {
    cursor: default;
    opacity: 0.48;
  }

  .select-primary {
    background: var(--accent);
    border-color: transparent;
    color: var(--on-accent);
  }

  /* The Select dropdown trigger. The flat .select-btn made it vanish into the
     bar, so it gets a chip treatment: accent-tinted fill + border that echoes
     the "N selected" count pill (so count + trigger read as one selection
     zone), plus an icon and a caret that mark it as a menu. */
  .select-trigger {
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border-color: color-mix(in srgb, var(--accent) 34%, transparent);
    color: var(--ink);
    gap: 0.45rem;
    padding-left: 0.62rem;
    padding-right: 0.58rem;
  }

  .select-trigger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--accent) 22%, transparent);
    border-color: color-mix(in srgb, var(--accent) 52%, transparent);
  }

  /* Filled while the menu is open, so the trigger clearly owns the popover. */
  .select-menu[open] .select-trigger {
    background: var(--accent);
    border-color: transparent;
    color: var(--on-accent);
  }

  .select-caret {
    opacity: 0.85;
    transition: transform 180ms ease;
  }

  .select-menu[open] .select-caret {
    transform: rotate(180deg);
  }

  .select-danger {
    border-color: color-mix(in srgb, var(--danger) 52%, transparent);
    color: var(--danger-ink);
  }

  .select-danger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--danger) 12%, transparent);
    border-color: var(--danger);
  }

  .playlist-input {
    background: color-mix(in srgb, var(--surface-2) 92%, var(--media-bg));
    border-color: var(--line);
    color: var(--ink);
    font-weight: 600;
    outline: none;
    width: 12rem;
  }

  .playlist-popover {
    align-items: center;
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: var(--r-xl);
    bottom: calc(max(0.75rem, env(safe-area-inset-bottom)) + 4.5rem);
    box-shadow: var(--shadow);
    display: flex;
    gap: 0.45rem;
    padding: 0.5rem;
    position: fixed;
    z-index: 55;
  }

  .playlist-menu:not([open]) .playlist-popover {
    display: none;
  }

  /* Same fixed-popover trick as the playlist menu so the dropdown isn't clipped
     by the dock's overflow-x-auto (clicking Select… would otherwise do nothing). */
  .select-popover {
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: var(--r-xl);
    bottom: calc(max(0.75rem, env(safe-area-inset-bottom)) + 4.5rem);
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 12rem;
    padding: 0.4rem;
    position: fixed;
    z-index: 55;
  }

  .select-menu:not([open]) .select-popover {
    display: none;
  }

  .export-btn {
    min-width: 7rem;
    overflow: hidden;
    position: relative;
  }

  .export-active {
    animation: export-pulse 900ms ease-in-out infinite alternate;
    background: linear-gradient(135deg, var(--accent), var(--success));
    border-color: transparent;
    color: var(--on-accent);
  }

  .export-orbit {
    animation: export-spin 800ms linear infinite;
    border: 2px solid var(--spinner-track);
    border-top-color: var(--on-accent);
    border-radius: 999px;
    height: 0.9rem;
    width: 0.9rem;
  }

  @keyframes export-sheen {
    to { transform: translateX(100%); }
  }

  @keyframes export-spin {
    to { transform: rotate(360deg); }
  }

  @keyframes export-pulse {
    from { box-shadow: 0 0 0 color-mix(in srgb, var(--accent) 0%, transparent); }
    to { box-shadow: 0 0 24px color-mix(in srgb, var(--accent) 42%, transparent); }
  }

  /* Montage button doubles as a live render indicator: themed particles drift over
     it and it glows while a montage is rendering server-side. */
  .montage-btn {
    overflow: hidden;
    position: relative;
  }

  .montage-label {
    align-items: center;
    display: inline-flex;
    gap: 0.4rem;
    position: relative;
    z-index: 1;
  }

  .montage-running {
    animation: montage-glow 1100ms ease-in-out infinite alternate;
    background: linear-gradient(135deg,
      color-mix(in srgb, var(--accent) 88%, transparent),
      color-mix(in srgb, #a78bfa 72%, transparent));
    border-color: transparent;
    color: var(--on-accent);
  }

  .montage-running:hover:not(:disabled) {
    background: linear-gradient(135deg,
      color-mix(in srgb, var(--accent) 96%, transparent),
      color-mix(in srgb, #a78bfa 82%, transparent));
    border-color: transparent;
  }

  :global(.montage-particles) {
    inset: 0;
    pointer-events: none;
    position: absolute;
    z-index: 0;
  }

  @keyframes montage-glow {
    from { box-shadow: 0 0 0 color-mix(in srgb, var(--accent) 0%, transparent); }
    to { box-shadow: 0 0 22px color-mix(in srgb, var(--accent) 55%, transparent); }
  }

  @media (min-width: 1536px) {
    .select-shell {
      left: 20rem;
    }
    
    .select-dock {
      max-width: min(100%, 82rem);
    }
  }

  /* Touch / narrow viewports (phones + iPad portrait): a single horizontally
     scrolling row hides options off-screen with no visible affordance on iOS, so
     the bar reads as broken. Wrap instead — every action stays reachable without
     scrolling. `display: contents` on the clusters lets their buttons flow as
     direct flex children of the dock so wrapping happens button-by-button (a
     cluster on its own would overflow its own row); the cluster dividers simply
     don't render, which is fine here. */
  @media (max-width: 900px) {
    .select-shell {
      padding-left: 0.5rem;
      padding-right: 0.5rem;
    }

    .select-dock {
      border-radius: var(--r-xl);
      flex-wrap: wrap;
      justify-content: center;
      max-height: min(60vh, 20rem);
      max-width: 100%;
      overflow-x: visible;
      overflow-y: auto;
      row-gap: 0.4rem;
      width: 100%;
    }

    .select-cluster {
      display: contents;
    }

    .playlist-input {
      width: 10rem;
    }

  }
</style>
