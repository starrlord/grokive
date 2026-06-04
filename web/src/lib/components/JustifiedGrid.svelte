<script>
  import { fade } from 'svelte/transition';
  import { justify } from '$lib/justified.js';
  import { favorites, stashed, toggleFavorite, setStashed, removeMedia, setSelection, setSelectMode } from '$lib/state.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let {
    items = [],
    targetHeight = 240,
    gap = 10,
    selectMode = false,
    selection = new Set(),
    onopen = () => {},
    ontoggleselect = () => {}
  } = $props();

  let width = $state(0);
  const rows = $derived(width ? justify(items, width, targetHeight, gap) : []);
  let confirming = $state(null); // item pending delete confirmation

  // Drag-to-paint selection (mouse only). Press the left button on a card and drag
  // across others to select (or deselect) the whole run. The first card sets the
  // direction: pressing an unselected card paints "select", a selected one "deselect".
  // Touch/pen fall through to the click handler (tap = toggle) so scrolling still works.
  let painting = false;
  let paintOn = false;            // true = selecting, false = deselecting
  let clickSuppressedFor = null;  // id whose synthetic click must be ignored (already painted)
  function paintDown(e, it) {
    if (!selectMode || e.pointerType !== 'mouse' || e.button !== 0) return;
    paintOn = !selection.has(it.id);
    setSelection(it.id, paintOn);
    painting = true;
    clickSuppressedFor = it.id;
    e.preventDefault();           // suppress native image-drag and text selection
  }
  function paintEnter(it) {
    if (selectMode && painting) setSelection(it.id, paintOn);
  }
  function cellClick(it) {
    if (!selectMode) { onopen(it, items); return; }
    const suppressed = clickSuppressedFor === it.id;
    clickSuppressedFor = null;
    if (suppressed) return;       // mouse already handled this card on pointerdown
    ontoggleselect(it);           // touch/pen tap or keyboard activation
  }
  // The hover selection circle. Outside select mode it flips into select mode and
  // selects this card; inside it just toggles.
  function selectCircle(it) {
    if (selectMode) {
      setSelection(it.id, !selection.has(it.id));
    } else {
      setSelectMode(true);
      setSelection(it.id, true);
    }
  }

  // Floating prompt tooltip — used in select mode, where the on-card prompt panel
  // is hidden so it doesn't intercept selection taps. Flips toward screen centre.
  let tip = $state(null);
  function showTip(e, text) {
    if (!text || painting) return;
    tip = {
      text,
      x: e.clientX,
      y: e.clientY,
      flipX: e.clientX > window.innerWidth * 0.6,
      flipY: e.clientY > window.innerHeight * 0.72
    };
  }

</script>

<svelte:window onpointerup={() => (painting = false)} onpointercancel={() => (painting = false)} />

<div class="w-full" bind:clientWidth={width} style="--g:{gap}px">
  {#each rows as row (row.cells[0]?.item.id)}
    <div class="flex" style="gap:var(--g); margin-bottom:var(--g)">
      {#each row.cells as cell (cell.item.id)}
        {@const it = cell.item}
        {@const fav = $favorites.has(it.id)}
        {@const sel = selection.has(it.id)}
        {@const isMontage = it.model === 'Beat Montage'}
        <!-- Mouse handlers only position a hover tooltip; the real click target is the Open button below. -->
        <div class="card-frame group relative shrink-0 overflow-hidden rounded-card bg-surface-2" role="presentation"
             class:ring-2={sel} class:select-none={selectMode}
             class:selecting-card={selectMode}
             style="width:{cell.w}px; height:{cell.h}px; --tw-ring-color:var(--accent)"
             onmousemove={(e) => { if (selectMode) showTip(e, it.prompt); }}
             onmouseleave={() => (tip = null)}>
          {#if it.thumb}
            <img src={it.thumb} alt="" loading="lazy" decoding="async" draggable="false"
                 class="absolute inset-0 h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.04]" />
          {:else}
            <div class="grid h-full w-full place-items-center text-xs text-muted">no thumbnail</div>
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
            {#if it.has_subtitles}<span class="meta-badge">CC</span>{/if}
          </span>

          <!-- Selection circle, top-left: always shown in select mode, on hover
               otherwise. Clicking it outside select mode flips into select mode and
               selects this card. pointer-events gated to hover so touch taps on the
               corner don't accidentally enter select mode. -->
          <button type="button"
            class="absolute left-2 top-2 z-[4] grid h-7 w-7 place-items-center rounded-full border-2 border-[var(--media-control-ink)] text-sm transition
                   {sel ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'bg-[var(--selection-control-bg)] text-[var(--media-control-ink-muted)]'}
                   {selectMode ? '' : 'pointer-events-none opacity-0 group-hover:pointer-events-auto group-hover:opacity-100'}"
            aria-label={sel ? 'Deselect' : 'Select'} aria-pressed={sel}
            onpointerdown={(e) => e.stopPropagation()}
            onclick={(e) => { e.stopPropagation(); selectCircle(it); }}>{sel ? '✓' : ''}</button>

          <!-- top-right hover actions: archive + favorite -->
          {#if !selectMode}
            {@const isStashed = $stashed.has(it.id)}
            <div class="card-actions absolute right-2 top-2 z-[5] flex gap-1.5">
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

{#if selectMode && tip}
  <div class="pointer-events-none fixed z-[60] max-w-xs rounded-lg border border-line bg-[var(--surface-solid)] px-3 py-2 text-xs leading-relaxed shadow-2xl"
       style="left:{tip.x}px; top:{tip.y}px; transform: translate({tip.flipX ? 'calc(-100% - 14px)' : '14px'}, {tip.flipY ? 'calc(-100% - 14px)' : '14px'});"
       transition:fade={{ duration: 90 }}>
    {tip.text}
  </div>
{/if}

<style>
  .card-frame {
    container-type: inline-size;
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
      font-size: 0.5625rem;
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
