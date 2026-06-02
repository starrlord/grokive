<script>
  import { fade } from 'svelte/transition';
  import { justify } from '$lib/justified.js';
  import { favorites, stashed, toggleFavorite, setStashed, removeMedia } from '$lib/state.js';
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

  // Floating prompt tooltip — used in select mode, where the on-card prompt panel
  // is hidden so it doesn't intercept selection taps. Flips toward screen centre.
  let tip = $state(null);
  function showTip(e, text) {
    if (!text) return;
    tip = {
      text,
      x: e.clientX,
      y: e.clientY,
      flipX: e.clientX > window.innerWidth * 0.6,
      flipY: e.clientY > window.innerHeight * 0.72
    };
  }

</script>

<div class="w-full" bind:clientWidth={width} style="--g:{gap}px">
  {#each rows as row}
    <div class="flex" style="gap:var(--g); margin-bottom:var(--g)">
      {#each row.cells as cell (cell.item.id)}
        {@const it = cell.item}
        {@const fav = $favorites.has(it.id)}
        {@const sel = selection.has(it.id)}
        <div class="group relative shrink-0 overflow-hidden rounded-card bg-surface-2"
             class:ring-2={sel} style="width:{cell.w}px; height:{cell.h}px; --tw-ring-color:var(--accent)"
             onmousemove={(e) => { if (selectMode) showTip(e, it.prompt); }}
             onmouseleave={() => (tip = null)}>
          {#if it.thumb}
            <img src={it.thumb} alt="" loading="lazy" decoding="async"
                 class="absolute inset-0 h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.04]" />
          {:else}
            <div class="grid h-full w-full place-items-center text-xs text-muted">no thumbnail</div>
          {/if}

          <!-- click/select hit area -->
          <button type="button" class="absolute inset-0 z-[1]" aria-label="Open"
            onclick={() => (selectMode ? ontoggleselect(it) : onopen(it, items))}></button>

          <!-- badges (fade out on hover so the action cluster has room) -->
          <span class="pointer-events-none absolute left-2 top-2 z-[2] flex gap-1 transition group-hover:opacity-0">
            {#if it.media_type === 'video'}<span class="rounded-full bg-black/65 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">video</span>{/if}
            {#if it.has_subtitles}<span class="rounded-full bg-black/65 px-1.5 py-0.5 text-[10px] font-bold text-white">CC</span>{/if}
          </span>

          <!-- top-right hover actions: stash + favorite -->
          {#if !selectMode}
            {@const isStashed = $stashed.has(it.id)}
            <div class="absolute right-2 top-2 z-[3] flex gap-1.5">
              <button type="button" aria-label={isStashed ? 'Unstash' : 'Stash'} title={isStashed ? 'Unstash' : 'Stash'}
                class="grid h-8 w-8 place-items-center rounded-full bg-black/45 text-white opacity-0 transition group-hover:opacity-100 {isStashed ? 'opacity-100 bg-[var(--accent)]' : ''}"
                onclick={(e) => { e.stopPropagation(); setStashed([it.id], !isStashed); }}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><path d="M10 12h4"/></svg>
              </button>
              <button type="button" aria-label="Favorite" title="Favorite"
                class="grid h-8 w-8 place-items-center rounded-full bg-black/45 text-white opacity-0 transition group-hover:opacity-100 {fav ? 'opacity-100 text-[#ff5a7a]' : ''}"
                onclick={(e) => { e.stopPropagation(); toggleFavorite(it.id); }}>{fav ? '♥' : '♡'}</button>
              <button type="button" aria-label="Delete" title="Delete"
                class="grid h-8 w-8 place-items-center rounded-full bg-black/45 text-white opacity-0 transition hover:bg-red-500 group-hover:opacity-100"
                onclick={(e) => { e.stopPropagation(); confirming = it; }}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
              </button>
            </div>
          {:else}
            <span class="pointer-events-none absolute left-2 top-2 z-[3] grid h-7 w-7 place-items-center rounded-full border-2 border-white text-sm {sel ? 'bg-[var(--accent)] text-white' : 'bg-black/50 text-transparent'}">✓</span>
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
