<script>
  // Floating, globally-mounted chip for the cross-library Montage queue ("basket").
  // Appears whenever the basket is non-empty (state.js `basket`), independent of
  // select mode / current view / collection — so clips gathered across libraries
  // stay one tap from a montage. Tapping opens a panel to review, remove, clear, or
  // fire the montage. Default anchor is bottom-LEFT so it never collides with the
  // bottom-RIGHT MontageStatusChip on desktop; both relocate to the top on phones
  // (see media query). The chip is also DRAGGABLE — it floats over content, so the
  // default corner can cover controls (especially on phones); the dropped spot is
  // persisted per-device and the panel flips to open away from the nearest edges.
  import { fly } from 'svelte/transition';
  import { basket, basketChipPos, clearBasket, toggleBasket, montageMode, isMontageSource } from '$lib/state.js';
  import { mediaByIds } from '$lib/api.js';

  // onview(list, index) — open the resolved queue in the Lightbox at that item, so a
  // queued clip can be watched before deciding to keep it. The panel deliberately
  // STAYS OPEN: it sits above the Lightbox (z-60 vs z-50), acting as a triage
  // sidebar — view a row, remove it (row ✕ or the viewer's own queue toggle), click
  // the next. `previewing` (a Lightbox is open) hides the scrim so the viewer stays
  // clickable and defers Escape to it; `previewId` highlights the row being viewed.
  let { onmontage = () => {}, onview = () => {}, previewing = false, previewId = null } = $props();

  const ids = $derived($basket);
  const count = $derived(ids.length);
  const picVideo = $derived($montageMode === 'picture-video');
  // Once the panel is open, items carry media_type — so we can show how many queued
  // sources are actually eligible under the current mode (video-only drops any images
  // queued earlier). Before items load, fall back to the raw count.
  const eligible = $derived(items.length ? items.filter((it) => isMontageSource(it, $montageMode)) : ids);
  const eligibleCount = $derived(eligible.length);
  let open = $state(false);
  let items = $state([]);
  let loading = $state(false);

  // Resolve queued ids -> media (thumb + prompt) for the panel list. The ids span
  // collections you may have navigated away from, so this MUST hit the server
  // (mediaByIds), never an in-memory grid map — that's the whole point of the queue.
  // Re-runs whenever the panel is open and the basket changes.
  $effect(() => {
    if (!open) return;
    const want = ids;
    loading = true;
    mediaByIds(want)
      .then((list) => {
        const order = new Map(want.map((id, i) => [String(id), i]));
        items = list.slice().sort((a, b) => (order.get(String(a.id)) ?? 0) - (order.get(String(b.id)) ?? 0));
      })
      .catch(() => { items = []; })
      .finally(() => { loading = false; });
  });

  // Auto-close when the queue empties (last item removed from the panel or elsewhere).
  $effect(() => { if (count === 0 && open) open = false; });

  // While a preview Lightbox is up, Escape belongs to it (this listener registered
  // first, so it would otherwise close the panel on the same keypress).
  function onKey(e) { if (e.key === 'Escape' && open && !previewing) open = false; }

  // Keep the row being previewed visible in the scrollable list as the viewer's
  // prev/next moves through the queue.
  let listEl = $state(null);
  $effect(() => {
    if (!previewing || previewId == null || !listEl) return;
    listEl.querySelector(`[data-id="${CSS.escape(String(previewId))}"]`)?.scrollIntoView({ block: 'nearest' });
  });

  // --- Drag-to-move ----------------------------------------------------------
  // The dropped spot lives in state.js `basketChipPos` as viewport FRACTIONS
  // (0..1 of the space the chip can occupy) — persisted per-device, so it
  // survives reloads and stays proportionally placed across window resizes and
  // phone rotation. Null -> the CSS default anchors above apply.
  const pos = $derived($basketChipPos);
  let vw = $state(typeof window === 'undefined' ? 0 : window.innerWidth);
  let vh = $state(typeof window === 'undefined' ? 0 : window.innerHeight);
  let chipEl = $state(null);
  let chipW = $state(0), chipH = $state(0);
  let dragging = $state(false);
  let suppressClick = false;
  let start = null; // pointer + chip origin at pointerdown

  const placed = $derived.by(() => {
    if (!pos || !vw || !vh || !chipW || !chipH) return null;
    const x = pos.fx * Math.max(0, vw - chipW);
    const y = pos.fy * Math.max(0, vh - chipH);
    return { x, y, right: pos.fx > 0.5, up: pos.fy > 0.5 };
  });
  // Anchor the wrap by the chip corner nearest the closest viewport edge so the
  // panel grows toward open space, and cap the wrap's width so the panel can
  // never extend past the viewport (it shrinks instead — max-width: 100%).
  const wrapStyle = $derived.by(() => {
    if (!placed) return '';
    const hpos = placed.right
      ? `left:auto;right:${Math.round(vw - placed.x - chipW)}px;max-width:${Math.round(placed.x + chipW)}px;`
      : `left:${Math.round(placed.x)}px;right:auto;max-width:${Math.round(vw - placed.x - 12)}px;`;
    const vpos = placed.up
      ? `top:auto;bottom:${Math.round(vh - placed.y - chipH)}px;`
      : `top:${Math.round(placed.y)}px;bottom:auto;`;
    return hpos + vpos;
  });

  function dragStart(e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const r = chipEl.getBoundingClientRect();
    start = { x: e.clientX, y: e.clientY, left: r.left, top: r.top };
    chipEl.setPointerCapture?.(e.pointerId);
  }
  function dragMove(e) {
    if (!start) return;
    const dx = e.clientX - start.x, dy = e.clientY - start.y;
    if (!dragging && Math.hypot(dx, dy) < 6) return; // still a tap until it travels
    dragging = true;
    const maxX = Math.max(0, vw - chipW), maxY = Math.max(0, vh - chipH);
    const x = Math.min(Math.max(0, start.left + dx), maxX);
    const y = Math.min(Math.max(0, start.top + dy), maxY);
    basketChipPos.set({ fx: maxX ? x / maxX : 0, fy: maxY ? y / maxY : 0 });
  }
  function dragEnd() {
    if (dragging) {
      // A click fires right after pointerup on a drag — it must not toggle the panel.
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 0);
    }
    dragging = false;
    start = null;
  }
  function onChipClick() {
    if (suppressClick) return;
    open = !open;
  }
