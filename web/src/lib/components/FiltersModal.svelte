<script>
  import { filters, toggleTag, toggleModel, toggleResolution, setMediaType, clearFilters } from '$lib/state.js';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import SearchField from './SearchField.svelte';

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

<Modal {onclose} ariaLabel="Filters" z="z-[60]" panelClass="panel flex max-h-[88dvh] w-full max-w-3xl flex-col overflow-hidden rounded-card">
    <div class="flex items-center gap-3 border-b border-line p-4">
      <SearchField bind:value={q} placeholder="Filter tags…" ariaLabel="tag filter" wrapperClass="flex-1"
        inputClass="rounded-full border border-line bg-[var(--surface-2)] py-2 pl-4 pr-10 text-sm outline-none" />
      <Button onclick={onclose}>Done</Button>
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
            {#each facets.resolutions as r (`${r.height}-${r.orientation}`)}
              {@const key = `${r.height}-${r.orientation}`}
              <button type="button"
                class="rounded-full border px-3 py-1 text-sm font-semibold capitalize transition {$filters.resolutions.includes(key) ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                onclick={() => toggleResolution(key)}>{r.height}p {r.orientation} <span class="opacity-60">{r.count}</span></button>
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
      <Button variant="secondary" onclick={() => clearFilters()}>Clear all</Button>
    </div>
</Modal>
