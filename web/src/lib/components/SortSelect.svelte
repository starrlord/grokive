<script>
  // Inline media-sort dropdown for the grid headers (Recent / All Media / Favorites /
  // Archive, and the collection/canvas detail views). Server-side sort: it just drives
  // the shared `filters.sort`, which db.query_media turns into ORDER BY and re-fetches.
  // Stays in sync with the same control in the TopBar Display popover (one store).
  import { filters, setSort } from '$lib/state.js';

  let { class: cls = '' } = $props();
  const options = [
    { value: 'new', label: 'Newest' },
    { value: 'old', label: 'Oldest' },
    { value: 'prompt', label: 'Prompt A–Z' },
    { value: 'model', label: 'Model A–Z' }
  ];
</script>

<select aria-label="Sort" title="Sort"
  class="shrink-0 rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm font-semibold {cls}"
  value={$filters.sort} onchange={(e) => setSort(e.target.value)}>
  {#each options as o (o.value)}<option value={o.value}>{o.label}</option>{/each}
</select>