</script>

<svelte:window onkeydown={onKey} bind:innerWidth={vw} bind:innerHeight={vh} />

{#if count}
  {#if open && !previewing}
    <!-- Hidden while a preview Lightbox is open — the scrim sits ABOVE the Lightbox
         (z-59 vs z-50) and would swallow every click meant for the player. -->
    <button type="button" class="basket-scrim" aria-label="Close montage queue" onclick={() => (open = false)}></button>
  {/if}
  <div class="basket-wrap" class:open class:custom={!!placed} class:panel-down={placed && !placed.up} class:anchor-right={!!placed?.right}
    style={wrapStyle} transition:fly={{ y: 16, duration: 200 }}>
    {#if open}
      <div class="basket-panel" role="dialog" aria-label="Montage queue">
        <div class="basket-head">
          <span class="basket-title">Montage queue · {count}</span>
          <button type="button" class="basket-clear" onclick={() => clearBasket()}>Clear</button>
        </div>
        <!-- Montage mode: video-only (styles) vs Picture & Video (still images ride along
             as beats). Adding a photo auto-selects picture-video; this is the manual toggle. -->
        <div class="basket-mode" role="group" aria-label="Montage mode">
          <button type="button" class="basket-mode-btn" class:active={!picVideo} aria-pressed={!picVideo} onclick={() => montageMode.set('video')}>Video only</button>
          <button type="button" class="basket-mode-btn" class:active={picVideo} aria-pressed={picVideo} onclick={() => montageMode.set('picture-video')}>Picture &amp; Video</button>
        </div>
        <div class="basket-list" bind:this={listEl}>
          {#if loading && !items.length}
            <p class="basket-status">Loading…</p>
          {:else if !items.length}
            <p class="basket-status">Nothing queued.</p>
          {:else}
            {#each items as it, idx (it.id)}
              <div class="basket-row" data-id={it.id}
                class:ineligible={!isMontageSource(it, $montageMode)}
                class:previewing={previewing && String(it.id) === String(previewId)}
                title={!isMontageSource(it, $montageMode) ? 'Not used in Video-only mode — switch to Picture & Video to include this photo' : undefined}>
                <button type="button" class="basket-rowmain" title="View / play" onclick={() => onview(items, idx)}>
                  {#if it.thumb}
                    <img class="basket-thumb" src={it.thumb} alt="" loading="lazy" decoding="async" />
                  {:else}
                    <span class="basket-thumb basket-thumb--empty"></span>
                  {/if}
                  <span class="basket-rowtext">{it.prompt || it.model || 'Untitled'}</span>
                </button>
                <button type="button" class="basket-remove" aria-label="Remove from queue" title="Remove" onclick={() => toggleBasket(it.id)}>✕</button>
              </div>
            {/each}
          {/if}
        </div>
        <button type="button" class="basket-go" disabled={eligibleCount < 2} onclick={() => { open = false; onmontage(); }}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          Make Montage{eligibleCount >= 2 ? ` (${eligibleCount})` : ''}
        </button>
        {#if eligibleCount < 2}<p class="basket-hint">Add at least 2 {picVideo ? 'photos or videos' : 'videos'} to montage.</p>{/if}
      </div>
    {/if}

    <button type="button" bind:this={chipEl} bind:clientWidth={chipW} bind:clientHeight={chipH}
      class="basket-chip" class:grabbing={dragging} onclick={onChipClick} aria-expanded={open}
      onpointerdown={dragStart} onpointermove={dragMove} onpointerup={dragEnd} onpointercancel={dragEnd}
      title="Montage queue — {count} {picVideo ? 'item' : 'video'}{count === 1 ? '' : 's'} · drag to move">
      <span class="basket-chip-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
      </span>
      <span class="basket-chip-count">{count}</span>
      <span class="basket-chip-label">queued</span>
    </button>
  </div>
{/if}

<style>
  .basket-scrim {
    background: transparent;
    inset: 0;
    position: fixed;
    z-index: 59;
  }

  .basket-wrap {
    align-items: flex-start;
    bottom: max(1rem, env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column; /* panel above the chip on desktop */
    gap: 0.5rem;
    left: max(1rem, env(safe-area-inset-left));
    max-width: min(22rem, calc(100vw - 2rem));
    position: fixed;
    z-index: 60;
  }

  .basket-chip {
    align-items: center;
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: 999px;
    box-shadow: var(--shadow-dock);
    display: inline-flex;
    gap: 0.4rem;
    min-height: 2.5rem;
    padding: 0.4rem 0.85rem 0.4rem 0.55rem;
    touch-action: none; /* the chip pans itself — without this, touch drags scroll the page */
    user-select: none;
    -webkit-user-select: none;
  }
  .basket-chip.grabbing { cursor: grabbing; }
  .basket-chip:hover,
  .basket-wrap.open .basket-chip { border-color: var(--accent); }

  .basket-chip-icon { color: var(--accent); display: grid; place-items: center; }
  .basket-chip-icon svg { height: 1.15rem; width: 1.15rem; }

  .basket-chip-count {
    background: var(--accent);
    border-radius: 999px;
    color: var(--on-accent);
    display: grid;
    font-size: 0.75rem;
    font-weight: 900;
    height: 1.4rem;
    min-width: 1.4rem;
    padding: 0 0.4rem;
    place-items: center;
  }
  .basket-chip-label { color: var(--ink); font-size: 0.8125rem; font-weight: 800; }

  .basket-panel {
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: var(--r-xl);
    box-shadow: var(--shadow-dock);
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    max-width: 100%; /* shrink to the wrap's inline max-width when the chip sits near an edge */
    padding: 0.6rem;
    width: min(20rem, calc(100vw - 2rem));
  }

  .basket-mode {
    background: var(--surface-2);
    border-radius: var(--r-lg);
    display: grid;
    gap: 0.2rem;
    grid-template-columns: 1fr 1fr;
    padding: 0.2rem;
  }
  .basket-mode-btn {
    border-radius: calc(var(--r-lg) - 0.2rem);
    color: color-mix(in srgb, var(--ink) 62%, transparent);
    font-size: 0.75rem;
    font-weight: 800;
    padding: 0.35rem 0.4rem;
  }
  .basket-mode-btn.active { background: var(--accent); color: var(--on-accent); }

  .basket-row.ineligible { opacity: 0.45; }
  .basket-row.ineligible .basket-thumb { filter: grayscale(0.6); }

  .basket-head { align-items: center; display: flex; gap: 0.5rem; justify-content: space-between; }
  .basket-title { color: var(--ink); font-size: 0.8125rem; font-weight: 850; }
  .basket-clear {
    border-radius: var(--r-lg);
    color: color-mix(in srgb, var(--ink) 58%, transparent);
    font-size: 0.75rem;
    font-weight: 700;
    padding: 0.25rem 0.5rem;
  }
  .basket-clear:hover { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger-ink); }

  .basket-list {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    max-height: min(46vh, 22rem);
    overflow-y: auto;
  }
  .basket-status {
    color: color-mix(in srgb, var(--ink) 58%, transparent);
    font-size: 0.8125rem;
    padding: 0.6rem;
    text-align: center;
  }

  .basket-row {
    align-items: center;
    border-radius: var(--r-lg);
    display: flex;
    gap: 0.25rem;
    padding: 0.25rem;
  }
  .basket-row:hover { background: var(--surface-2); }
  .basket-row.previewing { background: color-mix(in srgb, var(--accent) 16%, transparent); }

  /* The thumbnail + title is itself a button: click to view/play this item in the
     Lightbox (the panel stays open above it as a triage sidebar). */
  .basket-rowmain {
    align-items: center;
    display: flex;
    flex: 1 1 auto;
    gap: 0.5rem;
    min-width: 0;
    text-align: left;
  }

  .basket-thumb {
    background: var(--media-bg);
    border-radius: 0.4rem;
    flex: 0 0 auto;
    height: 2.4rem;
    object-fit: cover;
    width: 2.4rem;
  }
  .basket-thumb--empty { background: var(--surface-2); }

  .basket-rowtext {
    color: var(--ink);
    flex: 1 1 auto;
    font-size: 0.75rem;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .basket-remove {
    border-radius: 999px;
    color: color-mix(in srgb, var(--ink) 55%, transparent);
    display: grid;
    flex: 0 0 auto;
    height: 1.75rem;
    place-items: center;
    width: 1.75rem;
  }
  .basket-remove:hover { background: var(--surface-2); color: var(--ink); }

  .basket-go {
    align-items: center;
    background: var(--accent);
    border-radius: var(--r-lg);
    color: var(--on-accent);
    display: inline-flex;
    font-size: 0.8125rem;
    font-weight: 800;
    gap: 0.45rem;
    justify-content: center;
    min-height: 2.4rem;
    padding: 0.5rem;
  }
  .basket-go:disabled { cursor: default; opacity: 0.5; }

  .basket-hint {
    color: color-mix(in srgb, var(--ink) 55%, transparent);
    font-size: 0.6875rem;
    text-align: center;
  }

  /* Phones / narrow viewports: the SelectBar dock wraps and fills the bottom and the
     MontageStatusChip relocates to the top — so move this chip to the top too, stacked
     just BELOW the status chip's slot, and flip the panel to open downward. */
  @media (max-width: 900px) {
    .basket-wrap {
      bottom: auto;
      flex-direction: column-reverse; /* chip on top, panel below */
      top: calc(56px + max(0.5rem, env(safe-area-inset-top)) + 3.25rem);
    }
  }

  /* User-dragged position: inline left/top/right/bottom out-specificity everything;
     these two-class rules out-specificity the phone media query's flex flip so the
     panel opens away from whichever vertical half the chip sits in. */
  .basket-wrap.custom { flex-direction: column; } /* chip in bottom half -> panel above */
  .basket-wrap.custom.panel-down { flex-direction: column-reverse; } /* top half -> panel below */
  .basket-wrap.custom.anchor-right { align-items: flex-end; } /* right half -> panel grows leftward */
</style>
