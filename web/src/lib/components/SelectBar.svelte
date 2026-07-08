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

  // montageIds: selected sources eligible for a montage — videos AND still images (queuing
  // an image enters picture-video mode). videoIds stays video-only for Play/Playlist/Export.
  let { videoIds = [], imageIds = [], montageIds = [], selectableIds = [], collection = null, onplay = () => {}, onreorderexport = () => {}, oncollections = () => {}, onremovefromcollection = () => {}, onmovie = () => {}, onbasket = () => {}, onplayqueue = () => {} } = $props();
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
    // 2+ videos: hand off to the reorder-before-merge step (order decides the output).
    // A lone video or an images-only selection has no order to choose, so it exports here.
    if (videoIds.length > 1) { onreorderexport(); return; }
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
          <span class="btn-word">Select</span>
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
        <span class="play-glyph">▶</span><span class="btn-word"> Play{videoIds.length > 1 ? ` (${videoIds.length})` : ''}</span>
      </button>
    </div>

    <div class="select-cluster">
      <button class="select-btn" onclick={favAll}
              title={view === 'favorites' ? 'Unfavorite' : 'Favorite'} aria-label={view === 'favorites' ? 'Unfavorite' : 'Favorite'}>
        <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill={view === 'favorites' ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/></svg>
      </button>
      <button class="select-btn" onclick={() => oncollections()} title="Add to Collection" aria-label="Add to Collection">
        <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/><path d="M12 10v6"/><path d="M9 13h6"/></svg>
      </button>
      {#if collection}
        <button class="select-btn" onclick={() => onremovefromcollection()} title="Remove from Collection" aria-label="Remove from Collection">
          <svg viewBox="0 0 24 24" class="mobile-only-icon h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/><path d="M9 13h6"/></svg>
          <span class="btn-word">Remove</span>
        </button>
      {/if}
      <button class="select-btn" onclick={stashAll}
              title={view === 'archive' ? 'Restore from Archive' : 'Archive'} aria-label={view === 'archive' ? 'Restore from Archive' : 'Archive'}>
        <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><path d="M10 12h4"/></svg>
      </button>
    </div>

    <div class="select-cluster">
      <details class="playlist-menu relative"
               ontoggle={(e) => { playlistPos = e.currentTarget.open ? anchorAbove(e.currentTarget, 280) : null; }}>
        <summary class="select-btn cursor-pointer list-none [&::-webkit-details-marker]:hidden" aria-label="Save as playlist">
          <svg viewBox="0 0 24 24" class="mobile-only-icon h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h13"/><path d="M3 12h9"/><path d="M3 18h9"/><path d="M17 15v6"/><path d="M14 18h6"/></svg>
          <span class="btn-word">Playlist</span>
        </summary>
        <div class="playlist-popover" style={playlistPos ? `left:${playlistPos.left}px;bottom:${playlistPos.bottom}px` : ''}>
          <input class="playlist-input" placeholder="Playlist name" bind:value={name} maxlength="80" />
          <button class="select-btn select-primary" disabled={!name.trim() || !videoIds.length} onclick={save} title="Save playlist">Save</button>
        </div>
      </details>
      <button class="select-btn export-btn" class:export-active={busy}
              disabled={busy || (!videoIds.length && !imageIds.length)} onclick={doExport} aria-busy={busy}
              title={imagesOnly ? 'Download the selected images as a .zip' : 'Merge & download the selected videos as one MP4'}
              aria-label={imagesOnly ? 'Export selected images as ZIP' : 'Export selected videos as MP4'}>
        {#if busy}
          <span class="export-orbit" aria-hidden="true"></span>
          <span class="btn-word">{imagesOnly ? 'Zipping…' : 'Preparing MP4'}</span>
        {:else}
          <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V3"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
        {/if}
      </button>
      <button class="select-btn" disabled={!videoIds.length} onclick={() => onplayqueue()}
              title="Add the selected videos to the cross-library Play Queue" aria-label="Add selected videos to Play Queue">
        <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 12H3"/><path d="M16 6H3"/><path d="M12 18H3"/><path d="m16 12 5 3-5 3v-6Z"/></svg>
      </button>
      <button class="select-btn" disabled={!montageIds.length} onclick={() => onbasket()}
              title="Add the selected videos and photos to the cross-library Montage queue" aria-label="Add to Montage queue">
        <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
        <span class="q-plus mobile-only-icon">+</span><span class="btn-word"> + Queue</span>
      </button>
      <button class="select-btn montage-btn" class:montage-running={movieRunning}
              disabled={montageIds.length < 2 && !movieRunning} onclick={() => onmovie()}
              title={movieRunning ? `Montage rendering — ${moviePct}%. Click to view progress.` : 'Generate a beat-synced montage from the selected videos and photos'}>
        {#if movieRunning}
          <ParticleField active={false} count={9} scale={0.42} layers={1} class="montage-particles" />
        {/if}
        <span class="montage-label">
          <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          <span class="btn-word">{movieRunning ? `Rendering ${moviePct}%` : 'Montage'}</span>
        </span>
      </button>
    </div>

    <div class="select-cluster">
      <button class="select-btn select-danger" disabled={!n} onclick={() => (confirmingDelete = true)} title="Delete" aria-label="Delete">
        <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
      </button>
      <button class="select-btn" onclick={() => clearSelection()} aria-label="Clear selection">
        <svg viewBox="0 0 24 24" class="mobile-only-icon h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        <span class="btn-word">Clear</span>
      </button>
      <button class="select-btn" onclick={() => setSelectMode(false)} aria-label="Done">
        <svg viewBox="0 0 24 24" class="mobile-only-icon h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>
        <span class="btn-word">Done</span>
      </button>
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

  /* Compact glyphs that only replace the text labels on the mobile icon grid; the
     full-word .btn-word spans stay visible on desktop so the bar is unchanged there. */
  .mobile-only-icon {
    display: none;
  }

  .play-glyph {
    font-size: 0.75rem;
  }

  .q-plus {
    font-weight: 800;
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

  /* Touch / narrow viewports (phones + iPad portrait): a single scrolling row hides
     options off-screen with no affordance on iOS, and free-flowing wrap produced a
     ragged, jumbled block. Instead lay the actions out on a deliberate grid of equal
     columns so they line up. `repeat(auto-fit, minmax(2.6rem, 1fr))` resolves to
     ~7-8 columns at iPhone widths — a balanced two rows for the ~14 actions — and
     collapses to a single row on wider tablets. Every action is icon-only here:
     .btn-word labels are hidden and a few buttons swap in a compact .mobile-only-icon
     glyph, so the cells stay uniform. `display: contents` on the clusters lets their
     buttons participate directly as grid items (the cluster dividers don't render). */
  @media (max-width: 900px) {
    .select-shell {
      padding-left: 0.5rem;
      padding-right: 0.5rem;
    }

    .select-dock {
      border-radius: var(--r-xl);
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(2.6rem, 1fr));
      gap: 0.35rem;
      justify-items: stretch;
      max-width: 100%;
      overflow: visible;
      width: 100%;
    }

    .select-cluster {
      display: contents;
    }

    .select-btn,
    .select-count {
      min-height: 2.75rem;
      min-width: 0;
      width: 100%;
    }

    .select-btn {
      flex: initial;
      padding: 0.5rem;
    }

    .select-menu,
    .playlist-menu {
      display: block;
      min-width: 0;
    }

    .select-menu > summary,
    .playlist-menu > summary {
      width: 100%;
    }

    .select-trigger {
      padding-left: 0.5rem;
      padding-right: 0.5rem;
    }

    /* Icon-only cell: the caret would crowd the select-icon in a ~46px cell, and the
       distinctive icon already reads as a control. */
    .select-caret {
      display: none;
    }

    .select-count {
      justify-content: center;
      padding: 0.25rem;
    }

    .play-glyph {
      font-size: 1rem;
    }

    .btn-word,
    .select-count-label {
      display: none;
    }

    .mobile-only-icon {
      display: inline-flex;
    }

    .playlist-input {
      width: 10rem;
    }
  }
</style>
