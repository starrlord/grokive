<script>
  import { onMount } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import {
    collections, addCollection, addCollectionAndRemove, addToCollection, addToCollectionAndRemove,
    loadCollections, setSelectMode, clearSelection
  } from '$lib/state.js';
  import { toast } from '$lib/toast.js';

  let { ids = [], parent = null, previewThumbs = [], onclose = () => {} } = $props();
  let name = $state('');
  // Default ON (Joshua's call): nesting normally MOVES items into the sub-collection,
  // so the parent grid reads as "what's not yet filed". Untick to keep them in both.
  let removeAfter = $state(true);

  // Refetch on open (same as the collection picker) so sub-collections created on
  // another device show up instead of a stale list.
  onMount(loadCollections);

  const selected = $derived((ids || []).map(String).filter(Boolean));
  // Sealed (locked + not unlocked this session) children can't accept adds — same
  // predicate the Collections grid and picker use.
  const isSealed = (c) => c.locked && !c.unlocked;
  // Existing sub-collections of THIS parent, the collection you're actively filling
  // first — same recency comparator as the picker.
  const children = $derived(($collections || [])
    .filter((c) => c.parent_id === parent?.id && !isSealed(c))
    .sort((a, b) => (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || '')));

  function finish(childName) {
    const count = selected.length;
    clearSelection();
    setSelectMode(false);
    toast(`${removeAfter ? 'Moved' : 'Added'} ${count} item${count === 1 ? '' : 's'} to "${childName}" in "${parent?.name}"`, { type: 'success' });
    setTimeout(() => loadCollections(), 250);
    onclose();
  }

  function create() {
    const clean = name.trim();
    if (!clean || !selected.length || !parent) return;
    if (removeAfter) addCollectionAndRemove(clean, selected, { parent_id: parent.id }, parent.id);
    else addCollection(clean, selected, { parent_id: parent.id });
    finish(clean);
  }

  function addExisting(c) {
    if (!selected.length || !parent) return;
    if (removeAfter) addToCollectionAndRemove(c.id, selected, parent.id);
    else addToCollection(c.id, selected);
    finish(c.name);
  }
</script>

<Modal {onclose} ariaLabel="Add to nested collection" z="z-[65]" panelClass="panel flex max-h-[88dvh] w-full max-w-lg flex-col overflow-hidden rounded-card">
  <div class="border-b border-line p-4">
    <div class="mb-1 flex items-center justify-between gap-3">
      <h2 class="text-lg font-extrabold">Add to Nested Collection</h2>
      <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line" aria-label="Close" onclick={onclose}>✕</button>
    </div>
    <p class="text-sm text-muted">{selected.length} selected from "{parent?.name}"</p>
  </div>

  <div class="flex flex-col gap-4 overflow-auto p-4">
    {#if previewThumbs.length}
      <!-- What's about to be filed: the first few selected covers, so the folder you're
           minting has a face before it exists. -->
      <div class="flex items-center gap-1.5">
        {#each previewThumbs.slice(0, 4) as thumb (thumb)}
          <img src={thumb} alt="" class="h-12 w-16 rounded-sm border border-line object-cover" />
        {/each}
        {#if selected.length > 4}
          <span class="grid h-12 w-16 place-items-center rounded-sm border border-line bg-[var(--surface-2)] text-xs font-bold text-muted">+{selected.length - 4}</span>
        {/if}
      </div>
    {/if}

    <div>
      <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">New sub-collection</div>
      <div class="flex gap-2">
        <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
          placeholder="Sub-collection name" bind:value={name} maxlength="80"
          onkeydown={(e) => { if (e.key === 'Enter') create(); }} />
        <Button class="text-sm" disabled={!name.trim() || !selected.length} onclick={create}>Create</Button>
      </div>
    </div>

    <label class="flex items-center gap-2 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm">
      <input type="checkbox" bind:checked={removeAfter} />
      <span>Also remove from "{parent?.name}"</span>
    </label>

    {#if children.length}
      <div>
        <div class="mb-2 flex items-center justify-between gap-3 text-xs font-bold uppercase tracking-wider text-muted">
          <span>Existing sub-collections</span>
          <span>{children.length}</span>
        </div>
        <div class="flex max-h-60 flex-col gap-1 overflow-auto">
          {#each children as c (c.id)}
            <button type="button" class="flex items-center gap-2 rounded-lg border border-line p-2 text-left transition hover:border-[var(--accent)]"
              onclick={() => addExisting(c)}>
              {#if c.cover}
                <img src={c.cover} alt="" class="h-10 w-14 shrink-0 rounded-sm object-cover" />
              {:else}
                <span class="h-10 w-14 shrink-0 rounded-sm bg-[var(--media-placeholder)]"></span>
              {/if}
              <span class="min-w-0 flex-1">
                <span class="block truncate text-sm font-semibold">{c.name}</span>
                <span class="block text-xs text-muted">{c.item_count ?? c.ids?.length ?? 0} items</span>
              </span>
            </button>
          {/each}
        </div>
      </div>
    {/if}
  </div>
</Modal>
