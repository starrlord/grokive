<script>
  // The Library page: one home for the two kinds of saved grouping — Collections (media buckets)
  // and Playlists (video queues). Both are built the same way (Select → save), so they live under
  // one switch instead of being split between a top-level tab and the filter sidebar.
  import CollectionsGrid from './CollectionsGrid.svelte';
  import PlaylistsGrid from './PlaylistsGrid.svelte';
  import { playlists, collections } from '$lib/state.js';

  let {
    onopencollection = () => {}, onplaycollection = () => {}, onqueuecollection = () => {},
    onplayqueuecollection = () => {},
    onimportcollection = () => {}, onplayplaylist = () => {}, oneditplaylist = () => {}
  } = $props();

  let tab = $state('collections'); // collections | playlists
  const tabs = $derived([
    { id: 'collections', label: 'Collections', n: ($collections || []).length },
    { id: 'playlists', label: 'Playlists', n: ($playlists || []).length }
  ]);
</script>

<div class="mb-4 inline-flex rounded-lg border border-line bg-[var(--surface-2)] p-0.5 text-sm font-semibold" role="group" aria-label="Library section">
  {#each tabs as t (t.id)}
    <button type="button" aria-pressed={tab === t.id}
      class="inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 transition {tab === t.id ? 'bg-[var(--surface-solid)] text-ink shadow-sm' : 'text-muted hover:text-ink'}"
      onclick={() => (tab = t.id)}>
      {t.label}
      <span class="rounded-full px-1.5 text-xs font-semibold {tab === t.id ? 'bg-[var(--surface-2)] text-muted' : 'text-muted opacity-70'}">{t.n}</span>
    </button>
  {/each}
</div>

{#if tab === 'collections'}
  <CollectionsGrid onopen={onopencollection} onplay={onplaycollection} onqueue={onqueuecollection} onplayqueue={onplayqueuecollection} onimport={onimportcollection} />
{:else}
  <PlaylistsGrid onplay={onplayplaylist} onedit={oneditplaylist} />
{/if}
