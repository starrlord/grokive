<script>
  import { filters, toggleTag, toggleModel, toggleResolution, setMediaType, clearFilters } from '$lib/state.js';
  import PlaylistsPanel from './PlaylistsPanel.svelte';

  let { facets = { tags: [], models: [] }, onplay = () => {}, onedit = () => {}, onbrowse = () => {} } = $props();

  const topTags = $derived((facets.tags || []).slice(0, 12));
  const types = [
    { id: 'all', label: 'All' },
    { id: 'image', label: 'Images' },
    { id: 'video', label: 'Videos' }
  ];
</script>

<!-- Just the content: the wrapper (desktop <aside> or mobile drawer) owns
     width, scrolling and visibility so this can be reused in both. -->
<div class="p-4">
  <div class="mb-5">
    <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Media type</div>
    <div class="grid grid-cols-3 gap-1 rounded-lg border border-line bg-[var(--surface-2)] p-1">
      {#each types as t}
        <button type="button"
          class="rounded-md py-1.5 text-sm font-semibold {$filters.mediaType === t.id ? 'bg-[var(--surface-solid)] text-ink shadow-sm' : 'text-muted'}"
          onclick={() => setMediaType(t.id)}>{t.label}</button>
      {/each}
    </div>
  </div>

  {#if facets.resolutions?.length}
    <div class="mb-5">
      <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Resolution</div>
      <div class="flex flex-wrap gap-1.5">
        {#each facets.resolutions as r (r.height)}
          <button type="button"
            class="rounded-full border px-2.5 py-1 text-xs font-semibold transition {$filters.resolutions.includes(r.height) ? 'border-transparent bg-[var(--accent)] text-white' : 'border-line hover:border-[var(--accent)]'}"
            onclick={() => toggleResolution(r.height)}>{r.height}p <span class="opacity-70">{r.count}</span></button>
        {/each}
      </div>
    </div>
  {/if}

  <div class="mb-5">
    <div class="mb-2 flex items-center justify-between text-xs font-bold uppercase tracking-wider text-muted">
      <span>Tags</span>
      <button class="font-semibold normal-case text-[var(--accent)] hover:underline" onclick={onbrowse}>Browse all {facets.tags?.length || 0} →</button>
    </div>

    {#if $filters.tags.length}
      <div class="mb-2 flex flex-wrap gap-1.5">
        {#each $filters.tags as t}
          <button class="rounded-full bg-[var(--accent)] px-2.5 py-1 text-xs font-semibold text-white" onclick={() => toggleTag(t)}>{t} ✕</button>
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
  </div>

  <PlaylistsPanel {onplay} {onedit} />

  {#if facets.models?.length}
    <div class="mb-5">
      <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Models</div>
      <div class="flex flex-col gap-1">
        {#each facets.models as m (m.name)}
          <button type="button"
            class="flex items-center justify-between rounded-lg border px-2.5 py-1.5 text-left text-sm {$filters.models.includes(m.name) ? 'border-transparent bg-[var(--accent)] text-white' : 'border-line'}"
            onclick={() => toggleModel(m.name)}>
            <span class="truncate">{m.name}</span><span class="ml-2 text-xs opacity-70">{m.count}</span>
          </button>
        {/each}
      </div>
    </div>
  {/if}

  <button type="button" class="w-full rounded-lg border border-line py-2 text-sm font-semibold" onclick={clearFilters}>Clear filters</button>
</div>
