<script module>
  // Expand + filter state lives at MODULE scope for the same reason the grid's toolbar
  // state does (see CollectionsGrid): opening a collection unmounts the Library, and an
  // outline that re-collapsed itself on every Back would be useless as a map.
  let expanded = $state(new Set()); // 'group:<key>' | 'coll:<id>'
  let subsOnly = $state(false);     // prune to branches that actually hold sub-collections
</script>

<script>
  // Outline view: the whole Library hierarchy — Group → Collection → Sub-collection — on
  // one screen. The card grid only ever shows tier one (group cards + ungrouped roots), so
  // a sub-collection was invisible until you guessed which parent held it. Everything here
  // renders from collections the store already has (children included, with covers and
  // counts), so the view costs no extra request.
  let {
    groups = [],       // groupEntries from the grid (each carries .members)
    ungrouped = [],    // root collections that aren't in a group
    collections = [],  // every collection, children included — the parent→child lookup
    q = '',
    sortBy = 'updated',
    showLocked = false,
    onopengroup = () => {},
    onopen = () => {},
    onplay = () => {},
    onunlock = () => {}
  } = $props();

  const isSealed = (c) => c.locked && !c.unlocked;
  const countOf = (c) => c.item_count ?? c.ids?.length ?? 0;
  const visible = (c) => showLocked || !isSealed(c);

  const childrenOf = $derived.by(() => {
    const m = new Map();
    collections.forEach((c, index) => {
      if (!c.parent_id) return;
      if (!m.has(c.parent_id)) m.set(c.parent_id, []);
      m.get(c.parent_id).push({ ...c, store_index: index });
    });
    return m;
  });

  // Same ordering vocabulary as the card grid, applied within each tier.
  function sortNodes(list) {
    const arr = [...list];
    if (sortBy === 'name') arr.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    else if (sortBy === 'size') arr.sort((a, b) => countOf(b) - countOf(a));
    else if (sortBy === 'updated') arr.sort((a, b) => (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || ''));
    else arr.sort((a, b) => (a.store_index ?? 0) - (b.store_index ?? 0));
    return arr;
  }

  const needle = $derived(q.trim().toLowerCase());
  const hit = (name) => !needle || String(name || '').toLowerCase().includes(needle);

  // A branch survives the search if it matches or if anything under it does — so typing a
  // sub-collection's name keeps its parent (and that parent's group) as context, while a
  // matching ancestor shows its whole subtree.
  function buildCollection(coll, ancestorMatched) {
    const kids = (childrenOf.get(coll.id) || []).filter(visible);
    if (subsOnly && !kids.length) return null;
    const selfMatch = ancestorMatched || hit(coll.name);
    const shownKids = sortNodes(selfMatch ? kids : kids.filter((k) => hit(k.name)));
    if (!selfMatch && !shownKids.length) return null;
    return { ...coll, key: `coll:${coll.id}`, kind: 'collection', kids: shownKids, subCount: kids.length };
  }

  const tree = $derived.by(() => {
    const nodes = [];
    for (const g of groups) {
      if (!visible(g)) continue;
      const gMatch = hit(g.name);
      // A sealed group's members are redacted placeholders — the branch stays a leaf.
      const members = isSealed(g)
        ? []
        : sortNodes((g.members || []).filter(visible).map((m) => buildCollection(m, gMatch)).filter(Boolean));
      if (subsOnly && !members.length) continue;
      if (!gMatch && !members.length) continue;
      nodes.push({
        ...g, key: `group:${g.group_key}`, kind: 'group', kids: members,
        // The group's REAL total (from the grid), not a sum over the members a search
        // happens to have left standing — a badge that shrank as you typed would be
        // reporting the filter, not the library.
        subCount: g.sub_count ?? members.reduce((n, m) => n + m.subCount, 0)
      });
    }
    const roots = sortNodes(ungrouped.filter(visible).map((c) => buildCollection(c, false)).filter(Boolean));
    return { nodes: sortNodes(nodes), roots };
  });

  // Library totals for the summary line — the whole point of the view is "what do I have".
  const totals = $derived.by(() => ({
    groups: groups.filter(visible).length,
    roots: collections.filter((c) => !c.parent_id && visible(c)).length,
    subs: collections.filter((c) => c.parent_id && visible(c)).length
  }));

  // Searching, or pruning to sub-collections, opens every surviving branch: in both cases
  // the leaf IS the answer, and re-clicking your way down to it defeats the view.
  const autoExpand = $derived(!!needle || subsOnly);
  const isOpen = (key) => autoExpand || expanded.has(key);
  function toggle(key) {
    const next = new Set(expanded);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    expanded = next;
  }
  const expandableKeys = $derived.by(() => {
    const keys = [];
    for (const g of tree.nodes) {
      if (g.kids.length) keys.push(g.key);
      for (const m of g.kids) if (m.kids.length) keys.push(m.key);
    }
    for (const r of tree.roots) if (r.kids.length) keys.push(r.key);
    return keys;
  });
  // "Collapse all" only once everything IS open. Flipping the button the moment a single
  // branch was opened by hand left no way to open the rest in one click.
  const allOpen = $derived(!!expandableKeys.length && expandableKeys.every((k) => expanded.has(k)));

  function activate(node) {
    if (isSealed(node)) onunlock(node);
    else if (node.kind === 'group') onopengroup(node.name);
    else onopen(node);
  }
