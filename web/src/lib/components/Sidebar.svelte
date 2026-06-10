<script>
  import { filters, toggleTag, toggleModel, toggleResolution, clearFilters } from '$lib/state.js';
  import Collapsible from './Collapsible.svelte';

  let { facets = { tags: [], models: [] }, onbrowse = () => {} } = $props();

  const topTags = $derived((facets.tags || []).slice(0, 12));
</script>

<!-- Filters only. Media type lives in the grid header (high-frequency, applies everywhere) and
     Playlists moved to the Library page — so the sidebar is now a single-purpose: refine the grid.
     The wrapper (desktop <aside> or mobile drawer) owns width, scrolling and visibility. -->
<div class="p-4">
  {#if facets.resolutions?.length}
    <Collapsible title="Resolution" count={facets.resolutions.length} open={false}>
      <div class="flex flex-wrap gap-1.5">
        {#each facets.resolutions as r (`${r.height}-${r.orientation}`)}
          {@const key = `${r.height}-${r.orientation}`}
          <button type="button"
            class="rounded-full border px-2.5 py-1 text-xs font-semibold capitalize transition {$filters.resolutions.includes(key) ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
            onclick={() => toggleResolution(key)}>{r.height}p {r.orientation} <span class="opacity-70">{r.count}</span></button>
        {/each}
      </div>
    </Collapsible>
  {/if}

  <Collapsible title="Tags">
    {#if $filters.tags.length}
      <div class="mb-2 flex flex-wrap gap-1.5">
        {#each $filters.tags as t (t)}
          <button class="rounded-full bg-[var(--accent)] px-2.5 py-1 text-xs font-semibold text-[var(--on-accent)]" onclick={() => toggleTag(t)}>{t} ✕</button>
        {/each}
      </div>
    {/if}
    <div class="flex flex-wrap gap-1.5">
      {#each topTags as t (t.name)}
        {#if !$filters.tags.includes(t.name)}
          <button class="rounded-full border border-line px-2.5 py-1 text-xs hover:border-[var(--accent)]" onclick={() => toggleTag(t.name)}>{t.name} <span class="opacity-70">{t.count}</span></button>
        {/if}
      {/each}
    </div>
    <button class="mt-2.5 text-xs font-semibold text-[var(--accent)] hover:underline" onclick={onbrowse}>Browse all {facets.tags?.length || 0} tags →</button>
  </Collapsible>

  {#if facets.models?.length}
    <Collapsible title="Models" count={facets.models.length}>
      <div class="flex flex-col gap-1">
        {#each facets.models as m (m.name)}
          <button type="button"
            class="flex items-center justify-between rounded-lg border px-2.5 py-1.5 text-left text-sm {$filters.models.includes(m.name) ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line'}"
            onclick={() => toggleModel(m.name)}>
            <span class="truncate">{m.name}</span><span class="ml-2 text-xs opacity-70">{m.count}</span>
          </button>
        {/each}
      </div>
    </Collapsible>
  {/if}

  <button type="button" class="w-full rounded-lg border border-line py-2 text-sm font-semibold transition hover:border-[var(--accent)]" onclick={clearFilters}>Clear filters</button>
</div>
