<script>
  import { onMount } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import SearchField from './SearchField.svelte';
  import {
    collections, collectionGroups, addCollection, addCollectionAndRemove, addToCollection, addToCollectionAndRemove,
    loadCollections, setStashed, setSelectMode, clearSelection
  } from '$lib/state.js';
  import { toast } from '$lib/toast.js';

  let { ids = [], currentCollection = null, onclose = () => {} } = $props();
  let q = $state('');
  let name = $state('');
  let groupName = $state('');
  let archiveAfter = $state(true);
  let removeAfter = $state(false);
  let initializedFor = '';

  // The collections list is a shared, server-owned store loaded once at app start. Refetch on every
  // open so a rename/add made on another device (e.g. on mobile) shows here instead of a stale label.
  onMount(loadCollections);

  const selected = $derived((ids || []).map(String).filter(Boolean));
  const sourceName = $derived(currentCollection?.name || '');
  // Sealed (password-locked + not unlocked this session) collections are hidden from
  // the picker — same predicate the Collections grid uses — so you can't add to one
  // without first unlocking it. A collection unlocked this session stays addable.
  const isSealed = (c) => c.locked && !c.unlocked;
  const existingGroups = $derived.by(() => {
    const byKey = {};
    for (const c of $collections || []) {
      const group = String(c.group || '').trim();
      if (group && !byKey[group.toLowerCase()]) byKey[group.toLowerCase()] = group;
    }
    for (const g of $collectionGroups || []) {
      const name = String(g.name || '').trim();
      if (name && !byKey[name.toLowerCase()]) byKey[name.toLowerCase()] = name;
    }
    return Object.values(byKey).sort((a, b) => a.localeCompare(b));
  });
  const canonicalGroup = (value) => {
    const clean = String(value || '').trim();
    if (!clean) return '';
    return existingGroups.find((g) => g.toLowerCase() === clean.toLowerCase()) || clean;
  };
  const displayName = (c) => {
    const group = String(c.group || '').trim();
    return group ? `${group} / ${c.name}` : c.name;
  };
  const searchText = (c) => `${c.name || ''} ${c.group || ''} ${displayName(c)}`.toLowerCase();
  const available = $derived(($collections || [])
    .filter((c) => !isSealed(c))
    .filter((c) => !currentCollection || c.id !== currentCollection.id)
  );
  // Most-recently-touched first (updated_at bumps when items are added/removed or the
  // collection is renamed) — the collection you're actively filling stays at the top,
  // instead of wherever it sits in the stored order. Same comparator as the grid's
  // "Recently updated"; filter() already copies, so sort() never mutates the store.
  const shown = $derived(
    available
      .filter((c) => !q.trim() || searchText(c).includes(q.trim().toLowerCase()))
      .sort((a, b) => (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || ''))
  );

  $effect(() => {
    const sourceId = currentCollection?.id || '';
    if (initializedFor === sourceId) return;
    initializedFor = sourceId;
    archiveAfter = !currentCollection;
    removeAfter = !!currentCollection;
    groupName = currentCollection?.group || currentCollection?.name || '';
  });

  function finish(collectionName, didMove) {
    const selectedNow = [...selected];
    const count = selectedNow.length;
    if (archiveAfter) setStashed(selectedNow, true);
    clearSelection();
    setSelectMode(false);
    toast(`${didMove ? 'Moved' : 'Added'} ${count} item${count === 1 ? '' : 's'} to "${collectionName}"`, { type: 'success' });
    setTimeout(() => loadCollections(), 250);
    onclose();
  }

  function create() {
    const clean = name.trim();
    if (!clean || !selected.length) return;
    const group = canonicalGroup(groupName);
    const move = !!currentCollection && removeAfter;
    if (move) addCollectionAndRemove(clean, selected, group ? { group } : {}, currentCollection.id);
    else addCollection(clean, selected, group ? { group } : {});
    finish(group ? `${group} / ${clean}` : clean, move);
  }

  function addExisting(c) {
    if (!selected.length) return;
    const move = !!currentCollection && removeAfter;
    if (move) addToCollectionAndRemove(c.id, selected, currentCollection.id);
    else addToCollection(c.id, selected);
    finish(displayName(c), move);
  }
</script>

<Modal {onclose} ariaLabel={currentCollection ? 'Move or add to collection' : 'Add to collection'} z="z-[65]" panelClass="panel flex max-h-[88dvh] w-full max-w-lg flex-col overflow-hidden rounded-card">
    <div class="border-b border-line p-4">
      <div class="mb-1 flex items-center justify-between gap-3">
        <h2 class="text-lg font-extrabold">{currentCollection ? 'Move/Add to Collection' : 'Add to Collection'}</h2>
        <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line" aria-label="Close" onclick={onclose}>✕</button>
      </div>
      <p class="text-sm text-muted">{selected.length} selected{sourceName ? ` from "${sourceName}"` : ''}</p>
    </div>

    <div class="flex flex-col gap-4 overflow-auto p-4">
      <div>
        <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">New collection</div>
        <div class="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
          <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
            placeholder="Collection name" bind:value={name} maxlength="80" />
          <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
            placeholder="Group" bind:value={groupName} list="collection-picker-groups" maxlength="120" />
          <datalist id="collection-picker-groups">
            {#each existingGroups as group (group)}
              <option value={group}></option>
            {/each}
          </datalist>
          <Button class="text-sm" disabled={!name.trim() || !selected.length} onclick={create}>Create</Button>
        </div>
      </div>

      <div class="grid gap-2">
        {#if currentCollection}
          <label class="flex items-center gap-2 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm">
            <input type="checkbox" bind:checked={removeAfter} />
            <span>Remove from current collection after adding</span>
          </label>
        {/if}
        <label class="flex items-center gap-2 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm">
          <input type="checkbox" bind:checked={archiveAfter} />
          <span>Archive after adding</span>
        </label>
      </div>

      <div>
        <div class="mb-2 flex items-center justify-between gap-3 text-xs font-bold uppercase tracking-wider text-muted">
          <span>Existing collections</span>
          <span>{available.length}</span>
        </div>
        <SearchField bind:value={q} placeholder="Search collections..." ariaLabel="collection search"
          wrapperClass="mb-2 w-full"
          inputClass="rounded-lg border border-line bg-[var(--surface-2)] py-2 pl-3 pr-10 text-sm outline-none" />
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
                  <span class="block truncate text-sm font-semibold">{displayName(c)}</span>
                  <span class="block text-xs text-muted">{c.item_count ?? c.ids?.length ?? 0} items</span>
                </span>
              </button>
            {/each}
          {/if}
        </div>
      </div>
    </div>
</Modal>