</script>

<div class="mb-3 flex flex-wrap items-center gap-2">
  <p class="mr-auto text-sm text-muted">
    {totals.groups} group{totals.groups === 1 ? '' : 's'} · {totals.roots.toLocaleString()} collection{totals.roots === 1 ? '' : 's'} · {totals.subs.toLocaleString()} sub-collection{totals.subs === 1 ? '' : 's'}
  </p>
  <button type="button" onclick={() => (subsOnly = !subsOnly)} aria-pressed={subsOnly}
    title={subsOnly ? 'Show the whole library again' : 'Show only the branches that contain sub-collections'}
    class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-semibold transition {subsOnly ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)]' : 'border-line bg-[var(--surface-2)] hover:border-[var(--accent)]'}">
    <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-2.5a1 1 0 0 1-.8-.4l-.9-1.2A1 1 0 0 0 15 3h-2a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z"/><path d="M20 21a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1h-2.5a1 1 0 0 1-.8-.4l-.9-1.2a1 1 0 0 0-.8-.4h-2a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z"/><path d="M3 5a2 2 0 0 0 2 2h3"/><path d="M3 3v13a2 2 0 0 0 2 2h3"/></svg>
    Only with sub-collections
  </button>
  <button type="button" onclick={() => (expanded = allOpen ? new Set() : new Set(expandableKeys))}
    disabled={autoExpand || !expandableKeys.length}
    title={autoExpand ? 'Every matching branch is already open' : allOpen ? 'Collapse every branch' : 'Open every branch'}
    class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-1.5 text-sm font-semibold transition enabled:hover:border-[var(--accent)] disabled:opacity-40">
    {allOpen && !autoExpand ? 'Collapse all' : 'Expand all'}
  </button>
</div>

