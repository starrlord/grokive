<script module>
  // Toolbar state (filter, sort, locked-reveal) lives at MODULE scope, not instance
  // scope: drilling into a collection swaps this grid out of the DOM (+page.svelte's
  // {#if} on activeCollection), so instance state would reset on Back — the sort DDL
  // snapped to "Recent", reordering the landing under the restored scroll offset and
  // dumping you somewhere unrelated. Module scope survives the round-trip; it still
  // resets to defaults on a full page load (client-only SPA — no SSR sharing).
  let q = $state('');
  // Default is 'updated' (last-modified first): the collection you just added clips to
  // lands on top, which is what you almost always want back. 'recent' (stored order) is
  // still selectable and keeps Beat Montage pinned.
  let sortBy = $state('updated'); // updated (last-modified) | recent (store/creation order) | name | size
  let showLocked = $state(false);
  let activeGroup = $state('');
  let landingScrollY = 0;
</script>

<script>
  import { tick } from 'svelte';
  import { collections, collectionGroups, removeCollection, updateCollection, loadCollections, requestGalleryReload } from '$lib/state.js';
  import { relockCollection, relockAllCollections, relockGroup } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import SearchField from './SearchField.svelte';
  import CollectionLockModal from './CollectionLockModal.svelte';
  import PeekOverlay from './PeekOverlay.svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';

  let { onopen = () => {}, onplay = () => {}, onqueue = () => {}, onplayqueue = () => {}, onimport = () => {} } = $props();
  let confirming = $state(null);
  let fileInput = $state(null);
  let lockModal = $state(null); // { collection, mode } -> CollectionLockModal
  function onPick(e) {
    const picked = e.currentTarget.files;
    if (picked?.length) onimport(picked);
    e.currentTarget.value = ''; // allow re-picking the same folder later
  }
  const anyUnlocked = $derived(
    ($collections || []).some((c) => c.locked && c.unlocked) ||
    ($collectionGroups || []).some((g) => g.locked && g.unlocked)
  );
  // Lock-state changes shift what's visible in All Media / facets, so refresh the gallery too.
  async function relockNow(c) {
    try { await (c.is_group ? relockGroup(c.name) : relockCollection(c.id)); }
    finally { loadCollections(); requestGalleryReload(); }
  }
  async function lockAll() { try { await relockAllCollections(); } finally { loadCollections(); requestGalleryReload(); } }
  const unlockHoursLeft = (c) => {
    const secs = (c.unlock_expires || 0) - Date.now() / 1000;
    return secs > 0 ? Math.max(1, Math.ceil(secs / 3600)) : 0;
  };

  // Toolbar state (q, sortBy, showLocked) is declared in <script module> above so it
  // survives the landing being unmounted while a collection is drilled into.
  // Locked (and not unlocked this session) collections are hidden from the grid so they
  // don't clutter it — revealed only when you toggle them on. In-memory: resets to hidden
  // on every page load, so they stay out of sight by default. Unlocked ones always show.
  const isSealed = (c) => c.locked && !c.unlocked;

  // The auto-generated "Beat Montage" collection — pinned to the top of the default
  // (recent) order wherever it sits in the stored list. Same id/name check the server uses.
  const isMontage = (c) => c.id === 'beat-montage' || c.name?.toLowerCase() === 'beat montage';

  const groupKey = (name) => String(name || '').trim().toLowerCase();
  const mediaCount = (c) => c.item_count ?? c.ids?.length ?? 0;
  const collectionsList = $derived($collections || []);
  const collectionGroupList = $derived($collectionGroups || []);
  // A parent's "last updated" is the newest of itself and its nested sub-collections —
  // the landing shows only roots, so without this roll-up adding clips to a sub-folder
  // never moved its parent under "Recently updated" (the mutators also stamp the parent
  // now; this covers stamps written before that and any other writer). Same idea as
  // the group roll-up below. Children keep their own stamps.
  const rolledList = $derived.by(() => {
    const newestChild = new Map();
    for (const c of collectionsList) {
      if (!c.parent_id) continue;
      const stamp = c.updated_at || c.created_at || '';
      if (stamp > (newestChild.get(c.parent_id) || '')) newestChild.set(c.parent_id, stamp);
    }
    if (!newestChild.size) return collectionsList;
    return collectionsList.map((c) => {
      const child = newestChild.get(c.id);
      return child && child > (c.updated_at || c.created_at || '') ? { ...c, updated_at: child } : c;
    });
  });
  const groupEntries = $derived.by(() => {
    const collections = rolledList;
    const serverGroups = collectionGroupList;
    const byKey = new Map();
    const groupNames = new Map();
    collections.forEach((c, index) => {
      const group = String(c.group || '').trim();
      if (!group) return;
      const key = groupKey(group);
      if (!byKey.has(key)) byKey.set(key, { name: group, members: [], firstIndex: index });
      byKey.get(key).members.push({ ...c, store_index: index });
      groupNames.set(key, group);
    });
    serverGroups.forEach((g) => {
      const name = String(g.name || '').trim();
      if (!name) return;
      const key = groupKey(name);
      if (!byKey.has(key)) byKey.set(key, { name, members: [], firstIndex: collections.length });
      byKey.get(key).server = g;
      groupNames.set(key, name);
    });
    const entries = [];
    for (const [key, bucket] of byKey) {
      const members = bucket.members || [];
      const server = bucket.server || {};
      const covers = [];
      const coverItems = [];
      for (const member of members) {
        for (const cover of (member.covers || []).slice(0, 4)) {
          if (covers.length < 4) covers.push(cover);
        }
        for (const item of (member.cover_items || []).slice(0, 4)) {
          if (coverItems.length < 4) coverItems.push(item);
        }
        if (!covers.length && member.cover) covers.push(member.cover);
        if (!coverItems.length && member.cover_peek) coverItems.push(member.cover_peek);
      }
      const itemCount = members.reduce((sum, c) => sum + mediaCount(c), 0);
      entries.push({
        id: `group:${key}`,
        is_group: true,
        name: groupNames.get(key) || bucket.name,
        members,
        group_key: key,
        collection_count: server.collection_count ?? members.length,
        item_count: itemCount,
        video_count: members.reduce((sum, c) => sum + (c.video_count ?? 0), 0),
        image_count: members.reduce((sum, c) => sum + (c.image_count ?? 0), 0),
        covers,
        cover_items: coverItems,
        cover: covers[0] || null,
        cover_peek: coverItems[0] || null,
        locked: !!server.locked,
        unlocked: !!server.unlocked,
        unlock_expires: server.unlock_expires,
        updated_at: members.map((c) => c.updated_at || c.created_at || '').sort().at(-1) || '',
        created_at: members.map((c) => c.created_at || '').sort()[0] || '',
        store_index: bucket.firstIndex,
      });
    }
    return entries;
  });
  const topEntries = $derived.by(() => {
    const grouped = new Set(groupEntries.map((g) => g.group_key));
    // Nested collections live inside their parent (the drilled-in view's shelf) — the
    // landing shows only roots so sub-folders don't clutter or duplicate the grid.
    const ungrouped = rolledList
      .map((c, index) => ({ ...c, store_index: index }))
      .filter((c) => !c.parent_id && !grouped.has(groupKey(c.group)));
    return [...ungrouped, ...groupEntries];
  });
  const activeMembers = $derived.by(() => {
    const key = groupKey(activeGroup);
    return rolledList
      .map((c, index) => ({ ...c, store_index: index }))
      .filter((c) => groupKey(c.group) === key);
  });
  const baseEntries = $derived(activeGroup ? activeMembers : topEntries);
  const lockedHiddenCount = $derived(baseEntries.filter(isSealed).length);
  const visibleTotal = $derived(baseEntries.filter((c) => showLocked || !isSealed(c)).length);

  const filtered = $derived(
    baseEntries.filter((c) =>
      (showLocked || !isSealed(c)) &&
      (!q.trim() || (c.name || '').toLowerCase().includes(q.trim().toLowerCase())))
  );
  const shown = $derived.by(() => {
    const list = [...filtered];
    if (sortBy === 'name') list.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    else if (sortBy === 'size') list.sort((a, b) => mediaCount(b) - mediaCount(a));
    // 'updated' = last-modified first (updated_at bumps when items are added/removed or
    // the collection is renamed) — so a collection you just added clips to floats up,
    // unlike 'recent' which only reflects creation order. Falls back to created_at.
    else if (sortBy === 'updated') list.sort((a, b) => ((b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || '')));
    // 'recent' = stored order, with Beat Montage pinned on top (sort is stable, so the rest stay put).
    else list.sort((a, b) => activeGroup ? (a.store_index ?? 0) - (b.store_index ?? 0) : ((isMontage(b) ? 1 : 0) - (isMontage(a) ? 1 : 0)));
    // Finally pin only SEALED (still-locked) collections to the top — and, since sealed
    // ones are hidden until "Show locked", this only takes effect once you reveal them,
    // grouping them at the front as a batch you can unlock. The sort is stable, so their
    // relative order (and the rest) is preserved. Once a collection is UNLOCKED this
    // session it is no longer sealed, so it drops back into the chosen order like any
    // other collection — meaning Recently Updated / Name / Largest actually affect it.
    return list.sort((a, b) => (isSealed(b) ? 1 : 0) - (isSealed(a) ? 1 : 0));
  });

  // --- Hero band: on wide screens the first row of "Recently updated" renders as three
  // oversized feature cards. Only in the default browse state (updated sort, no search,
  // not inside a group) so card position stays meaningful under every other sort, and
  // only when the landing is big enough that featuring doesn't cannibalize the grid.
  // Sealed cards never feature (a vault has nothing to show at hero size).
  let wideScreen = $state(typeof window !== 'undefined' && window.matchMedia('(min-width: 1024px)').matches);
  $effect(() => {
    const mq = window.matchMedia('(min-width: 1024px)');
    const onchange = () => (wideScreen = mq.matches);
    mq.addEventListener('change', onchange);
    return () => mq.removeEventListener('change', onchange);
  });
  const heroEntries = $derived.by(() => {
    if (!wideScreen || activeGroup || sortBy !== 'updated' || q.trim() || shown.length < 8) return [];
    return shown.filter((c) => !isSealed(c)).slice(0, 3);
  });
  const gridEntries = $derived.by(() => {
    if (!heroEntries.length) return shown;
    const heroIds = new Set(heroEntries.map((c) => c.id));
    return shown.filter((c) => !heroIds.has(c.id));
  });

  // --- Living covers: hover-dwell makes ONE card at a time come alive — a muted looping
  // clip when a cover item is a video, else a slow Ken Burns drift across the mosaic.
  // Mouse-only (touch already has long-press peek) and skipped under reduced motion.
  // The <video> element exists only while its card is live, so an idle landing costs
  // nothing; sealed cards ship no cover_items, so they can never go live.
  const reduceMotion = typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  let live = $state(null); // entry id currently alive
  let liveTimer = null;
  const liveVideoFor = (c) => (c.cover_items || []).find((it) => it?.media_type === 'video' && it.href) || null;
  function liveEnter(c, e) {
    if (reduceMotion || e.pointerType !== 'mouse') return;
    if (isSealed(c) || !(c.cover_items || []).length) return;
    clearTimeout(liveTimer);
    liveTimer = setTimeout(() => (live = c.id), 400);
  }
  function liveLeave() {
    clearTimeout(liveTimer);
    liveTimer = null;
    live = null;
  }

  // --- Drag a collection card onto another to group them (desktop HTML5 DnD; touch has
  // no drag path — grouping stays available via the picker/group field). Onto a group
  // card: join that group. Onto a grouped collection: join ITS group. Onto an ungrouped
  // collection: name a brand-new group holding both. Sealed cards neither drag nor
  // accept drops, and group cards themselves don't drag (group-merge is out of scope).
  let dragging = $state(null); // id of the card being dragged
  let dropTarget = $state(null); // id of the card currently hovered as a drop target
  let groupPrompt = $state(null); // { source, target, name } -> new-group naming modal
  const canDrop = (c) => !!dragging && c.id !== dragging && !isSealed(c);
  function dragStart(c, e) {
    // The pointer is dragging, not holding — disarm a pending long-press peek.
    if (peekTimer != null) { clearTimeout(peekTimer); peekTimer = null; }
    liveLeave();
    dragging = c.id;
    e.dataTransfer.setData('text/plain', c.id); // Firefox: no data = inert drag
    e.dataTransfer.effectAllowed = 'move';
  }
  function dragEnd() { dragging = null; dropTarget = null; }
  function dragOver(c, e) {
    if (!canDrop(c)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    dropTarget = c.id;
  }
  function dragLeave(c, e) {
    // dragleave fires for every child boundary — only clear when truly leaving the card.
    if (dropTarget === c.id && !e.currentTarget.contains(e.relatedTarget)) dropTarget = null;
  }
  function dropOn(target, e) {
    e.preventDefault();
    const source = collectionsList.find((c) => c.id === dragging);
    dragEnd();
    if (!source || source.id === target.id) return;
    const joinName = target.is_group ? target.name : String(target.group || '').trim();
    if (joinName) {
      updateCollection(source.id, { group: joinName });
      toast(`Added “${source.name}” to “${joinName}”`, { type: 'success' });
    } else {
      groupPrompt = { source, target, name: '' };
    }
  }
  function confirmGroupPrompt() {
    const name = String(groupPrompt?.name || '').trim().slice(0, 60);
    if (!name) return;
    const { source, target } = groupPrompt;
    groupPrompt = null;
    updateCollection(target.id, { group: name });
    updateCollection(source.id, { group: name });
    toast(`Grouped “${source.name}” and “${target.name}” as “${name}”`, { type: 'success' });
  }
  // Modal's trapFocus focuses the PANEL one rAF after mount (deliberately not the first
  // input — iOS keyboard pop). This dialog exists solely to type a name, so steal focus
  // back one frame LATER; two rAFs land after the trap's one.
  const autofocus = (el) => {
    let r2 = 0;
    const r1 = requestAnimationFrame(() => { r2 = requestAnimationFrame(() => el.focus()); });
    return { destroy: () => { cancelAnimationFrame(r1); cancelAnimationFrame(r2); } };
  };

  // --- Long-press peek: hold a cover to preview the full media, release to dismiss ---
  // Armed on the full-bleed open button (it overlays the whole cover). A quick tap
  // still opens the collection; a >10px drift means scroll, which disarms. Sealed
  // collections ship no cover_items/cover_peek, so nothing can leak from them.
  let peek = $state(null); // {thumb, href, media_type} while held
  let peekTimer = null, peekX = 0, peekY = 0, suppressOpen = false;

  function peekTargetFor(c, e) {
    const items = c.cover_items || [];
    if (c.covers?.length > 1 && items.length) {
      // Mosaic: map the press position to its 2×2 quadrant (grid-cols-2 order).
      const r = e.currentTarget.getBoundingClientRect();
      const idx = (e.clientY - r.top > r.height / 2 ? 2 : 0) + (e.clientX - r.left > r.width / 2 ? 1 : 0);
      return items[Math.min(idx, items.length - 1)];
    }
    return c.cover_peek || items[0] || null;
  }
  function peekDown(c, e) {
    suppressOpen = false; // clear a stale flag from a press that ended in pointercancel
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const target = peekTargetFor(c, e); // resolved now — currentTarget is gone by timer time
    if (!target?.href) return;
    peekX = e.clientX; peekY = e.clientY;
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch {}
    clearTimeout(peekTimer);
    // 400ms: fires ahead of Android's ~500ms native context menu so ours wins.
    peekTimer = setTimeout(() => {
      peekTimer = null;
      suppressOpen = true; // the click on release must not open the collection
      peek = target;
      navigator.vibrate?.(15);
    }, 400);
  }
  function peekMove(e) {
    // A moving pointer means scroll/drag, not a long-press — disarm.
    if (peekTimer != null && (Math.abs(e.clientX - peekX) > 10 || Math.abs(e.clientY - peekY) > 10)) {
      clearTimeout(peekTimer); peekTimer = null;
    }
  }
  function peekEnd() {
    if (peekTimer != null) { clearTimeout(peekTimer); peekTimer = null; }
    peek = null;
  }
  function openClick(c, sealed) {
    if (suppressOpen) { suppressOpen = false; return; }
    if (sealed) lockModal = c.is_group ? { group: c, mode: 'unlock' } : { collection: c, mode: 'unlock' };
    else if (c.is_group) openGroup(c.name);
    else onopen(c);
  }
  async function openGroup(name) {
    landingScrollY = window.scrollY || 0;
    activeGroup = name;
    q = '';
    await tick();
    window.scrollTo({ top: 0 });
  }
  async function closeGroup() {
    activeGroup = '';
    q = '';
    await tick();
    window.scrollTo({ top: landingScrollY });
  }

  // Cover srcset: the 400px grid thumb for small slots, the server's lazily generated
  // /covers/<id>.jpg high-res tier once a card renders large (desktop). `it` is a
  // cover_items entry (which mirrors covers, same items and order — see server summary).
  const coverSrcset = (thumb, it) => (it?.id ? `${thumb} 400w, /covers/${it.id}.jpg 1280w` : undefined);
  // Mosaic quadrants are half the card's width; hero cards sit 3-across on wide screens.
  const quadSizes = (hero) => (hero ? '16vw' : '(min-width: 1280px) 12vw, (min-width: 1024px) 16vw, (min-width: 640px) 25vw, 50vw');
  const fullSizes = (hero) => (hero ? '33vw' : '(min-width: 1280px) 25vw, (min-width: 1024px) 33vw, (min-width: 640px) 50vw, 100vw');

  // Count line for the cover, dropping any zero segments (e.g. "26 items · 26 videos").
  const countLabel = (c) => {
    if (c.is_group) {
      const count = c.collection_count ?? c.members?.length ?? 0;
      const total = c.item_count ?? 0;
      return `${count} collection${count === 1 ? '' : 's'} · ${total} item${total === 1 ? '' : 's'}`;
    }
    const total = c.item_count ?? c.ids?.length ?? 0;
    const videos = c.video_count ?? 0;
    const images = c.image_count ?? 0;
    const parts = [`${total} item${total === 1 ? '' : 's'}`];
    if (videos) parts.push(`${videos} video${videos === 1 ? '' : 's'}`);
    if (images) parts.push(`${images} image${images === 1 ? '' : 's'}`);
    return parts.join(' · ');
  };
</script>

<input bind:this={fileInput} type="file" webkitdirectory multiple class="hidden" onchange={onPick} aria-hidden="true" tabindex="-1" />

{#if !topEntries.length}
  <div class="grid place-items-center rounded-card border border-dashed border-line py-24 text-center text-muted">
    <div>
      <p class="mb-1 text-lg font-bold text-ink">No collections yet</p>
      <p class="mb-4 text-sm">Select media from Recent or All Media, then add it to a collection — or import a folder from your device.</p>
      <button type="button" onclick={() => fileInput?.click()}
        class="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-bold text-[var(--on-accent)]">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4 4 4-4"/><path d="M4 14.9A6 6 0 0 1 7 4a5 5 0 0 1 9 1 4 4 0 0 1 2 7.7"/></svg>
        Import a folder
      </button>
    </div>
  </div>
{:else}
  <!-- Toolbar: title + count, name filter, ordering. -->
  <div class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
    {#if activeGroup}
      <button type="button" class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]" onclick={closeGroup}>
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
        Back
      </button>
      <span class="min-w-0 truncate text-base font-extrabold">{activeGroup}</span>
    {/if}
    <span class="text-sm text-muted">{visibleTotal} collection{visibleTotal === 1 ? '' : 's'}</span>
    <div class="ml-auto flex w-full flex-wrap items-center gap-2 sm:w-auto sm:flex-nowrap">
      {#if lockedHiddenCount}
        <button type="button" onclick={() => (showLocked = !showLocked)} aria-pressed={showLocked}
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-semibold transition {showLocked ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)]' : 'border-line bg-[var(--surface-2)] hover:border-[var(--accent)]'}"
          title={showLocked ? 'Hide locked collections' : 'Show locked collections'}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          {showLocked ? 'Hide' : 'Show'} locked ({lockedHiddenCount})
        </button>
        <button type="button" onclick={() => (lockModal = { collection: null, mode: 'unlock-all' })}
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]"
          title="Unlock every collection that shares one password">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
          Unlock all
        </button>
      {/if}
      {#if anyUnlocked}
        <button type="button" onclick={lockAll}
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--accent)] bg-[var(--accent)]/10 px-3 py-1.5 text-sm font-semibold text-[var(--accent)] transition hover:bg-[var(--accent)]/20"
          title="Re-lock all currently unlocked collections now">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          Lock all
        </button>
      {/if}
      <button type="button" onclick={() => fileInput?.click()}
        class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]"
        title="Import a folder of videos/images into a new collection">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4 4 4-4"/><path d="M4 14.9A6 6 0 0 1 7 4a5 5 0 0 1 9 1 4 4 0 0 1 2 7.7"/></svg>
        Import
      </button>
      <SearchField bind:value={q} placeholder="Search collections…" ariaLabel="collection search"
        wrapperClass="order-last w-full min-w-0 sm:order-none sm:w-60 sm:flex-none"
        inputClass="rounded-full border border-line bg-[var(--surface-2)] py-1.5 pl-3.5 pr-10 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
      <select bind:value={sortBy} aria-label="Sort collections" title="Sort collections"
        class="shrink-0 rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm font-semibold">
        <option value="updated">Recently updated</option>
        <option value="recent">Recent</option>
        <option value="name">Name A–Z</option>
        <option value="size">Largest</option>
      </select>
    </div>
  </div>

  {#if !shown.length}
    {#if !showLocked && lockedHiddenCount && !q.trim()}
      <div class="py-16 text-center text-sm text-muted">
        <p class="mb-3">{lockedHiddenCount} locked collection{lockedHiddenCount === 1 ? '' : 's'} hidden.</p>
        <button type="button" onclick={() => (showLocked = true)}
          class="inline-flex items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-4 py-2 font-semibold transition hover:border-[var(--accent)]">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          Show locked
        </button>
      </div>
    {:else}
      <p class="py-16 text-center text-sm text-muted">No collections match “{q.trim()}”.</p>
    {/if}
  {:else}
    {#if heroEntries.length}
      <p class="mb-2 text-[11px] font-bold uppercase tracking-[0.14em] text-muted">Recently active</p>
      <div class="mb-5 grid grid-cols-3 gap-3">
        {#each heroEntries as c (c.id)}
          {@render collectionCard(c, true)}
        {/each}
      </div>
    {/if}
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {#each gridEntries as c (c.id)}
        {@render collectionCard(c, false)}
      {/each}
    </div>
  {/if}
{/if}

{#snippet collectionCard(c, hero = false)}
  {@const sealed = c.locked && !c.unlocked}
  {@const liveVideo = live === c.id ? liveVideoFor(c) : null}
  <!-- Group cards get a stacked-deck silhouette (edges peeking above the card) so a
       CONTAINER never shares a body with a leaf collection. -->
  <div class="relative {c.is_group ? 'pt-2' : ''}">
    {#if c.is_group}
      <span aria-hidden="true" class="deck-edge absolute inset-x-4 top-0 h-3 rounded-t-[10px]"></span>
      <span aria-hidden="true" class="deck-edge-near absolute inset-x-2 top-1 h-3 rounded-t-[10px]"></span>
    {/if}
    <article
      class="group relative overflow-hidden rounded-card border bg-[var(--surface-2)] transition-colors focus-within:border-[var(--accent)] {sealed ? 'vault-card border-line' : 'border-line hover:border-[var(--accent)]'} {dragging === c.id ? 'opacity-40' : ''} {dropTarget === c.id ? 'drop-target' : ''}"
      draggable={!sealed && !c.is_group}
      ondragstart={(e) => dragStart(c, e)}
      ondragend={dragEnd}
      ondragover={(e) => dragOver(c, e)}
      ondragleave={(e) => dragLeave(c, e)}
      ondrop={(e) => canDrop(c) && dropOn(c, e)}
      onpointerenter={(e) => liveEnter(c, e)}
      onpointerleave={liveLeave}>
      <!-- Cover with title + count overlay. -->
      <div class="relative {hero ? 'aspect-[21/10]' : 'aspect-[4/3]'} w-full overflow-hidden bg-[var(--media-bg)]">
        {#if sealed}
          <!-- Vault: deliberately unlike a media card — no imagery, a ringed lock on a
               dark glow. Identity is redacted server-side; the card only says "locked". -->
          <span class="vault-face grid h-full w-full place-items-center">
            <span class="grid h-16 w-16 place-items-center rounded-full border border-line bg-[var(--surface-2)]/70 text-muted backdrop-blur-sm">
              <svg viewBox="0 0 24 24" class="h-7 w-7 opacity-80" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
            </span>
          </span>
        {:else if c.covers?.length > 1}
          <span class="grid h-full w-full grid-cols-2 grid-rows-2 gap-0.5">
            {#each c.covers.slice(0, 4) as cover, ci (cover)}
              <img src={cover} alt="" loading="lazy"
                srcset={coverSrcset(cover, c.cover_items?.[ci])} sizes={quadSizes(hero)}
                class="h-full w-full object-cover object-top transition group-hover:scale-[1.03] {live === c.id && !liveVideo ? 'live-drift' : ''}" />
            {/each}
          </span>
        {:else if c.cover}
          <img src={c.cover} alt="" loading="lazy"
            srcset={coverSrcset(c.cover, c.cover_peek)} sizes={fullSizes(hero)}
            class="h-full w-full object-cover object-top transition group-hover:scale-[1.03] {live === c.id && !liveVideo ? 'live-drift' : ''}" />
        {:else}
          <span class="grid h-full w-full place-items-center text-sm text-muted">No cover</span>
        {/if}

        {#if liveVideo}
          <!-- Living cover: one muted clip fades in over the mosaic while hovered. -->
          <video class="live-video pointer-events-none absolute inset-0 h-full w-full object-cover object-top"
            src={liveVideo.href} poster={liveVideo.thumb || undefined} autoplay muted loop playsinline></video>
        {/if}

        {#if c.locked && c.unlocked}
          <!-- Unlocked-for-now badge: one tap to re-lock immediately. -->
          <button type="button" class="absolute left-2 top-2 z-20 inline-flex items-center gap-1 rounded-full bg-[var(--accent)]/90 px-2 py-1 text-[11px] font-bold text-[var(--on-accent)] backdrop-blur-sm"
            title={`Unlocked — ${unlockHoursLeft(c)}h left. Click to lock now.`} aria-label="Lock now" onclick={(e) => { e.stopPropagation(); relockNow(c); }}>
            <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
            {unlockHoursLeft(c)}h
          </button>
        {/if}

        <span class="pointer-events-none absolute inset-x-0 bottom-0 z-[5] bg-gradient-to-t from-[var(--media-scrim-strong)] to-transparent px-3 pb-3 {hero ? 'pt-20' : 'pt-14'} text-[var(--media-control-ink)]">
          <span class="flex min-w-0 items-center gap-1.5">
            {#if c.is_group}
              <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 opacity-85" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
            {/if}
            <span class="block truncate {hero ? 'text-2xl' : 'text-lg'} font-black tracking-tight">{c.name}</span>
          </span>
          <span class="block {hero ? 'text-sm' : 'text-xs'} font-medium opacity-65">{sealed && c.is_group ? `Locked · ${c.collection_count ?? 0} collection${(c.collection_count ?? 0) === 1 ? '' : 's'}` : `${sealed ? 'Locked · ' : ''}${countLabel(c)}`}</span>
        </span>
      </div>

          <!-- Full-bleed open target sits under the action buttons (which carry higher z).
               A sealed collection opens the unlock prompt instead of its contents.
               Also the long-press surface: hold to peek at the pressed cover's full media. -->
          <button type="button" class="peek-press absolute inset-0 z-0 select-none" aria-label={sealed ? `Unlock ${c.is_group ? 'group' : 'collection'} ${c.name}` : `Open ${c.is_group ? 'group' : 'collection'} ${c.name}`}
            onclick={() => openClick(c, sealed)}
            onpointerdown={(e) => peekDown(c, e)}
            onpointermove={peekMove}
            oncontextmenu={(e) => { if (peekTimer != null || peek) e.preventDefault(); }}></button>

          <!-- Secondary actions: top-right, revealed on hover / keyboard focus anywhere in the
               card (group-focus-within), always shown for touch. Hidden while sealed (so a
               lock can't be bypassed by deleting the collection or queueing its videos). -->
          <div class="absolute right-2 top-2 z-10 flex gap-1.5 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100">
            {#if !sealed && !c.is_group && ((c.video_count ?? 0) >= 1 || (c.image_count ?? 0) >= 1)}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--media-control-border-hover)] hover:bg-[var(--media-control-bg-hover)]"
                title="Add this collection's videos and photos to the montage queue" aria-label="Add this collection's videos and photos to the montage queue" onclick={() => onqueue(c)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
              </button>
            {/if}
            {#if !c.locked}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--accent)] hover:text-[var(--accent)]"
                title={c.is_group ? 'Lock this group with a password' : 'Lock this collection with a password'} aria-label={c.is_group ? 'Lock group' : 'Lock collection'} onclick={() => (lockModal = c.is_group ? { group: c, mode: 'set' } : { collection: c, mode: 'set' })}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
              </button>
            {:else if c.unlocked}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--accent)] hover:text-[var(--accent)]"
                title="Manage lock (re-lock now or remove the password)" aria-label="Manage lock" onclick={() => (lockModal = c.is_group ? { group: c, mode: 'manage' } : { collection: c, mode: 'manage' })}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
              </button>
            {/if}
            {#if !sealed && !c.is_group}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--danger-hover)] hover:bg-[var(--danger-hover)] hover:text-white"
                title="Delete collection" aria-label="Delete collection" onclick={() => (confirming = c)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
              </button>
            {/if}
          </div>

          <!-- Primary actions: bottom-right, only when accessible and has videos. "Add to
               Play Queue" and "Shuffle" (compact, secondary) sit to the LEFT of the accent
               "Play" — queue these videos onto the cross-library Play Queue, or play this
               collection now in order / at random. An icon rather than a caret menu: the card
               is overflow-hidden for its mosaic, which would clip a dropdown panel. -->
          {#if !sealed && !c.is_group && c.video_count}
            <div class="absolute bottom-2 right-2 z-10 flex items-center gap-1.5">
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] opacity-0 shadow-lg backdrop-blur-sm transition hover:border-[var(--accent)] hover:text-[var(--accent)] group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
                title="Add this collection's videos to the Play Queue" aria-label="Add collection videos to play queue" onclick={() => onplayqueue(c)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 12H3"/><path d="M16 6H3"/><path d="M12 18H3"/><path d="m16 12 5 3-5 3v-6Z"/></svg>
              </button>
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] opacity-0 shadow-lg backdrop-blur-sm transition hover:border-[var(--accent)] hover:text-[var(--accent)] group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
                title="Play this collection's videos in a random order" aria-label="Play collection videos at random" onclick={() => onplay(c, null, { shuffle: true })}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"/><path d="m18 2 4 4-4 4"/><path d="M2 6h1.9c1.5 0 2.9.9 3.6 2.2"/><path d="M22 18h-5.9c-1.3 0-2.6-.7-3.3-1.8l-.5-.8"/><path d="m18 14 4 4-4 4"/></svg>
              </button>
              <button type="button" class="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-[var(--on-accent)] opacity-0 shadow-lg transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
                title="Play videos" aria-label="Play collection videos" onclick={() => onplay(c)}>
                <span aria-hidden="true">▶</span> Play
              </button>
            </div>
          {:else if sealed}
            <button type="button" class="absolute bottom-2 right-2 z-10 inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-[var(--on-accent)] opacity-0 shadow-lg transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"
              title={c.is_group ? 'Unlock group' : 'Unlock collection'} aria-label={c.is_group ? 'Unlock group' : 'Unlock collection'} onclick={() => (lockModal = c.is_group ? { group: c, mode: 'unlock' } : { collection: c, mode: 'unlock' })}>
              <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
              Unlock
            </button>
          {/if}
    </article>
  </div>
{/snippet}

{#if confirming}
  {@const childCount = collectionsList.filter((c) => c.parent_id === confirming.id).length}
  <ConfirmDialog title="Delete collection?"
    message={`"${confirming.name}"${childCount ? ` and its ${childCount} sub-collection${childCount === 1 ? '' : 's'}` : ''} will be removed. The media files stay in your library.`}
    confirmLabel="Delete"
    onconfirm={() => { removeCollection(confirming.id); confirming = null; }}
    oncancel={() => (confirming = null)} />
{/if}

{#if lockModal}
  <CollectionLockModal collection={lockModal.collection} group={lockModal.group} mode={lockModal.mode}
    onclose={() => (lockModal = null)} ondone={() => { loadCollections(); requestGalleryReload(); }} />
{/if}

{#if groupPrompt}
  <!-- Dropped one ungrouped collection onto another: name the group they'll share. -->
  <Modal onclose={() => (groupPrompt = null)} ariaLabel="Name the new group" z="z-[70]" panelClass="panel w-full max-w-sm rounded-2xl p-6">
    <h2 class="mb-1 text-lg font-bold">Group these collections?</h2>
    <p class="mb-4 text-sm leading-relaxed text-muted">“{groupPrompt.source.name}” and “{groupPrompt.target.name}” will move into a new group.</p>
    <input use:autofocus bind:value={groupPrompt.name} placeholder="Group name" maxlength="60"
      class="mb-4 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]"
      onkeydown={(e) => { if (e.key === 'Enter') confirmGroupPrompt(); }} />
    <div class="flex gap-2">
      <Button variant="secondary" size="lg" class="flex-1" onclick={() => (groupPrompt = null)}>Cancel</Button>
      <Button variant="primary" size="lg" class="flex-1" disabled={!groupPrompt.name.trim()} onclick={confirmGroupPrompt}>Create group</Button>
    </div>
  </Modal>
{/if}

<!-- Release anywhere (or a cancelled gesture / window losing focus) ends the peek. -->
<svelte:window onpointerup={peekEnd} onpointercancel={peekEnd} onblur={peekEnd} />

<PeekOverlay item={peek} />

<style>
  /* Long-press is the peek gesture — stop iOS Safari's save/callout menu from
     hijacking it (same trick as JustifiedGrid's select-mode long-press). */
  .peek-press {
    -webkit-touch-callout: none;
  }

  /* Stacked-deck edges behind group cards: two card "backs" peeking above the top edge.
     They live OUTSIDE the overflow-hidden article (in the pt-2 wrapper), farthest first. */
  .deck-edge,
  .deck-edge-near {
    border: 1px solid var(--line);
    border-bottom: none;
  }

  .deck-edge {
    background: color-mix(in srgb, var(--surface-2) 72%, var(--bg));
  }

  .deck-edge-near {
    background: color-mix(in srgb, var(--surface-2) 88%, var(--ink) 4%);
  }

  /* Vault face for sealed cards: no imagery by design — a faint accent glow, hairline
     diagonal stripes, and a darkened floor so it can't be mistaken for a media card. */
  .vault-face {
    background:
      radial-gradient(120% 90% at 50% 8%, color-mix(in srgb, var(--accent) 8%, transparent), transparent 62%),
      repeating-linear-gradient(135deg, transparent 0 16px, color-mix(in srgb, var(--line) 30%, transparent) 16px 17px),
      linear-gradient(180deg, color-mix(in srgb, var(--media-bg) 86%, black), var(--media-bg));
  }

  /* Living cover: the clip melts in instead of popping. */
  .live-video {
    animation: live-fade 480ms ease;
  }

  @keyframes live-fade {
    from { opacity: 0; }
  }

  /* Image-only living cover: a slow Ken Burns drift across the mosaic tiles. */
  .live-drift {
    animation: live-drift 7s ease-in-out infinite alternate;
  }

  @keyframes live-drift {
    from { transform: scale(1.04); }
    to { transform: scale(1.16) translateY(-2.5%); }
  }

  /* Card under a dragged collection: accent ring + glow says "drop to group". */
  .drop-target {
    border-color: var(--accent);
    box-shadow:
      0 0 0 2px color-mix(in srgb, var(--accent) 55%, transparent),
      0 0 26px color-mix(in srgb, var(--accent) 32%, transparent);
  }
</style>
