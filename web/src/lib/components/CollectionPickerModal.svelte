<script>
  import { onMount, tick } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import SearchField from './SearchField.svelte';
  import {
    collections, collectionGroups, addCollection, addCollectionAndRemove, addToCollection, addToCollectionAndRemove,
    loadCollections, setStashed, setSelectMode, clearSelection, loadLastGroup, saveLastGroup
  } from '$lib/state.js';
  import { toast } from '$lib/toast.js';

  let { ids = [], currentCollection = null, onclose = () => {} } = $props();
  let q = $state('');
  let name = $state('');
  // Sentinel for the "New group…" dropdown row. Real group names are user-typed trimmed
  // text, so a NUL character can never collide with one.
  const NEW_GROUP = '\u0000';
  let groupChoice = $state(''); // '' = no group | existing group name | NEW_GROUP | NEST_PREFIX+parentId
  // Prefix sentinel for a chosen "Nest inside" parent: value = NEST_PREFIX + collection id.
  // Same reasoning as NEW_GROUP — a control char can't appear in a typed group name — and
  // it survives trim() (U+0001 isn't whitespace), so the last-used memory can store it too.
  const NEST_PREFIX = String.fromCharCode(1);
  // Sentinel for the "Nest inside…" dropdown row. Transient UI state only — while active the
  // select swaps to a parent search (like New group's input swap); it's never persisted and
  // create() treats it as "no group" if the user commits without picking a parent.
  const NEST_PICK = String.fromCharCode(2);
  let newGroup = $state('');
  let newGroupInput = $state(null);
  let nestQ = $state('');
  let nestIdx = $state(0); // keyboard highlight in the nest-parent list
  let nestInput = $state(null);
  let nestListEl = $state(null);
  let archiveAfter = $state(true);
  let removeAfter = $state(false);
  let initializedFor = null;

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
  const byId = $derived(new Map(($collections || []).map((c) => [c.id, c])));
  const displayName = (c) => {
    // A nested collection reads as "Parent › Child" so it stays findable as an
    // add-target from anywhere; grouped roots keep their "Group / Name" label.
    if (c.parent_id) {
      const parent = byId.get(c.parent_id);
      if (parent) return `${parent.name} › ${c.name}`;
    }
    const group = String(c.group || '').trim();
    return group ? `${group} / ${c.name}` : c.name;
  };
  const searchText = (c) => `${c.name || ''} ${c.group || ''} ${displayName(c)}`.toLowerCase();
  const available = $derived(($collections || [])
    .filter((c) => !isSealed(c))
    .filter((c) => !currentCollection || c.id !== currentCollection.id)
  );
  // Root (top-level, unsealed) collections offered as "Nest inside" targets for a NEW
  // collection — one level deep, so children can't be parents, and a sealed vault can't
  // be nested into (the server would 403 it anyway). Recency-first, same comparator as
  // the Existing list below: the parent you're actively filling floats to the top.
  const rootParents = $derived(($collections || [])
    .filter((c) => !c.parent_id && !isSealed(c))
    .sort((a, b) => (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || '')));
  // The chosen nest parent (chip state), or null when groupChoice isn't a nest value.
  const nestParent = $derived(groupChoice.startsWith(NEST_PREFIX) ? byId.get(groupChoice.slice(1)) : null);
  const nestShown = $derived(
    rootParents.filter((c) => !nestQ.trim() || searchText(c).includes(nestQ.trim().toLowerCase()))
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

  // Seed the group dropdown once per source context. Opened from inside a collection, that
  // collection's group (or, ungrouped, its name — as a suggested new group) wins; opened from
  // the library, the last group committed by Create is re-selected. Either way a remembered
  // name that no longer matches an existing group falls back rather than showing a dead option.
  $effect(() => {
    const sourceId = currentCollection?.id || '';
    if (initializedFor === sourceId) return;
    initializedFor = sourceId;
    archiveAfter = !currentCollection;
    removeAfter = !!currentCollection;
    if (currentCollection) {
      const seed = canonicalGroup(currentCollection.group || currentCollection.name || '');
      if (existingGroups.includes(seed)) { groupChoice = seed; newGroup = ''; }
      else { groupChoice = NEW_GROUP; newGroup = seed; }
    } else {
      const raw = loadLastGroup();
      if (raw.startsWith(NEST_PREFIX)) {
        // Last create was nested — reselect that parent if it's still a valid target;
        // a deleted/locked/now-nested parent falls back to No group, never a dead option.
        groupChoice = rootParents.some((c) => c.id === raw.slice(1)) ? raw : '';
        newGroup = '';
      } else {
        const remembered = canonicalGroup(raw);
        groupChoice = existingGroups.includes(remembered) ? remembered : '';
        newGroup = '';
      }
    }
  });

  // Focus the swapped-in input as soon as "New group…" / "Nest inside…" is picked — the
  // {#if} swap hasn't rendered it yet inside the change handler, hence the tick().
  async function onGroupSelect() {
    if (groupChoice === NEW_GROUP) {
      await tick();
      newGroupInput?.focus();
    } else if (groupChoice === NEST_PICK) {
      nestQ = '';
      nestIdx = 0;
      await tick();
      nestInput?.focus();
    }
  }

  function pickParent(c) {
    groupChoice = NEST_PREFIX + c.id;
    nestQ = '';
  }

  // Keyboard flow for the nest search: arrows move the highlight, Enter picks it, Escape
  // clears the query first, then backs out to the group select — stopPropagation keeps
  // those Escapes from reaching the Modal's window listener and closing the dialog.
  function onNestKey(e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!nestShown.length) return;
      nestIdx = e.key === 'ArrowDown'
        ? Math.min(nestIdx + 1, nestShown.length - 1)
        : Math.max(nestIdx - 1, 0);
      tick().then(() => nestListEl?.children[nestIdx]?.scrollIntoView({ block: 'nearest' }));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const c = nestShown[nestIdx];
      if (c) pickParent(c);
    } else if (e.key === 'Escape') {
      e.stopPropagation();
      if (nestQ) { nestQ = ''; nestIdx = 0; }
      else groupChoice = '';
    }
  }

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
    // A "Nest inside" choice makes the new collection a sub-collection of that parent;
    // group and parent are mutually exclusive by construction (one dropdown, one answer),
    // matching the server model (children carry no group). A parent deleted since the
    // modal opened falls back to a plain root collection rather than failing.
    const parentId = groupChoice.startsWith(NEST_PREFIX) ? groupChoice.slice(1) : '';
    const parent = parentId ? byId.get(parentId) : null;
    // Mid-search (NEST_PICK, no parent committed yet) falls back to a plain ungrouped
    // collection — the transient sentinel must never leak into a group name.
    const group = parent ? '' : (groupChoice === NEW_GROUP ? canonicalGroup(newGroup) : (groupChoice === NEST_PICK ? '' : groupChoice));
    saveLastGroup(parent ? NEST_PREFIX + parent.id : group); // '' (No group) clears the memory
    const patch = parent ? { parent_id: parent.id } : (group ? { group } : {});
    const move = !!currentCollection && removeAfter;
    if (move) addCollectionAndRemove(clean, selected, patch, currentCollection.id);
    else addCollection(clean, selected, patch);
    finish(parent ? `${parent.name} › ${clean}` : (group ? `${group} / ${clean}` : clean), move);
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
          {#if groupChoice === NEW_GROUP}
            <div class="relative min-w-0">
              <input class="w-full rounded-lg border border-line bg-[var(--surface-2)] py-2 pl-3 pr-9 text-sm outline-none"
                placeholder="New group" bind:value={newGroup} bind:this={newGroupInput} maxlength="120" />
              <button type="button" class="absolute right-1.5 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded text-muted transition hover:text-[var(--ink)]"
                aria-label="Back to group list" title="Back to group list"
                onclick={() => { groupChoice = ''; newGroup = ''; }}>✕</button>
            </div>
          {:else if groupChoice === NEST_PICK}
            <div class="relative min-w-0">
              <input class="w-full rounded-lg border border-line bg-[var(--surface-2)] py-2 pl-3 pr-9 text-sm outline-none"
                placeholder="Search collections…" aria-label="Search collections to nest inside"
                bind:value={nestQ} bind:this={nestInput}
                oninput={() => (nestIdx = 0)} onkeydown={onNestKey} />
              <button type="button" class="absolute right-1.5 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded text-muted transition hover:text-[var(--ink)]"
                aria-label="Back to group list" title="Back to group list"
                onclick={() => { groupChoice = ''; nestQ = ''; }}>✕</button>
            </div>
          {:else if nestParent}
            <!-- Committed nest target: a chip in the select's slot. The › prefix is the app's
                 nesting glyph (displayName renders "Parent › Child"). ✕ returns to the select. -->
            <div class="flex min-w-0 items-center gap-2 rounded-lg border border-line bg-[var(--surface-2)] py-1 pl-1.5 pr-1"
              title="Nest inside {nestParent.name}">
              {#if nestParent.cover}
                <img src={nestParent.cover} alt="" class="h-7 w-10 shrink-0 rounded-sm object-cover" />
              {:else}
                <span class="h-7 w-10 shrink-0 rounded-sm bg-[var(--media-placeholder)]"></span>
              {/if}
              <span class="min-w-0 flex-1 leading-tight">
                <span class="block truncate text-xs font-semibold">› {nestParent.name}</span>
                <span class="block truncate text-[11px] text-muted">{nestParent.item_count ?? nestParent.ids?.length ?? 0} items</span>
              </span>
              <button type="button" class="grid h-6 w-6 shrink-0 place-items-center rounded text-muted transition hover:text-[var(--ink)]"
                aria-label="Clear nest target" title="Clear nest target"
                onclick={() => (groupChoice = '')}>✕</button>
            </div>
          {:else}
            <select class="min-w-0 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
              aria-label="Group" bind:value={groupChoice} onchange={onGroupSelect}>
              <option value="">No group</option>
              {#each existingGroups as group (group)}
                <option value={group}>{group}</option>
              {/each}
              <option value={NEW_GROUP}>New group…</option>
              {#if rootParents.length}
                <option value={NEST_PICK}>Nest inside…</option>
              {/if}
            </select>
          {/if}
          <Button class="text-sm" disabled={!name.trim() || !selected.length} onclick={create}>Create</Button>
        </div>
        {#if groupChoice === NEST_PICK}
          <!-- Filtered nest-target list, full width under the create row. Same row anatomy as
               Existing collections below (cover · name · count); the accent border tracks the
               keyboard highlight, and hovering a row moves it so the two never disagree. -->
          <div bind:this={nestListEl} class="mt-2 flex max-h-48 flex-col gap-1 overflow-auto">
            {#if !nestShown.length}
              <p class="py-4 text-center text-sm text-muted">No collections match.</p>
            {:else}
              {#each nestShown as c, i (c.id)}
                <button type="button"
                  class="flex items-center gap-2 rounded-lg border p-2 text-left transition {i === nestIdx ? 'border-[var(--accent)]' : 'border-line'}"
                  onclick={() => pickParent(c)} onmouseenter={() => (nestIdx = i)}>
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
        {/if}
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
