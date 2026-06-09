<script>
  import { justify } from '$lib/justified.js';
  import { favorites, stashed, toggleFavorite, setStashed, removeMedia, setSelection, addSelection, setSelectMode, selectionMembers, sendToImagine } from '$lib/state.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let {
    items = [],
    targetHeight = 240,
    gap = 10,
    selectMode = false,
    onopen = () => {},
    ontoggleselect = () => {}
  } = $props();

  let width = $state(0);
  const rows = $derived(width ? justify(items, width, targetHeight, gap) : []);
  let confirming = $state(null); // item pending delete confirmation

  // Selection has three gestures, all funnelling through the same id-keyed store:
  //   • Mouse: press + drag to paint a run (the first card sets select vs. deselect);
  //     Shift-click extends a range from the last-touched card.
  //   • Touch/pen: tap toggles one card; long-press extends a range from the anchor
  //     (so phones get bulk selection without a drag, which would fight scrolling).
  // `anchorId` is the last card the user singled out — the pivot for both ranges.
  let painting = false;
  let paintOn = false;            // true = selecting, false = deselecting
  let clickSuppressedFor = null;  // id whose synthetic click must be ignored (already handled)
  let anchorId = null;            // range pivot (last tapped/painted card)

  // Mouse drag past the viewport edge auto-scrolls and keeps painting (below).
  let lastPointer = { x: 0, y: 0 };
  let autoScrollRAF = null;
  // Touch long-press arming.
  let pressId = null, pressX = 0, pressY = 0, longPressTimer = null;

  function selectRange(fromId, toId) {
    const a = items.findIndex((x) => x.id === fromId);
    const b = items.findIndex((x) => x.id === toId);
    if (a < 0 || b < 0) return;
    const [lo, hi] = a <= b ? [a, b] : [b, a];
    // One batched store update (not per-item) so a big range is O(n), not O(n²).
    addSelection(items.slice(lo, hi + 1).map((x) => x.id));
  }

  function paintDown(e, it) {
    if (!selectMode) return;
    clickSuppressedFor = null;    // clear any stale flag from a press that ended elsewhere
    if (e.pointerType === 'mouse') {
      if (e.button !== 0) return;
      if (e.shiftKey && anchorId != null) {       // range-extend, no paint
        selectRange(anchorId, it.id);
        clickSuppressedFor = it.id;
        e.preventDefault();
        return;
      }
      paintOn = !selectionMembers.has(it.id);
      setSelection(it.id, paintOn);
      anchorId = it.id;
      painting = true;
      lastPointer = { x: e.clientX, y: e.clientY };
      clickSuppressedFor = it.id;
      e.preventDefault();         // suppress native image-drag and text selection
    } else {
      // Touch/pen: DON'T preventDefault — a vertical drag must still scroll. Arm a
      // long-press; a quick tap falls through to cellClick (toggle).
      pressId = it.id;
      pressX = e.clientX;
      pressY = e.clientY;
      clearTimeout(longPressTimer);
      longPressTimer = setTimeout(() => {
        longPressTimer = null;
        if (anchorId != null && anchorId !== it.id) selectRange(anchorId, it.id);
        else { setSelection(it.id, true); anchorId = it.id; }
        clickSuppressedFor = it.id;   // the click that follows must not toggle it back
        navigator.vibrate?.(15);
      }, 450);
    }
  }
  function paintEnter(it) {
    if (selectMode && painting) setSelection(it.id, paintOn);
  }
  function cellClick(it) {
    if (!selectMode) { onopen(it, items); return; }
    const suppressed = clickSuppressedFor === it.id;
    clickSuppressedFor = null;
    if (suppressed) return;       // already handled on pointerdown / long-press
    ontoggleselect(it);           // touch/pen tap or keyboard activation
    anchorId = it.id;
  }
  // The hover selection circle. Outside select mode it flips into select mode and
  // selects this card; inside it just toggles.
  function selectCircle(it) {
    if (selectMode) {
      setSelection(it.id, !selectionMembers.has(it.id));
    } else {
      setSelectMode(true);
      setSelection(it.id, true);
    }
    anchorId = it.id;
  }

  // --- Edge auto-scroll while drag-painting (mouse) -------------------------
  // Drag toward the top/bottom edge and the window scrolls so you can paint a run
  // longer than the viewport. The pointer is stationary while content moves under
  // it, so pointerenter won't fire — we hit-test elementFromPoint each frame.
  const EDGE = 90;     // px hot-zone
  const MAX_V = 26;    // px/frame at the very edge
  function edgeVelocity(y) {
    if (y < EDGE) return -Math.ceil(((EDGE - y) / EDGE) * MAX_V);
    const dist = window.innerHeight - y;
    if (dist < EDGE) return Math.ceil(((EDGE - dist) / EDGE) * MAX_V);
    return 0;
  }
  function autoTick() {
    autoScrollRAF = null;
    if (!painting) return;
    const v = edgeVelocity(lastPointer.y);
    if (v === 0) return;          // left the hot-zone; a pointermove restarts us
    window.scrollBy(0, v);
    const el = document.elementFromPoint(lastPointer.x, lastPointer.y);
    const id = el?.closest?.('[data-id]')?.dataset.id;
    if (id != null) setSelection(id, paintOn);
    autoScrollRAF = requestAnimationFrame(autoTick);
  }
  function endPaint() {
    painting = false;
    if (autoScrollRAF != null) { cancelAnimationFrame(autoScrollRAF); autoScrollRAF = null; }
  }
  function onWinPointerMove(e) {
    if (painting) {
      lastPointer = { x: e.clientX, y: e.clientY };
      if (autoScrollRAF == null && edgeVelocity(e.clientY) !== 0) autoScrollRAF = requestAnimationFrame(autoTick);
      return;
    }
    // A moving finger means "scroll", not "long-press select" — disarm.
    if (longPressTimer != null && (Math.abs(e.clientX - pressX) > 10 || Math.abs(e.clientY - pressY) > 10)) {
      clearTimeout(longPressTimer);
      longPressTimer = null;
    }
  }
  function onWinPointerUp() {
    endPaint();
    if (longPressTimer != null) { clearTimeout(longPressTimer); longPressTimer = null; }
  }

