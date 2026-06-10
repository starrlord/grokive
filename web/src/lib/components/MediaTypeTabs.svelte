<script>
  // The All / Images / Videos filter. Lifted out of the sidebar into the grid header: it's
  // the highest-frequency filter and the one that applies to every media view, so it earns a
  // visible, one-tap home next to the grid it reshapes (and stays reachable on mobile, where
  // the sidebar is behind the drawer).
  import { filters, setMediaType } from '$lib/state.js';

  let { class: cls = '' } = $props();
  const types = [
    { id: 'all', label: 'All' },
    { id: 'image', label: 'Images' },
    { id: 'video', label: 'Videos' }
  ];
</script>

<div class="inline-flex shrink-0 rounded-lg border border-line bg-[var(--surface-2)] p-0.5 text-sm font-semibold {cls}" role="group" aria-label="Media type">
  {#each types as t (t.id)}
    <button type="button" aria-pressed={$filters.mediaType === t.id}
      class="rounded-md px-3 py-1 transition {$filters.mediaType === t.id ? 'bg-[var(--surface-solid)] text-ink shadow-sm' : 'text-muted hover:text-ink'}"
      onclick={() => setMediaType(t.id)}>{t.label}</button>
  {/each}
</div>
