<script>
  // Groups the items of an open collection by the base image each video was generated
  // from (the downloader/Imagine set parent_id = the source image's id), so a
  // collection of loose clips resolves into the families they belong to — each ready
  // to merge-export or turn into a montage in a click. A family's base still is shown
  // as its header even when only its videos were collected: lineage that isn't in the
  // collection is resolved by fetching those rows (mediaByIds), which also lets the
  // grouping climb across an edit's uncollected intermediate to the true base image.
  import { mediaByIds } from '$lib/api.js';
  import JustifiedGrid from './JustifiedGrid.svelte';
  import EditorialList from './EditorialList.svelte';

  let {
    items = [],
    mode = 'cinematic',
    targetHeight = 240,
    gap = 10,
    selectMode = false,
    loaded = 0,   // collection items loaded so far (may trail `total` mid-load)
    total = 0,    // the collection's true item count
    onopen = () => {},
    ontoggleselect = () => {},
    onexport = () => {},
    onmontage = () => {},
    onplay = () => {}
  } = $props();

  const MAX_DEPTH = 12; // lineage-walk guard against a pathological parent chain

  // Grouping/actions must see the WHOLE collection, not just the first page — until it
  // is fully loaded the family counts and Export/Montage scope would be truncated. The
  // page eagerly drains the remaining pages while grouping; we pause actions until then.
  const complete = $derived(total <= 0 || loaded >= total);

  const byId = $derived.by(() => {
    const m = new Map();
    for (const it of items) m.set(String(it.id), it);
    return m;
  });

  // Lineage rows fetched for ancestors that aren't in the collection (an uncollected
  // intermediate edit, or a base still you didn't collect). Merged with the collected
  // items so the climb in anchorOf can cross them. `attempted` (plain, non-reactive)
  // dedupes fetches; a prompt-key fallback parent_id simply resolves to nothing.
  let resolved = $state(new Map());
  const attempted = new Set();
  const lineage = $derived.by(() => {
    const m = new Map(byId);
    for (const [k, v] of resolved) if (!m.has(k)) m.set(k, v);
    return m;
  });

  // The base an item belongs to. An image anchors itself — it IS a base, and its videos
  // point at its id. A video climbs parent_id (through collected OR fetched ancestors)
  // until it reaches an image (→ that image's id), a parent not yet resolved (→ that id,
  // pending a fetch), or runs out of lineage (→ itself). Cycle- and depth-guarded.
  function anchorOf(it) {
    if (!it) return '';
    if (it.media_type === 'image') return String(it.id);
    let cur = it;
    const seen = new Set([String(it.id)]);
    for (let i = 0; i < MAX_DEPTH; i++) {
      const pid = cur.parent_id ? String(cur.parent_id) : '';
      if (!pid) return String(cur.id);
      if (seen.has(pid)) return pid; // cycle
      seen.add(pid);
      const parent = lineage.get(pid);
      if (!parent) return pid; // unresolved (yet) — anchor here for now
      if (parent.media_type === 'image') return String(parent.id);
      cur = parent; // climb an intermediate edit (collected or fetched)
    }
    return String(cur.id);
  }

  // Resolve uncollected ancestors so anchorOf can climb to the true base. Cascades: a
  // fetched video's own parent gets fetched next, until images / dead ends / dupes stop
  // it (attempted grows monotonically, so it always terminates). On a network failure
  // the batch is un-marked so it can retry; a definitively-absent id stays marked.
  $effect(() => {
    const want = new Set();
    const consider = (node) => {
      const pid = node?.parent_id ? String(node.parent_id) : '';
      if (!pid || lineage.has(pid) || attempted.has(pid)) return;
      want.add(pid);
    };
    for (const it of items) if (it.media_type === 'video') consider(it);
    for (const [, v] of resolved) if (v?.media_type === 'video') consider(v);
    if (!want.size) return;
    const batch = [...want];
    for (const a of batch) attempted.add(a);
    mediaByIds(batch)
      .then((rows) => {
        if (!rows?.length) return;
        const next = new Map(resolved);
        for (const r of rows) next.set(String(r.id), r);
        resolved = next;
      })
      .catch(() => {
        for (const a of batch) attempted.delete(a); // transient failure — allow retry
      });
  });

  // anchor -> { anchor, items[] }, preserving the collection's order both within and
  // across groups (first appearance wins).
  const rawGroups = $derived.by(() => {
    const groups = new Map();
    for (const it of items) {
      const a = anchorOf(it);
      let g = groups.get(a);
      if (!g) { g = { anchor: a, items: [] }; groups.set(a, g); }
      g.items.push(it);
    }
    return groups;
  });

  // Families = 2+ videos sharing a base; these get an actionable section, biggest first.
  // Everything else (singletons, lone stills, a single video + its base) flows into one
  // Ungrouped grid so nothing is ever hidden. A family's base is its anchor item if
  // collected, else the fetched ancestor, else null (just a cluster of related videos).
  const grouped = $derived.by(() => {
    const families = [];
    const ungrouped = [];
    for (const [anchor, g] of rawGroups) {
      const videos = g.items.filter((x) => x.media_type === 'video');
      if (videos.length < 2) { ungrouped.push(...g.items); continue; }
      const base = byId.get(anchor) || resolved.get(anchor) || null;
      const external = !!base && !byId.has(anchor);
      families.push({
        anchor,
        items: g.items,
        videos,
        base,
        external,
        montageIds: videos.filter((v) => v.model !== 'Beat Montage').map((v) => v.id),
        exportIds: videos.map((v) => v.id),
        label: (base?.prompt || g.items.find((x) => x.prompt)?.prompt || 'Related videos').trim(),
        cover: base?.thumb || videos[0]?.thumb || g.items[0]?.thumb || ''
      });
    }
    families.sort((a, b) => b.videos.length - a.videos.length || a.label.localeCompare(b.label));
    return { families, ungrouped };
  });

  // Merging is order-sensitive; the parent opens a reorder step so the family's clips
  // (which land here in the collection's sort order, newest-first) can be arranged before
  // the concat instead of merging back-to-front.
  function exportFamily(fam) {
    if (!fam.exportIds.length) return;
    onexport(fam.videos, fam.label || 'group');
  }

  function baseNoun(fam) {
    return fam.base?.media_type === 'video' ? 'base clip' : 'base image';
  }

  // Open the base, then swipe straight through its clips; with no resolved base, open
  // the family in place.
  function openFamily(fam) {
    if (fam.base) onopen(fam.base, [fam.base, ...fam.videos]);
    else onopen(fam.items[0], fam.items);
  }
  // NOTE: each family renders its own JustifiedGrid, so mouse drag-paint selection is
  // scoped to one section (tap / selection-circle / "Select visible" in the bar still
  // span the whole collection). Crossing a family boundary mid-drag is an accepted
  // limitation of the grouped view.