{#if !tree.nodes.length && !tree.roots.length}
  <p class="py-16 text-center text-sm text-muted">
    {needle ? `Nothing matches “${q.trim()}”.` : subsOnly ? 'No collection has sub-collections yet.' : 'Nothing to show here.'}
  </p>
{:else}
  <ul class="overflow-hidden rounded-card border border-line">
    {#each tree.nodes as g (g.key)}
      <li>
        {@render row(g, 0)}
        {#if isOpen(g.key)}
          {#each g.kids as m (m.key)}
            {@render row(m, 1)}
            {#if isOpen(m.key)}
              {#each m.kids as child (child.id)}
                {@render row({ ...child, key: `sub:${child.id}`, kind: 'sub', kids: [], subCount: 0 }, 2)}
              {/each}
            {/if}
          {/each}
        {/if}
      </li>
    {/each}
    {#each tree.roots as r (r.key)}
      <li>
        {@render row(r, 0)}
        {#if isOpen(r.key)}
          {#each r.kids as child (child.id)}
            {@render row({ ...child, key: `sub:${child.id}`, kind: 'sub', kids: [], subCount: 0 }, 1)}
          {/each}
        {/if}
      </li>
    {/each}
  </ul>
{/if}

{#snippet row(node, depth)}
  {@const sealed = isSealed(node)}
  {@const hasKids = node.kids.length > 0}
  <div class="group/row flex items-center gap-2 border-b border-line py-1.5 pr-2 transition-colors last:border-b-0 hover:bg-[var(--surface-2)] {node.kind === 'sub' ? 'bg-[var(--surface-2)]/30' : ''}"
    style="padding-left: {0.5 + depth * 1.5}rem">
    {#if hasKids}
      <button type="button" onclick={() => toggle(node.key)} disabled={autoExpand}
        aria-expanded={isOpen(node.key)} aria-label={isOpen(node.key) ? `Collapse ${node.name}` : `Expand ${node.name}`}
        class="grid h-6 w-6 shrink-0 place-items-center rounded-md text-muted transition hover:bg-[var(--surface-solid)] hover:text-ink disabled:opacity-40">
        <svg viewBox="0 0 24 24" class="h-4 w-4 transition-transform {isOpen(node.key) ? 'rotate-90' : ''}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
      </button>
    {:else}
      <span class="h-6 w-6 shrink-0" aria-hidden="true"></span>
    {/if}

    <!-- The rest of the row opens the node; the twisty above stays a separate target so
         expanding a branch never navigates away from the map. -->
    <button type="button" class="flex min-w-0 flex-1 items-center gap-2.5 py-0.5 text-left"
      aria-label={sealed ? `Unlock ${node.kind === 'group' ? 'group' : 'collection'} ${node.name}` : `Open ${node.name}`}
      onclick={() => activate(node)}>
      {#if node.kind === 'group'}
        <span class="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-line bg-[var(--surface-2)] text-muted">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
        </span>
      {:else if sealed}
        <span class="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-line bg-[var(--surface-2)] text-muted">
          <svg viewBox="0 0 24 24" class="h-4 w-4 opacity-80" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        </span>
      {:else if node.covers?.length > 1}
        <span class="grid h-9 w-9 shrink-0 grid-cols-2 grid-rows-2 gap-px overflow-hidden rounded-md bg-[var(--media-bg)]">
          {#each node.covers.slice(0, 4) as cover (cover)}
            <img src={cover} alt="" loading="lazy" class="h-full w-full object-cover object-top" />
          {/each}
        </span>
      {:else if node.cover}
        <img src={node.cover} alt="" loading="lazy" class="h-9 w-9 shrink-0 rounded-md object-cover object-top" />
      {:else}
        <span class="h-9 w-9 shrink-0 rounded-md border border-dashed border-line" aria-hidden="true"></span>
      {/if}

      <span class="min-w-0 flex-1">
        <span class="flex min-w-0 items-center gap-1.5">
          <span class="truncate font-bold {node.kind === 'sub' ? 'text-sm' : ''}">{node.name}</span>
          {#if node.subCount}
            <!-- The signal that was missing everywhere: this branch holds sub-collections. -->
            <span class="inline-flex shrink-0 items-center gap-1 rounded-full border border-[var(--accent)]/40 bg-[var(--accent)]/10 px-1.5 py-px text-[11px] font-bold tabular-nums text-[var(--accent)]"
              title="{node.subCount} sub-collection{node.subCount === 1 ? '' : 's'}">
              <svg viewBox="0 0 24 24" class="h-3 w-3" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
              {node.subCount}
            </span>
          {/if}
        </span>
        <span class="block truncate text-xs text-muted">
          {#if sealed}
            Locked{node.kind === 'group' ? ` · ${node.collection_count ?? 0} collections` : ''}
          {:else if node.kind === 'group'}
            {node.collection_count ?? node.kids.length} collection{(node.collection_count ?? node.kids.length) === 1 ? '' : 's'} · {countOf(node).toLocaleString()} items
          {:else}
            {countOf(node).toLocaleString()} item{countOf(node) === 1 ? '' : 's'}{node.video_count ? ` · ${node.video_count} video${node.video_count === 1 ? '' : 's'}` : ''}
          {/if}
        </span>
      </span>
    </button>

    {#if !sealed && node.kind !== 'group' && node.video_count}
      <button type="button" onclick={() => onplay(node)}
        title="Play this collection's videos" aria-label="Play {node.name}"
        class="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-line text-muted opacity-0 transition hover:border-[var(--accent)] hover:text-[var(--accent)] group-hover/row:opacity-100 group-focus-within/row:opacity-100 pointer-coarse:opacity-100">
        <span aria-hidden="true" class="text-xs">▶</span>
      </button>
    {/if}
  </div>
{/snippet}
