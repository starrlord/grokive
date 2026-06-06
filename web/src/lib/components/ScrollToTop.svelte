<script>
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';

  // `lift` raises the button above the bottom SelectBar / montage chip so it never
  // sits underneath them.
  let { lift = false } = $props();

  let show = $state(false);
  let ticking = false;

  // The page itself is the scroller (sticky topbar, <main> grows), so we watch
  // window scroll. rAF-throttled and only writes `show` when it actually flips, so
  // scrolling stays cheap — no per-frame reactive churn.
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      const next = window.scrollY > window.innerHeight * 1.4;
      if (next !== show) show = next;
      ticking = false;
    });
  }

  onMount(() => {
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener('scroll', onScroll);
  });

  function toTop() {
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
  }
</script>

{#if show}
  <button type="button" class="scroll-top glass" class:raised={lift}
    aria-label="Back to top" title="Back to top" onclick={toTop}
    transition:fly={{ y: 12, duration: 180 }}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M12 19V5M5 12l7-7 7 7" />
    </svg>
  </button>
{/if}

<style>
  .scroll-top {
    position: fixed;
    right: max(1rem, env(safe-area-inset-right));
    bottom: max(1rem, env(safe-area-inset-bottom));
    z-index: 50;
    display: grid;
    place-items: center;
    height: 2.75rem;
    width: 2.75rem;
    border-radius: 999px;
    box-shadow: var(--shadow-dock);
    color: var(--ink);
    transition: background 140ms ease, border-color 140ms ease, color 140ms ease, transform 140ms ease;
  }

  .scroll-top:hover,
  .scroll-top:focus-visible {
    background: color-mix(in srgb, var(--accent) 16%, var(--surface));
    border-color: var(--accent);
    color: var(--ink);
    transform: translateY(-2px);
  }

  /* Clear the bottom SelectBar (~4rem) / montage chip when either is showing. */
  .scroll-top.raised {
    bottom: calc(max(1rem, env(safe-area-inset-bottom)) + 5rem);
  }

  .scroll-top svg {
    height: 1.15rem;
    width: 1.15rem;
  }

  @media (prefers-reduced-motion: reduce) {
    .scroll-top { transition: none; }
  }
</style>
