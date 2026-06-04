<script>
  // Floating, globally-mounted status pill for the beat-montage render. Driven by
  // the shared `movieChip` derived, it appears the moment a render starts and stays
  // until the result is dealt with (committed / "Make another" / dismissed here),
  // independent of the panel or select mode — so a render is always reachable.
  import { fly } from 'svelte/transition';
  import { movieChip, acknowledgeMovie } from '$lib/state.js';
  import ParticleField from './ParticleField.svelte';

  let { onopen = () => {} } = $props();

  const chip = $derived($movieChip);
  const pct = $derived(Math.round((chip?.progress || 0) * 100));
  const RING = 2 * Math.PI * 11; // circumference for r=11
</script>

{#if chip}
  <div class="montage-chip" class:is-done={chip.status === 'done'} class:is-error={chip.status === 'error'}
       transition:fly={{ y: 16, duration: 200 }}>
    {#if chip.running}
      <ParticleField active={false} count={10} scale={0.4} layers={1} class="chip-particles" />
    {/if}

    <button type="button" class="chip-main" onclick={() => onopen()}
            title={chip.running ? 'Montage rendering — open for progress' : chip.status === 'done' ? 'Montage ready — open to view & save' : 'Montage failed — open for details'}>
      {#if chip.running}
        <span class="chip-ring" aria-hidden="true">
          <svg viewBox="0 0 28 28">
            <circle class="track" cx="14" cy="14" r="11" />
            <circle class="bar" cx="14" cy="14" r="11" style="stroke-dasharray:{RING};stroke-dashoffset:{RING * (1 - pct / 100)}" />
          </svg>
          <span class="chip-pct">{pct}</span>
        </span>
        <span class="chip-text"><strong>Rendering montage</strong><small>{chip.detail || 'Working…'}</small></span>
      {:else if chip.status === 'done'}
        <span class="chip-icon ok" aria-hidden="true">▶</span>
        <span class="chip-text"><strong>Montage ready</strong><small>Tap to view &amp; save</small></span>
      {:else}
        <span class="chip-icon err" aria-hidden="true">!</span>
        <span class="chip-text"><strong>Montage failed</strong><small>Tap for details</small></span>
      {/if}
    </button>

    {#if !chip.running}
      <button type="button" class="chip-x" aria-label="Dismiss" onclick={() => acknowledgeMovie(chip.job_id)}>✕</button>
    {/if}
  </div>
{/if}

<style>
  .montage-chip {
    align-items: center;
    background: var(--surface-solid);
    border: 1px solid var(--line);
    border-radius: 999px;
    bottom: max(1rem, env(safe-area-inset-bottom));
    box-shadow: var(--shadow-dock);
    display: flex;
    gap: 0.15rem;
    max-width: min(22rem, calc(100vw - 2rem));
    overflow: hidden;
    padding: 0.4rem 0.5rem;
    position: fixed;
    right: max(1rem, env(safe-area-inset-right));
    z-index: 60;
  }

  .is-done { border-color: color-mix(in srgb, var(--accent) 55%, transparent); }
  .is-error { border-color: color-mix(in srgb, var(--danger) 55%, transparent); }

  .chip-main {
    align-items: center;
    background: transparent;
    display: inline-flex;
    gap: 0.55rem;
    min-width: 0;
    padding: 0.1rem 0.35rem 0.1rem 0.15rem;
    position: relative;
    text-align: left;
    z-index: 1;
  }

  .chip-ring { flex: 0 0 auto; display: grid; height: 28px; place-items: center; position: relative; width: 28px; }
  .chip-ring svg { height: 28px; transform: rotate(-90deg); width: 28px; }
  .chip-ring .track { fill: none; stroke: color-mix(in srgb, var(--line) 85%, transparent); stroke-width: 3; }
  .chip-ring .bar { fill: none; stroke: var(--accent); stroke-linecap: round; stroke-width: 3; transition: stroke-dashoffset 450ms ease; }
  .chip-pct { color: var(--ink); font-size: 0.5rem; font-weight: 800; position: absolute; }

  .chip-icon { flex: 0 0 auto; display: grid; height: 28px; place-items: center; border-radius: 999px; width: 28px; font-weight: 900; font-size: 0.8rem; }
  .chip-icon.ok { background: var(--accent); color: var(--on-accent); }
  .chip-icon.err { background: color-mix(in srgb, var(--danger) 22%, transparent); color: var(--danger-ink); }

  .chip-text { display: flex; flex-direction: column; line-height: 1.15; min-width: 0; }
  .chip-text strong { color: var(--ink); font-size: 0.8125rem; font-weight: 800; white-space: nowrap; }
  .chip-text small { color: color-mix(in srgb, var(--ink) 58%, transparent); font-size: 0.6875rem; max-width: 13rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .chip-x {
    color: color-mix(in srgb, var(--ink) 55%, transparent);
    display: grid;
    flex: 0 0 auto;
    height: 1.6rem;
    place-items: center;
    position: relative;
    border-radius: 999px;
    width: 1.6rem;
    z-index: 1;
  }
  .chip-x:hover { background: var(--surface-2); color: var(--ink); }

  :global(.chip-particles) { inset: 0; pointer-events: none; position: absolute; z-index: 0; }

  /* On phones / narrow viewports the SelectBar dock wraps and fills the bottom, so
     move the chip up under the header to keep both reachable. */
  @media (max-width: 900px) {
    .montage-chip {
      bottom: auto;
      top: calc(56px + max(0.5rem, env(safe-area-inset-top)));
    }
  }
</style>
