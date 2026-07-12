<script>
  import { onMount } from 'svelte';
  import { Tween } from 'svelte/motion';
  import { cubicOut } from 'svelte/easing';
  import { getStats } from '$lib/api.js';
  import { fmtSize, fmtCount } from '$lib/format.js';
  import Modal from './Modal.svelte';

  let { onclose = () => {} } = $props();

  let loading = $state(true);
  let error = $state('');
  let data = $state({ videos: 0, images: 0, total: 0, bytes: 0 });

  // Count-up on load — numbers ease from 0 to their real value so the panel feels
  // alive rather than popping in fully formed. Bytes runs a touch longer so the
  // size readout keeps climbing after the counts have settled.
  const tVideos = new Tween(0, { duration: 750, easing: cubicOut });
  const tImages = new Tween(0, { duration: 750, easing: cubicOut });
  const tBytes = new Tween(0, { duration: 950, easing: cubicOut });

  onMount(async () => {
    try {
      data = await getStats();
      tVideos.target = data.videos;
      tImages.target = data.images;
      tBytes.target = data.bytes;
    } catch {
      error = "Couldn't load library stats.";
    } finally {
      loading = false;
    }
  });

  // Current-month per-day average, formatted like fmtSize's numbers: one decimal
  // under 10, whole at/above, trailing ".0" dropped. Null when the payload lacks
  // month data (older backend) so the line is omitted rather than showing junk.
  function perDay(count) {
    const m = data.month;
    if (!m?.days) return null;
    const n = count / m.days;
    return (n < 10 ? n.toFixed(1) : Math.round(n).toString()).replace(/\.0$/, '');
  }
</script>

<Modal {onclose} ariaLabel="Library Stats" panelClass="panel w-full max-w-md overflow-hidden rounded-card">
  <header class="flex items-center gap-2 border-b border-line px-5 py-3.5">
    <svg viewBox="0 0 24 24" class="h-5 w-5 text-[var(--accent)]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3v18h18"/><rect x="7" y="11" width="3" height="6" rx="0.5"/><rect x="12" y="7" width="3" height="10" rx="0.5"/><rect x="17" y="13" width="3" height="4" rx="0.5"/></svg>
    <h2 class="text-lg font-bold">Library Stats</h2>
    <button type="button" class="ml-auto grid h-9 w-9 place-items-center rounded-lg border border-line transition hover:bg-[var(--surface-2)] pointer-coarse:h-11 pointer-coarse:w-11"
      aria-label="Close" onclick={onclose}>✕</button>
  </header>

  <div class="p-5">
    {#if loading}
      <div class="grid h-44 place-items-center">
        <svg viewBox="0 0 24 24" class="h-7 w-7 animate-spin text-[var(--accent)]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-label="Loading"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
      </div>
    {:else if error}
      <p class="py-12 text-center text-sm text-muted">{error}</p>
    {:else}
      <!-- Hero: total library size -->
      <div class="stat-hero relative overflow-hidden rounded-2xl border border-line p-5">
        <div class="relative flex items-center gap-4">
          <span class="grid h-12 w-12 shrink-0 place-items-center rounded-xl bg-[var(--accent)] text-[var(--on-accent)]">
            <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/></svg>
          </span>
          <div class="min-w-0">
            <div class="text-xs font-bold uppercase tracking-wider text-muted">Total size</div>
            <div class="truncate text-3xl font-black leading-tight tabular-nums">{fmtSize(Math.round(tBytes.current)) || '0 B'}</div>
            <div class="mt-0.5 text-xs text-muted">across {fmtCount(data.total)} item{data.total === 1 ? '' : 's'}</div>
          </div>
        </div>
      </div>

      <!-- Videos + images -->
      <div class="mt-3 grid grid-cols-2 gap-3">
        <div class="rounded-2xl border border-line p-4">
          <div class="mb-2 flex items-center gap-2 text-muted">
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m10 9 5 3-5 3z"/></svg>
            <span class="text-xs font-bold uppercase tracking-wider">Videos</span>
          </div>
          <div class="text-3xl font-black leading-none tabular-nums">{fmtCount(Math.round(tVideos.current))}</div>
          {#if data.month}
            <div class="mt-1.5 text-xs text-muted tabular-nums">≈ {perDay(data.month.videos)}/day this month</div>
          {/if}
        </div>
        <div class="rounded-2xl border border-line p-4">
          <div class="mb-2 flex items-center gap-2 text-muted">
            <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-4.35-4.35a2 2 0 0 0-2.83 0L4 20"/></svg>
            <span class="text-xs font-bold uppercase tracking-wider">Images</span>
          </div>
          <div class="text-3xl font-black leading-none tabular-nums">{fmtCount(Math.round(tImages.current))}</div>
          {#if data.month}
            <div class="mt-1.5 text-xs text-muted tabular-nums">≈ {perDay(data.month.images)}/day this month</div>
          {/if}
        </div>
      </div>
    {/if}
  </div>
</Modal>

<style>
  .stat-hero {
    background:
      radial-gradient(120% 140% at 100% 0%, color-mix(in srgb, var(--accent) 18%, transparent), transparent 60%),
      color-mix(in srgb, var(--accent) 6%, var(--surface-2));
  }
</style>