</script>

<svelte:window onpointerup={onWinPointerUp} onpointercancel={onWinPointerUp} onpointermove={onWinPointerMove} />

<div class="w-full" bind:clientWidth={width} style="--g:{gap}px">
  {#each rows as row (row.cells[0]?.item.id)}
    <!-- content-visibility:auto (see .grid-row) lets the browser skip rendering rows
         outside the viewport — cheap, native virtualization that keeps the DOM,
         selection, and scroll behaviour intact. contain-intrinsic-size reserves the
         row's real width×height so skipped rows don't collapse or shift the scrollbar. -->
    <div class="grid-row flex" style="gap:var(--g); margin-bottom:var(--g); contain-intrinsic-size:auto {width}px auto {row.cells[0]?.h ?? targetHeight}px">
      {#each row.cells as cell (cell.item.id)}
        {@const it = cell.item}
        {@const fav = $favorites.has(it.id)}
        {@const sel = selectionMembers.has(it.id)}
        {@const isMontage = it.model === 'Beat Montage'}
        <!-- The real click target is the Open/select button below. data-id lets the
             auto-scroll hit-test (elementFromPoint) map a point back to a card. -->
        <div class="card-frame group relative shrink-0 overflow-hidden rounded-card bg-surface-2" role="presentation"
             data-id={it.id}
             class:select-none={selectMode}
             style="width:{cell.w}px; height:{cell.h}px">
          {#if it.thumb}
            <!-- Hover-zoom is disabled in select mode: dragging across cards would
                 otherwise fire a 300ms scale animation per card and jank the drag. -->
            <img src={it.thumb} alt="" loading="lazy" decoding="async" draggable="false"
                 class="absolute inset-0 h-full w-full object-cover {selectMode ? '' : 'transition-transform duration-300 group-hover:scale-[1.04]'}" />
          {:else}
            <div class="grid h-full w-full place-items-center text-xs text-muted">no thumbnail</div>
          {/if}

          <!-- Selection outline. Drawn as an INSET overlay (inside the card) rather
               than an outset ring: the row's content-visibility:auto applies paint
               containment, which clips an outset ring at the row edge — that produced
               the half-highlighted look. An inset border stays within the card. -->
          {#if sel}
            <span class="pointer-events-none absolute inset-0 z-[3] rounded-card border-2 border-[var(--accent)]"></span>
          {/if}

          <!-- click/select hit area. In select mode, mouse paints via pointer events
               (press + drag across cards); touch/pen taps toggle through onclick. -->
          <button type="button" class="absolute inset-0 z-[1]"
            aria-label={selectMode ? (sel ? 'Deselect' : 'Select') : 'Open'}
            onpointerdown={(e) => paintDown(e, it)}
            onpointerenter={() => paintEnter(it)}
            onclick={() => cellClick(it)}></button>

          <!-- Resolution / video / CC badges, bottom-right — clear of the top-right
               action cluster and the top-left selection circle, so they never collide
               (notably on touch, where there's no hover to swap them out). Resolution
               uses the shorter side so portrait and landscape both read sensibly. -->
          <span class="card-meta pointer-events-none absolute bottom-2 right-2 z-[2]">
            {#if it.media_w && it.media_h}<span class="meta-badge">{Math.min(it.media_w, it.media_h)}p</span>{/if}
            {#if isMontage}
              <span class="meta-badge meta-badge-music" title="Music montage" aria-label="Music montage">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
              </span>
            {:else if it.media_type === 'video'}
              <span class="meta-badge meta-badge-video">video</span>
            {/if}
            {#if it.api_generated}
              <span class="meta-badge meta-badge-api" title="Generated with Grok Imagine" aria-label="Generated with Grok Imagine">
                <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 3l1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6z"/></svg>
              </span>
            {/if}
            {#if it.has_subtitles}<span class="meta-badge">CC</span>{/if}
          </span>

          <!-- Selection circle, top-left: always shown in select mode, on hover
               otherwise. Clicking it outside select mode flips into select mode and
               selects this card. pointer-events gated to hover so touch taps on the
               corner don't accidentally enter select mode. -->
          <!-- pointer-coarse:* keeps the circle visible & tappable on touch (no hover),
               so phone users can enter select mode and reach every bulk action. -->
          <!-- The visible circle stays 28px, but a transparent ::after pad extends the
               hit target to ~44px (8px out on every side) so taps near the circle select
               instead of falling through to the full-card Open button beneath it. -->
          <button type="button"
            class="absolute left-2 top-2 z-[4] grid h-7 w-7 place-items-center rounded-full border-2 border-[var(--media-control-ink)] text-sm transition after:absolute after:-inset-2 after:content-[''] pointer-coarse:opacity-100 pointer-coarse:pointer-events-auto
                   {sel ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'bg-[var(--selection-control-bg)] text-[var(--media-control-ink-muted)]'}
                   {selectMode ? '' : 'pointer-events-none opacity-0 group-hover:pointer-events-auto group-hover:opacity-100'}"
            aria-label={sel ? 'Deselect' : 'Select'} aria-pressed={sel}
            onpointerdown={(e) => e.stopPropagation()}
            onclick={(e) => { e.stopPropagation(); selectCircle(it); }}>{sel ? '✓' : ''}</button>

          <!-- top-right hover actions: archive + favorite -->
          {#if !selectMode}
            {@const isStashed = $stashed.has(it.id)}
            <div class="card-actions absolute right-2 top-2 z-[5] flex gap-1.5">
              {#if it.media_type !== 'video'}
                <button type="button" aria-label="Use as Imagine source" title="Use as source for Grok Imagine"
                  class="card-action-btn"
                  onclick={(e) => { e.stopPropagation(); sendToImagine(it); }}>
                  <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8 19 13"/><path d="M15 9h.01"/><path d="M17.8 6.2 19 5"/><path d="m3 21 9-9"/><path d="M12.2 6.2 11 5"/></svg>
                </button>
              {/if}
              <button type="button" aria-label="Favorite" title="Favorite"
                class="card-action-btn {fav ? 'text-[var(--favorite)]' : ''}"
                onclick={(e) => { e.stopPropagation(); toggleFavorite(it.id); }}>{fav ? '♥' : '♡'}</button>
              <button type="button" aria-label={isStashed ? 'Restore' : 'Archive'} title={isStashed ? 'Restore' : 'Archive'}
                class="card-action-btn {isStashed ? 'card-action-active' : ''}"
                onclick={(e) => { e.stopPropagation(); setStashed([it.id], !isStashed); }}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><path d="M10 12h4"/></svg>
              </button>
              <button type="button" aria-label="Delete" title="Delete"
                class="card-action-btn hover:bg-[var(--danger)]"
                onclick={(e) => { e.stopPropagation(); confirming = it; }}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
              </button>
            </div>
          {/if}

        </div>
      {/each}
    </div>
  {/each}
</div>

{#if confirming}
  <ConfirmDialog title="Delete this item?"
    message="The file is permanently removed from disk and won't be re-downloaded on future syncs."
    confirmLabel="Delete"
    onconfirm={() => { removeMedia([confirming.id]); confirming = null; }}
    oncancel={() => (confirming = null)} />
{/if}

<style>
  /* Native virtualization: off-screen rows aren't rendered/laid out. The per-row
     contain-intrinsic-size (set inline) keeps total height stable so scrolling and
     the infinite-scroll sentinel behave exactly as before. */
  .grid-row {
    content-visibility: auto;
  }

  .card-frame {
    container-type: inline-size;
  }

  /* In select mode, long-press is the range gesture — stop iOS Safari from popping
     its image save/callout menu and hijacking it. */
  .card-frame.select-none {
    -webkit-touch-callout: none;
  }

  .card-meta {
    align-items: center;
    display: flex;
    gap: 0.25rem;
    justify-content: flex-end;
    max-width: calc(100% - 1rem);
    transform: scale(var(--meta-scale, 1));
    transform-origin: bottom right;
    white-space: nowrap;
  }

  .meta-badge {
    align-items: center;
    background: var(--media-badge-bg);
    border-radius: 999px;
    color: var(--media-control-ink);
    display: inline-flex;
    font-size: 0.625rem;
    font-weight: 800;
    justify-content: center;
    letter-spacing: 0;
    line-height: 1;
    min-height: 1.25rem;
    padding: 0.25rem 0.45rem;
    text-transform: uppercase;
  }

  .meta-badge-video {
    padding-inline: 0.5rem;
  }

  /* Music-montage marker: accent-filled pill with a music-note glyph, matching the
     Montage action's icon. Stands out from the neutral resolution/CC badges so a
     beat montage reads at a glance on the Recent tab. */
  .meta-badge-music {
    background: var(--accent);
    color: var(--on-accent);
    padding-inline: 0.4rem;
  }

  .meta-badge-music svg {
    height: 0.85rem;
    width: 0.85rem;
  }

  /* Grok Imagine marker: teal accent-2 pill with a sparkle, distinct from the
     violet music-montage badge so API-generated media reads at a glance. */
  .meta-badge-api {
    background: var(--accent-2, #4bb3a8);
    color: var(--on-accent, #fff);
    padding-inline: 0.4rem;
  }

  .meta-badge-api svg {
    height: 0.85rem;
    width: 0.85rem;
  }

  .card-actions {
    flex-direction: column;
    opacity: 0;
    pointer-events: none;
    transform: translateY(-0.125rem);
    transition: opacity 140ms ease, transform 140ms ease;
  }

  .card-frame:hover .card-actions,
  .card-frame:focus-within .card-actions {
    opacity: 1;
    pointer-events: auto;
    transform: translateY(0);
  }

  /* Touch has no hover, which would otherwise leave favourite/archive/delete
     unreachable per card — so reveal them persistently on coarse pointers. Delete
     still routes through the confirm dialog, so an accidental tap can't destroy a
     file. */
  @media (pointer: coarse) {
    .card-actions {
      opacity: 1;
      pointer-events: auto;
      transform: none;
    }
  }

  .card-action-btn {
    align-items: center;
    background: var(--media-control-bg);
    border: 1px solid var(--media-control-border);
    border-radius: 999px;
    color: var(--media-control-ink);
    display: grid;
    height: 2rem;
    place-items: center;
    transition: background 140ms ease, border-color 140ms ease, color 140ms ease, transform 140ms ease;
    width: 2rem;
  }

  .card-action-btn:hover,
  .card-action-btn:focus-visible {
    background: var(--media-control-bg-hover);
    border-color: var(--media-control-border-hover);
  }

  .card-action-active {
    background: var(--accent);
    border-color: color-mix(in srgb, white 28%, transparent);
  }

  @container (min-width: 190px) {
    .card-actions {
      flex-direction: row;
    }
  }

  @container (max-width: 174px) {
    .card-meta {
      --meta-scale: 0.92;
      gap: 0.2rem;
    }

    .meta-badge {
      font-size: 0.625rem;
      min-height: 1.125rem;
      padding: 0.2rem 0.35rem;
    }

    .meta-badge-video {
      padding-inline: 0.4rem;
    }
  }

  @container (max-width: 150px) {
    .card-meta {
      --meta-scale: 0.82;
    }
  }
</style>