</script>

<div class="space-y-5">
  {#if !complete}
    <p class="rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-xs text-muted">
      Loading the full collection to group it… {loaded.toLocaleString()} of {total.toLocaleString()}. Actions are paused until every clip is in.
    </p>
  {/if}

  {#each grouped.families as fam (fam.anchor)}
    <section class="overflow-hidden rounded-card border border-line bg-[var(--surface-2)]/40">
      <header class="flex flex-wrap items-center gap-3 border-b border-line p-3">
        <button type="button" class="relative h-14 w-14 shrink-0 overflow-hidden rounded-lg bg-[var(--media-bg)]"
          title={fam.base ? `Open ${baseNoun(fam)}` : 'Open clips'} aria-label="Open family" onclick={() => openFamily(fam)}>
          {#if fam.cover}
            <img src={fam.cover} alt="" loading="lazy" class="h-full w-full object-cover" />
          {/if}
          {#if fam.external}
            <span class="absolute inset-x-0 bottom-0 bg-[var(--media-scrim)] px-1 py-px text-center text-[9px] font-bold uppercase tracking-wide text-[var(--media-control-ink)]">source</span>
          {/if}
        </button>
        <div class="min-w-0 flex-1">
          <p class="truncate text-sm font-bold text-ink" title={fam.label}>{fam.label}</p>
          <p class="text-xs text-muted">
            {fam.videos.length} video{fam.videos.length === 1 ? '' : 's'}{fam.base ? (fam.external ? ` · ${baseNoun(fam)} not in collection` : ` · ${baseNoun(fam)}`) : ''}
          </p>
        </div>
        <!-- w-full below sm: the nowrap Play/Export/Montage strip is wider than a phone
             leaves next to the label, and the label (min-w-0 flex-1) loses that fight —
             it crushes to a sliver and the meta wraps word-by-word into the buttons.
             Forcing the cluster onto its own row gives the label the full first line. -->
        <div class="flex w-full flex-wrap items-center gap-1.5 sm:w-auto">
          <button type="button" class="group-action" disabled={!complete || !fam.videos.length} onclick={() => onplay(fam.videos, fam.label)}>
            <span class="text-[0.7rem]">▶</span> Play
          </button>
          <button type="button" class="group-action" disabled={!complete || !fam.exportIds.length} onclick={() => exportFamily(fam)}>
            <span class="text-[0.7rem]">⇩</span> Export merged
          </button>
          <button type="button" class="group-action group-action-primary" disabled={!complete || fam.montageIds.length < 2} onclick={() => onmontage(fam.montageIds)}
            title={fam.montageIds.length < 2 ? 'Needs at least 2 montage-eligible videos' : 'Beat-synced montage from this family'}>
            <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
            Montage
          </button>
        </div>
      </header>
      <div class="p-3">
        {#if mode === 'editorial'}
          <EditorialList items={fam.items} {onopen} />
        {:else}
          <JustifiedGrid items={fam.items} {targetHeight} {gap} {selectMode} {onopen} {ontoggleselect} />
        {/if}
      </div>
    </section>
  {/each}

  {#if complete && !grouped.families.length && grouped.ungrouped.length}
    <p class="rounded-lg border border-dashed border-line px-3 py-2 text-xs text-muted">
      No related families — every collected item has a unique base image (or fewer than two videos share one).
    </p>
  {/if}

  {#if grouped.ungrouped.length}
    <section>
      <header class="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-1">
        <h3 class="text-sm font-bold text-ink">Ungrouped</h3>
        <span class="text-xs text-muted">{grouped.ungrouped.length.toLocaleString()} item{grouped.ungrouped.length === 1 ? '' : 's'} with no shared base</span>
      </header>
      {#if mode === 'editorial'}
        <EditorialList items={grouped.ungrouped} {onopen} />
      {:else}
        <JustifiedGrid items={grouped.ungrouped} {targetHeight} {gap} {selectMode} {onopen} {ontoggleselect} />
      {/if}
    </section>
  {/if}
</div>

<style>
  .group-action {
    align-items: center;
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    color: var(--ink);
    display: inline-flex;
    font-size: 0.75rem;
    font-weight: 700;
    gap: 0.3rem;
    line-height: 1;
    padding: 0.45rem 0.65rem;
    transition: border-color 160ms ease, background 160ms ease, color 160ms ease, opacity 160ms ease;
    white-space: nowrap;
  }

  .group-action:hover:not(:disabled) {
    border-color: var(--accent);
  }

  .group-action:disabled {
    cursor: default;
    opacity: 0.42;
  }

  .group-action-primary {
    background: var(--accent);
    border-color: transparent;
    color: var(--on-accent);
  }

  .group-action-primary:hover:not(:disabled) {
    border-color: transparent;
    filter: brightness(1.06);
  }
</style>
