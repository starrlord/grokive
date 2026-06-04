<script>
  import { filters, toggleTag, toggleModel, toggleResolution, setMediaType, clearFilters } from '$lib/state.js';
  import { portal } from '$lib/portal.js';

  let { facets = { tags: [], models: [] }, onclose = () => {} } = $props();
  let q = $state('');

  const shown = $derived((facets.tags || []).filter((t) => !q || t.name.toLowerCase().includes(q.toLowerCase())));
  const maxCount = $derived(Math.max(1, ...(facets.tags || []).map((t) => t.count)));
  // Font size scales with sqrt of frequency: ~0.8rem (rare) … ~1.7rem (most used).
  const size = (c) => (0.8 + Math.sqrt(c / maxCount) * 0.9).toFixed(2);

  const types = [
    { id: 'all', label: 'All' },
    { id: 'image', label: 'Images' },
    { id: 'video', label: 'Videos' }
  ];
</script>

<svelte:window onkeydown={(e) => e.key === 'Escape' && onclose()} />

<!-- Backdrop is presentational chrome; dismissal is mirrored by Escape (above) and the Done button. -->
<div use:portal class="fixed inset-0 z-[60] grid place-items-center bg-[var(--overlay)] p-4 backdrop-blur-sm" role="presentation"
     onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="panel flex max-h-[88dvh] w-full max-w-3xl flex-col overflow-hidden rounded-card" role="dialog" aria-modal="true" aria-label="Filters" tabindex="-1">
    <div class="flex items-center gap-3 border-b border-line p-4">
      <input class="flex-1 rounded-full border border-line bg-[var(--surface-2)] px-4 py-2 text-sm outline-none" placeholder="Filter tags…" bind:value={q} />
      <button class="rounded-lg bg-[var(--accent)] px-4 py-2 font-bold text-[var(--on-accent)]" onclick={onclose}>Done</button>
    </div>

    <div class="overflow-auto p-4">
      <div class="mb-5">
        <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Media type</div>
        <div class="inline-grid grid-cols-3 gap-1 rounded-lg border border-line bg-[var(--surface-2)] p-1">
          {#each types as t (t.id)}
            <button class="rounded-md px-5 py-1.5 text-sm font-semibold {$filters.mediaType === t.id ? 'bg-[var(--surface-solid)] shadow-sm' : 'text-muted'}" onclick={() => setMediaType(t.id)}>{t.label}</button>
          {/each}
        </div>
      </div>

      {#if facets.resolutions?.length}
        <div class="mb-5">
          <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Resolution</div>
          <div class="flex flex-wrap gap-2">
            {#each facets.resolutions as r (r.height)}
              <button type="button"
                class="rounded-full border px-3 py-1 text-sm font-semibold transition {$filters.resolutions.includes(r.height) ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                onclick={() => toggleResolution(r.height)}>{r.height}p <span class="opacity-60">{r.count}</span></button>
            {/each}
          </div>
        </div>
      {/if}

      <div class="mb-5">
        <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Tags · {shown.length}</div>
        <div class="flex flex-wrap items-baseline gap-2">
          {#each shown as t (t.name)}
            <button class="rounded-full px-2.5 py-1 leading-none transition {$filters.tags.includes(t.name) ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'border border-line hover:border-[var(--accent)]'}"
              style="font-size:{size(t.count)}rem" onclick={() => toggleTag(t.name)}>
              {t.name}<span class="ml-1 align-super text-[0.6em] opacity-60">{t.count}</span>
            </button>
          {/each}
          {#if !shown.length}<p class="text-sm text-muted">No tags match “{q}”.</p>{/if}
        </div>
      </div>

      {#if facets.models?.length}
        <div>
          <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Models</div>
          <div class="flex flex-wrap gap-2">
            {#each facets.models as m (m.name)}
              <button class="rounded-full px-3 py-1 text-sm {$filters.models.includes(m.name) ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'border border-line'}" onclick={() => toggleModel(m.name)}>{m.name} <span class="opacity-60">{m.count}</span></button>
            {/each}
          </div>
        </div>
      {/if}
    </div>

    <div class="flex gap-2 border-t border-line p-4">
      <button class="rounded-lg border border-line px-4 py-2 font-semibold" onclick={() => clearFilters()}>Clear all</button>
    </div>
  </div>
</div>
