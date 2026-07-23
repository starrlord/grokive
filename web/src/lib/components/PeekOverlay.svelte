<script>
  // Full-screen long-press peek: shows the full media behind a scrim while the
  // pointer is held. Purely presentational — the owner arms/dismisses the gesture
  // and passes {thumb, href, media_type} (or null) as `item`.
  import { fade } from 'svelte/transition';
  let { item = null } = $props();
</script>

{#if item}
  <!-- pointer-events-none: the held pointer must keep flowing to the pressed element
       underneath so its release still reaches the owner's handlers. -->
  <div data-peek-overlay class="pointer-events-none fixed inset-0 z-[90] grid place-items-center bg-[var(--overlay-strong)] p-4 backdrop-blur-sm sm:p-8"
    transition:fade={{ duration: 120 }} aria-hidden="true">
    <div class="peek-spinner absolute h-10 w-10 rounded-full border-2" aria-hidden="true"></div>
    {#if item.media_type === 'video'}
      <video src={item.href} poster={item.thumb} autoplay muted loop playsinline
        class="relative max-h-full max-w-full rounded-xl bg-[var(--media-bg)] object-contain shadow-2xl"></video>
    {:else}
      <img src={item.href} alt="" draggable="false"
        class="relative max-h-full max-w-full rounded-xl bg-[var(--media-bg)] object-contain shadow-2xl" />
    {/if}
  </div>
{/if}

<style>
  /* Centered via left/top (not translate) so the spin animation can own transform. */
  .peek-spinner {
    animation: peek-spin 0.8s linear infinite;
    border-color: color-mix(in srgb, var(--accent) 35%, transparent);
    border-top-color: var(--accent);
    left: calc(50% - 1.25rem);
    top: calc(50% - 1.25rem);
  }

  @keyframes peek-spin {
    to { transform: rotate(360deg); }
  }
</style>
