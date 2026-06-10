<script>
  // A lightweight anchored dropdown: a trigger button with a panel that opens beneath it.
  // Closes on outside-click, Escape, or when content calls the close() it's handed. Not
  // portaled — the panel is an absolutely-positioned child, so the trigger's ancestors must
  // not clip overflow around it (the sticky top bar doesn't). For menus, call close() in the
  // item handlers; for settings panels (multi-change), just ignore it so it stays open.
  let {
    triggerClass = '',
    align = 'right',     // which edge the panel aligns to: 'right' | 'left'
    ariaLabel = undefined,
    title = undefined,
    trigger,             // snippet: trigger button contents
    children,            // snippet(close): panel contents
  } = $props();

  let open = $state(false);
  let root = $state(null);
  let panel = $state(null);
  let shift = $state(0); // horizontal nudge to keep the panel on-screen
  const close = () => (open = false);

  function onDocClick(e) {
    if (open && root && !root.contains(e.target)) open = false;
  }
  function onKey(e) {
    if (e.key === 'Escape' && open) open = false;
  }

  // Wherever the trigger sits, nudge the panel horizontally so it stays within the viewport.
  // Computed from the (untransformed) trigger box + the panel's layout width, so it's a single
  // pass with no feedback from the transform we apply.
  $effect(() => {
    if (!open || !panel || !root) return;
    const r = root.getBoundingClientRect();
    const w = panel.offsetWidth;
    const m = 8;
    let left = align === 'right' ? r.right - w : r.left;
    let s = 0;
    if (left + w > window.innerWidth - m) s = window.innerWidth - m - (left + w);
    if (left + s < m) s = m - left;
    shift = s;
  });
</script>

<svelte:window onclick={onDocClick} onkeydown={onKey} />

<div bind:this={root} class="relative">
  <button type="button" class={triggerClass} aria-haspopup="true" aria-expanded={open}
    aria-label={ariaLabel} {title} onclick={() => (open = !open)}>
    {@render trigger()}
  </button>
  {#if open}
    <div bind:this={panel} class="absolute top-full z-50 mt-1.5 {align === 'right' ? 'right-0' : 'left-0'}" style="transform: translateX({shift}px)">
      {@render children(close)}
    </div>
  {/if}
</div>
