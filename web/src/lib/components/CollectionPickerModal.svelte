<script>
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import {
    collections, addCollection, addToCollection,
    loadCollections, setStashed, setSelectMode, clearSelection
  } from '$lib/state.js';
  import { toast } from '$lib/toast.js';

  let { ids = [], onclose = () => {} } = $props();
  let q = $state('');
  let name = $state('');
  let archiveAfter = $state(false);

  const selected = $derived((ids || []).map(String).filter(Boolean));
  const shown = $derived(($collections || []).filter((c) => !q.trim() || c.name.toLowerCase().includes(q.trim().toLowerCase())));

  function finish(collectionName) {
    const selectedNow = [...selected];
    const count = selectedNow.length;
    if (archiveAfter) setStashed(selectedNow, true);
    clearSelection();
    setSelectMode(false);
    toast(`Added ${count} item${count === 1 ? '' : 's'} to "${collectionName}"`, { type: 'success' });
    setTimeout(() => loadCollections(), 250);
    onclose();
  }

  function create() {
    const clean = name.trim();
    if (!clean || !selected.length) return;
    addCollection(clean, selected);
    finish(clean);
  }

  function addExisting(c) {
    if (!selected.length) return;
    addToCollection(c.id, selected);
    finish(c.name);
  }
</script>

<Modal {onclose} ariaLabel="Add to collection" z="z-[65]" panelClass="panel flex max-h-[88dvh] w-full max-w-lg flex-col overflow-hidden rounded-card">
    <div class="border-b border-line p-4">
      <div class="mb-1 flex items-center justify-between gap-3">
        <h2 class="text-lg font-extrabold">Add to Collection</h2>
        <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line" aria-label="Close" onclick={onclose}>✕</button>
      </div>
      <p class="text-sm text-muted">{selected.length} selected</p>
    </div>

    <div class="flex flex-col gap-4 overflow-auto p-4">
      <div>
        <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">New collection</div>
        <div class="flex gap-2">
          <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
            placeholder="Collection name" bind:value={name} maxlength="80" />
          <Button class="text-sm" disabled={!name.trim() || !selected.length} onclick={create}>Create</Button>
        </div>
      </div>

      <label class="flex items-center gap-2 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm">
        <input type="checkbox" bind:checked={archiveAfter} />
        <span>Archive after adding</span>
      </label>

      <div>
        <div class="mb-2 flex items-center justify-between gap-3 text-xs font-bold uppercase tracking-wider text-muted">
          <span>Existing collections</span>
          <span>{$collections.length}</span>
        </div>
        <input class="mb-2 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
          placeholder="Search collections..." bind:value={q} />
        <div class="flex max-h-72 flex-col gap-1 overflow-auto">
          {#if !shown.length}
            <p class="py-6 text-center text-sm text-muted">No collections match.</p>
          {:else}
            {#each shown as c (c.id)}
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
          {/if}
        </div>
      </div>
    </div>
</Modal>
