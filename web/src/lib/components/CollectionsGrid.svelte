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
  let onlyUngrouped = $state(false); // landing filter: only collections that aren't in a group
  // 'cards' = the cover grid (default, untouched); 'outline' = the whole Group → Collection
  // → Sub-collection tree on one screen. Nested collections never appear on the card
  // landing (it shows roots only), so the outline is the only place the hierarchy is visible.
  let viewMode = $state('cards');
  let landingScrollY = 0;
</script>

<script>
  import { tick } from 'svelte';
  import { collections, collectionGroups, removeCollection, updateCollection, setGroupCover, setCollectionsGroup, renameCollectionGroup, loadCollections, requestGalleryReload } from '$lib/state.js';
  import { relockCollection, relockAllCollections, relockGroup } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import SearchField from './SearchField.svelte';
  import CollectionLockModal from './CollectionLockModal.svelte';
  import CollectionsOutline from './CollectionsOutline.svelte';
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
  // The member a group card is pinned to ("Use as group cover"): the newest pin among
  // members this session can see. A sealed member ships no covers, so it can't win.
  const pinnedMember = (members) => members
    .filter((m) => m.group_cover_at && !isSealed(m))
    .reduce((best, m) => (!best || m.group_cover_at > best.group_cover_at ? m : best), null);
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
  // How many sub-collections hang off each collection. The landing renders roots only, so
  // without this nothing on screen says a collection (or a group) contains any nesting at
  // all — you had to open each one to find out. Sealed children are left out (unlike the
  // delete dialog, which must warn about them): the badge is a promise of what the Outline
  // will actually list, and the outline can't list a collection this session can't see.
  const childCountOf = $derived.by(() => {
    const m = new Map();
    for (const c of collectionsList) {
      if (!c.parent_id || (!showLocked && isSealed(c))) continue;
      m.set(c.parent_id, (m.get(c.parent_id) || 0) + 1);
    }
    return m;
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
      // Cover: the pinned member's own mosaic, else the newest covers across every member
      // this session can see (cover_items carry created_at) — never just whichever member
      // happens to be stored first, which pinned the card to the newest-CREATED collection.
      const pinned = pinnedMember(members);
      let coverItems;
      if (pinned) {
        coverItems = (pinned.cover_items || []).slice(0, 4);
      } else {
        const byId = new Map();
        for (const member of members) {
          if (isSealed(member)) continue;
          for (const item of member.cover_items || []) {
            if (item?.id && !byId.has(item.id)) byId.set(item.id, item);
          }
        }
        coverItems = [...byId.values()]
          .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
          .slice(0, 4);
      }
      const covers = coverItems.map((it) => it.thumb);
      const itemCount = members.reduce((sum, c) => sum + mediaCount(c), 0);
      entries.push({
        id: `group:${key}`,
        is_group: true,
        name: groupNames.get(key) || bucket.name,
        members,
        group_key: key,
        collection_count: server.collection_count ?? members.length,
        sub_count: members.reduce((sum, c) => sum + (childCountOf.get(c.id) || 0), 0),
        item_count: itemCount,
        video_count: members.reduce((sum, c) => sum + (c.video_count ?? 0), 0),
        image_count: members.reduce((sum, c) => sum + (c.image_count ?? 0), 0),
        covers,
        cover_items: coverItems,
        cover: (pinned ? pinned.cover : null) || covers[0] || null,
        cover_peek: (pinned ? pinned.cover_peek : null) || coverItems[0] || null,
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
  // Roots with no group — the outline's second section (the card grid mixes them into
  // topEntries alongside the group cards).
  const ungroupedRoots = $derived(topEntries.filter((c) => !c.is_group));
  // The outline is a landing-only map: inside a group you're already one tier down, so the
  // drill-in always renders cards.
  const outline = $derived(viewMode === 'outline' && !activeGroup);
  const activeMembers = $derived.by(() => {
    const key = groupKey(activeGroup);
    return rolledList
      .map((c, index) => ({ ...c, store_index: index }))
      .filter((c) => groupKey(c.group) === key);
  });
  const activePin = $derived(activeGroup ? pinnedMember(activeMembers) : null);

  // --- Search is LIBRARY-WIDE, not tier-wide. The card landing browses one tier (group
  // cards + ungrouped roots), and search used to filter exactly that list — so typing a
  // sub-collection's name matched nothing (children aren't in topEntries), and neither did
  // the name of a collection that sits inside a group (it's folded into the group card).
  // With a query the pool becomes every collection plus the group cards, each match carrying
  // the crumb that says where it lives; an empty query leaves the landing untouched.
  const collById = $derived(new Map(collectionsList.map((c) => [c.id, c])));
  // "Parent" for a sub-collection, "Group" for a grouped root, '' for a plain root. Also
  // part of the haystack: searching a parent's (or group's) name surfaces what's inside it,
  // the same way a matching ancestor opens its subtree in the Outline.
  const crumbOf = (c) => (c.parent_id ? collById.get(c.parent_id)?.name || '' : String(c.group || '').trim());
  const needle = $derived(q.trim().toLowerCase());
  const searchPool = $derived.by(() => {
    if (!needle) return [];
    const cards = rolledList.map((c, index) => ({ ...c, store_index: index, crumb: crumbOf(c) }));
    // Inside a group, search stays scoped to that group: its members and their
    // sub-collections, never the rest of the library.
    if (activeGroup) {
      const memberIds = new Set(activeMembers.map((c) => c.id));
      return cards.filter((c) => memberIds.has(c.id) || memberIds.has(c.parent_id));
    }
    return [...cards, ...groupEntries];
  });
  // Searching overrides the "Not in a group" landing filter (the button hides while a query
  // is live) — a whole-library search that silently skipped grouped collections would be
  // the very hole this fixes.
  const baseEntries = $derived(needle ? searchPool : activeGroup ? activeMembers : onlyUngrouped ? topEntries.filter((c) => !c.is_group) : topEntries);
  const lockedHiddenCount = $derived(baseEntries.filter(isSealed).length);
  const visibleTotal = $derived(baseEntries.filter((c) => showLocked || !isSealed(c)).length);

  const filtered = $derived(
    baseEntries.filter((c) =>
      (showLocked || !isSealed(c)) &&
      (!needle || `${c.name || ''} ${c.crumb || ''}`.toLowerCase().includes(needle)))
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
    if (!wideScreen || activeGroup || selecting || onlyUngrouped || sortBy !== 'updated' || needle || shown.length < 8) return [];
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
  const canDrop = (c) => !!dragging && c.id !== dragging && !c.parent_id && !isSealed(c);
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
    if (selecting) return; // in select mode a press toggles the card, never peeks
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

  // --- Select mode: tick collection cards, then move them into a group (or out of one) in
  // one write. Only plain collections can be picked — group cards aren't movable, and a sealed
  // card's copy here is a redacted placeholder the server won't take edits for.
  let selecting = $state(false);
  let picked = $state(new Set());
  let movePrompt = $state(null); // { name } -> move-to-group picker
  const pickedList = $derived(collectionsList.filter((c) => picked.has(c.id)));
  const pickedGrouped = $derived(pickedList.filter((c) => String(c.group || '').trim()).length);
  function toggleSelecting() {
    selecting = !selecting;
    picked = new Set();
  }
  function togglePick(c) {
    const next = new Set(picked);
    if (next.has(c.id)) next.delete(c.id);
    else next.add(c.id);
    picked = next;
  }
  function selectAllShown() {
    picked = new Set([...picked, ...shown.filter((c) => !c.is_group && !c.parent_id && !isSealed(c)).map((c) => c.id)]);
  }
  function openMovePrompt() {
    if (pickedList.length) movePrompt = { name: '' };
  }
  function finishSelection(message) {
    movePrompt = null;
    picked = new Set();
    selecting = false;
    toast(message, { type: 'success' });
    loadCollections(); // pick up the server's canonical group names + lock summaries
  }
  function confirmMove(name) {
    const typed = String(name || '').trim().slice(0, 60);
    if (!typed || !pickedList.length) return;
    const existing = groupEntries.find((g) => g.group_key === groupKey(typed));
    if (existing && isSealed(existing)) {
      toast('Unlock that group before adding collections to it', { type: 'error' });
      return;
    }
    const target = existing ? existing.name : typed;
    const moved = setCollectionsGroup(pickedList.map((c) => c.id), target);
    finishSelection(moved ? `Moved ${moved} collection${moved === 1 ? '' : 's'} to “${target}”` : `Already in “${target}”`);
  }
  function removePickedFromGroup() {
    const moved = setCollectionsGroup(pickedList.map((c) => c.id), '');
    finishSelection(`Took ${moved} collection${moved === 1 ? '' : 's'} out of ${moved === 1 ? 'its group' : 'their groups'}`);
  }

  // --- Group actions (inside a group): rename, or ungroup back onto the landing. Blocked while
  // the group is locked (its lock is stored under the group's name, so renaming or dissolving
  // would silently shed it) or holds sealed members (their records can't be edited from here,
  // so they'd be left behind in a split group).
  const activeGroupEntry = $derived(activeGroup ? groupEntries.find((g) => g.group_key === groupKey(activeGroup)) || null : null);
  const groupEditBlocker = $derived.by(() => {
    if (!activeGroup) return '';
    if (activeGroupEntry?.locked) return 'Remove the group lock first — the lock is tied to the group name';
    const sealedCount = activeMembers.filter(isSealed).length;
    return sealedCount ? `Unlock its ${sealedCount} locked collection${sealedCount === 1 ? '' : 's'} first` : '';
  });
  let renamingGroup = $state(false);
  let groupDraft = $state('');
  let confirmUngroup = $state(false);
  function startGroupRename() {
    groupDraft = activeGroup;
    renamingGroup = true;
  }
  function commitGroupRename() {
    if (!renamingGroup) return; // Enter commits, then the unmounting input's blur calls again
    renamingGroup = false;
    const to = groupDraft.trim().slice(0, 60);
    if (!to || to === activeGroup) return;
    const other = groupEntries.find((g) => g.group_key === groupKey(to) && g.group_key !== groupKey(activeGroup));
    if (other?.locked) {
      toast("Can't merge into a locked group", { type: 'error' });
      return;
    }
    const target = other ? other.name : to;
    renameCollectionGroup(activeGroup, target);
    activeGroup = target;
    toast(other ? `Merged into “${target}”` : `Renamed group to “${target}”`, { type: 'success' });
    loadCollections();
  }
  function ungroupActive() {
    const name = activeGroup;
    const moved = setCollectionsGroup(activeMembers.map((c) => c.id), '');
    confirmUngroup = false;
    toast(`Ungrouped “${name}” — ${moved} collection${moved === 1 ? '' : 's'} back on the Library page`, { type: 'success' });
    closeGroup();
    loadCollections();
  }
  // A group view whose group no longer exists (emptied from select mode, or ungrouped) returns
  // to the landing instead of showing an empty page.
  $effect(() => {
    if (activeGroup && !renamingGroup && collectionsList.length && !groupEntries.some((g) => g.group_key === groupKey(activeGroup))) closeGroup();
  });

  // Cover srcset: the 400px grid thumb for small slots, the server's lazily generated
  // /covers/<id>.jpg high-res tier once a card renders large (desktop). `it` is a
  // cover_items entry (which mirrors covers, same items and order — see server summary).
  const coverSrcset = (thumb, it) => (it?.id ? `${thumb} 400w, /covers/${it.id}.jpg 1280w` : undefined);
  // Mosaic quadrants are half the card's width; hero cards sit 3-across on wide screens.
  const quadSizes = (hero) => (hero ? '16vw' : '(min-width: 1280px) 12vw, (min-width: 1024px) 16vw, (min-width: 640px) 25vw, 50vw');
  const fullSizes = (hero) => (hero ? '33vw' : '(min-width: 1280px) 25vw, (min-width: 1024px) 33vw, (min-width: 640px) 50vw, 100vw');

  // Sub-collections hiding under a card: a group's total across its members, or a
  // collection's own children. Drives the folder pill on the cover — the only hint on the
  // card landing that there's another tier below (Outline view shows the whole tree).
  const subCountOf = (c) => (c.is_group ? c.sub_count || 0 : childCountOf.get(c.id) || 0);

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
      {#if renamingGroup}
        <input use:autofocus bind:value={groupDraft} maxlength="60" aria-label="Group name"
          onkeydown={(e) => { if (e.key === 'Enter') commitGroupRename(); else if (e.key === 'Escape') renamingGroup = false; }}
          onblur={commitGroupRename}
          class="min-w-0 rounded-lg border border-[var(--accent)] bg-[var(--surface-2)] px-2.5 py-1 text-base font-extrabold outline-none" />
      {:else}
        <span class="min-w-0 truncate text-base font-extrabold">{activeGroup}</span>
        <button type="button" onclick={startGroupRename} disabled={!!groupEditBlocker} title={groupEditBlocker || 'Rename this group (typing another group’s name merges them)'}
          class="shrink-0 rounded-lg border border-line px-2.5 py-1 text-xs font-semibold transition enabled:hover:border-[var(--accent)] disabled:opacity-40">Rename</button>
        <button type="button" onclick={() => (confirmUngroup = true)} disabled={!!groupEditBlocker} title={groupEditBlocker || 'Remove this group — its collections go back to the Library page'}
          class="shrink-0 rounded-lg border border-line px-2.5 py-1 text-xs font-semibold transition enabled:hover:border-[var(--accent)] disabled:opacity-40">Ungroup</button>
      {/if}
    {/if}
    {#if !outline}
      <span class="text-sm text-muted">{#if needle}{shown.length} match{shown.length === 1 ? '' : 'es'}{:else}{visibleTotal} collection{visibleTotal === 1 ? '' : 's'}{/if}</span>
    {/if}
    <div class="ml-auto flex w-full flex-wrap items-center gap-2 sm:w-auto sm:justify-end">
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
      {#if !activeGroup && groupEntries.length && !outline && !needle}
        <button type="button" onclick={() => (onlyUngrouped = !onlyUngrouped)} aria-pressed={onlyUngrouped}
          title={onlyUngrouped ? 'Show groups and every collection again' : 'Show only collections that are not in a group'}
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-semibold transition {onlyUngrouped ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)]' : 'border-line bg-[var(--surface-2)] hover:border-[var(--accent)]'}">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M7 12h10M10 18h4"/></svg>
          Not in a group
        </button>
      {/if}
      {#if !outline}
      <button type="button" onclick={toggleSelecting} aria-pressed={selecting}
        title={selecting ? 'Stop organizing' : 'Select collections to move them into or out of a group'}
        class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-semibold transition {selecting ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)]' : 'border-line bg-[var(--surface-2)] hover:border-[var(--accent)]'}">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="m8 12 3 3 5-6"/></svg>
        {selecting ? 'Done' : 'Organize'}
      </button>
      {/if}
      <button type="button" onclick={() => fileInput?.click()}
        class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]"
        title="Import a folder of videos/images into a new collection">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4 4 4-4"/><path d="M4 14.9A6 6 0 0 1 7 4a5 5 0 0 1 9 1 4 4 0 0 1 2 7.7"/></svg>
        Import
      </button>
      {#if !activeGroup}
        <!-- Cards ⇄ Outline. The card grid can only ever show tier one; the outline is
             where the whole Group → Collection → Sub-collection tree lives. -->
        <div class="inline-flex shrink-0 rounded-lg border border-line bg-[var(--surface-2)] p-0.5" role="group" aria-label="Library layout">
          {#each [{ id: 'cards', label: 'Cards', hint: 'Cover cards — groups and top-level collections' }, { id: 'outline', label: 'Outline', hint: 'The whole tree: groups, collections and their sub-collections' }] as v (v.id)}
            <button type="button" aria-pressed={viewMode === v.id} title={v.hint}
              class="rounded-md px-2.5 py-1 text-sm font-semibold transition {viewMode === v.id ? 'bg-[var(--surface-solid)] text-ink shadow-sm' : 'text-muted hover:text-ink'}"
              onclick={() => { viewMode = v.id; if (v.id === 'outline' && selecting) toggleSelecting(); }}>{v.label}</button>
          {/each}
        </div>
      {/if}
      <SearchField bind:value={q} placeholder={outline ? 'Search the whole tree…' : 'Search collections…'} ariaLabel="collection search"
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

  {#if outline}
    <CollectionsOutline groups={groupEntries} ungrouped={ungroupedRoots} collections={rolledList}
      {q} {sortBy} {showLocked}
      onopengroup={openGroup} {onopen} onplay={(c) => onplay(c)}
      onunlock={(c) => (lockModal = c.is_group ? { group: c, mode: 'unlock' } : { collection: c, mode: 'unlock' })} />
  {:else if !shown.length}
    {#if !showLocked && lockedHiddenCount && !needle}
      <div class="py-16 text-center text-sm text-muted">
        <p class="mb-3">{lockedHiddenCount} locked collection{lockedHiddenCount === 1 ? '' : 's'} hidden.</p>
        <button type="button" onclick={() => (showLocked = true)}
          class="inline-flex items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-4 py-2 font-semibold transition hover:border-[var(--accent)]">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          Show locked
        </button>
      </div>
    {:else}
      <p class="py-16 text-center text-sm text-muted">{needle ? `No collections match “${q.trim()}”.` : onlyUngrouped ? 'Every collection is in a group.' : 'Nothing to show here.'}</p>
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
  {#if selecting}
    <!-- Select-mode action bar: sticks to the bottom of the viewport while you pick. -->
    <div class="sticky bottom-3 z-30 mt-4 flex flex-wrap items-center gap-2 rounded-xl border border-[var(--accent)] bg-[var(--surface-solid)] px-3 py-2 shadow-lg">
      <span class="text-sm font-semibold tabular-nums">{pickedList.length} selected</span>
      <button type="button" onclick={selectAllShown} class="rounded-lg px-2 py-1 text-xs font-semibold text-muted transition hover:text-ink">Select all shown</button>
      {#if pickedList.length}
        <button type="button" onclick={() => (picked = new Set())} class="rounded-lg px-2 py-1 text-xs font-semibold text-muted transition hover:text-ink">Clear</button>
      {/if}
      <div class="ml-auto flex flex-wrap items-center gap-2">
        <button type="button" onclick={openMovePrompt} disabled={!pickedList.length}
          class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm font-bold text-[var(--on-accent)] transition hover:brightness-110 disabled:opacity-40">Move to group…</button>
        <button type="button" onclick={removePickedFromGroup} disabled={!pickedGrouped}
          title={pickedGrouped ? 'Put the selected collections back on the Library page' : 'None of the selected collections are in a group'}
          class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition enabled:hover:border-[var(--accent)] disabled:opacity-40">Remove from group</button>
        <button type="button" onclick={toggleSelecting}
          class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]">Done</button>
      </div>
    </div>
  {/if}
{/if}

{#snippet collectionCard(c, hero = false)}
  {@const sealed = c.locked && !c.unlocked}
  {@const liveVideo = live === c.id ? liveVideoFor(c) : null}
  {@const canPick = selecting && !c.is_group && !c.parent_id && !sealed}
  {@const isPicked = canPick && picked.has(c.id)}
  <!-- Group cards get a stacked-deck silhouette (edges peeking above the card) so a
       CONTAINER never shares a body with a leaf collection. -->
  <div class="relative {c.is_group ? 'pt-2' : ''}">
    {#if c.is_group}
      <span aria-hidden="true" class="deck-edge absolute inset-x-4 top-0 h-3 rounded-t-[10px]"></span>
      <span aria-hidden="true" class="deck-edge-near absolute inset-x-2 top-1 h-3 rounded-t-[10px]"></span>
    {/if}
    <article
      class="group relative overflow-hidden rounded-card border bg-[var(--surface-2)] transition-colors focus-within:border-[var(--accent)] {sealed ? 'vault-card border-line' : 'border-line hover:border-[var(--accent)]'} {dragging === c.id ? 'opacity-40' : ''} {dropTarget === c.id ? 'drop-target' : ''} {isPicked ? 'picked' : ''}"
      draggable={!sealed && !c.is_group && !c.parent_id && !selecting}
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

        {#if canPick}
          <!-- Select-mode tick box (the whole card is the toggle; this just shows the state). -->
          <span aria-hidden="true" class="pointer-events-none absolute left-2 top-2 z-20 grid h-6 w-6 place-items-center rounded-md border-2 text-sm font-black shadow {isPicked ? 'border-[var(--accent)] bg-[var(--accent)] text-[var(--on-accent)]' : 'border-white/85 bg-black/35 text-transparent'}">✓</span>
        {/if}
        {#if c.locked && c.unlocked && !selecting}
          <!-- Unlocked-for-now badge: one tap to re-lock immediately. -->
          <button type="button" class="absolute left-2 top-2 z-20 inline-flex items-center gap-1 rounded-full bg-[var(--accent)]/90 px-2 py-1 text-[11px] font-bold text-[var(--on-accent)] backdrop-blur-sm"
            title={`Unlocked — ${unlockHoursLeft(c)}h left. Click to lock now.`} aria-label="Lock now" onclick={(e) => { e.stopPropagation(); relockNow(c); }}>
            <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
            {unlockHoursLeft(c)}h
          </button>
        {/if}

        <span class="pointer-events-none absolute inset-x-0 bottom-0 z-[5] bg-gradient-to-t from-[var(--media-scrim-strong)] to-transparent px-3 pb-3 {hero ? 'pt-20' : 'pt-14'} text-[var(--media-control-ink)]">
          {#if c.crumb && !sealed}
            <!-- Where this match lives. Search spans every tier, so a card that isn't on the
                 landing you're looking at says so: "Neon ›" for a sub-collection, "Sci-fi /"
                 for a collection inside a group. Only ever set on search results. -->
            <span class="mb-0.5 flex min-w-0 items-center gap-1 text-[11px] font-bold uppercase tracking-[0.12em] opacity-75">
              <svg viewBox="0 0 24 24" class="h-3 w-3 shrink-0" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
              <span class="truncate">{c.crumb}</span>
              <span class="shrink-0" aria-hidden="true">{c.parent_id ? '›' : '/'}</span>
            </span>
          {/if}
          <span class="flex min-w-0 items-center gap-1.5">
            {#if c.is_group}
              <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 opacity-85" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
            {/if}
            <span class="block truncate {hero ? 'text-2xl' : 'text-lg'} font-black tracking-tight">{c.name}</span>
            {#if !sealed && subCountOf(c)}
              <!-- Nesting badge: sub-collections live a tier below the landing, so without
                   this the card gives no sign they exist at all. -->
              <span class="inline-flex shrink-0 items-center gap-1 rounded-full border border-[var(--media-control-border)] bg-[var(--media-control-bg)] px-1.5 py-0.5 text-[11px] font-bold tabular-nums backdrop-blur-sm"
                title="{subCountOf(c)} sub-collection{subCountOf(c) === 1 ? '' : 's'} inside">
                <svg viewBox="0 0 24 24" class="h-3 w-3" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-2.5a1 1 0 0 1-.8-.4l-.9-1.2A1 1 0 0 0 15 3h-2a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z"/><path d="M20 21a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1h-2.5a1 1 0 0 1-.8-.4l-.9-1.2a1 1 0 0 0-.8-.4h-2a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z"/><path d="M3 5a2 2 0 0 0 2 2h3"/><path d="M3 3v13a2 2 0 0 0 2 2h3"/></svg>
                {subCountOf(c)}
              </span>
            {/if}
          </span>
          <span class="block {hero ? 'text-sm' : 'text-xs'} font-medium opacity-65">{sealed && c.is_group ? `Locked · ${c.collection_count ?? 0} collection${(c.collection_count ?? 0) === 1 ? '' : 's'}` : `${sealed ? 'Locked · ' : ''}${countLabel(c)}${activePin && activePin.id === c.id ? ' · Group cover' : ''}`}</span>
        </span>
      </div>

          <!-- Full-bleed open target sits under the action buttons (which carry higher z).
               A sealed collection opens the unlock prompt instead of its contents.
               Also the long-press surface: hold to peek at the pressed cover's full media. -->
          <button type="button" class="peek-press absolute inset-0 z-0 select-none"
            aria-label={canPick ? `${isPicked ? 'Deselect' : 'Select'} collection ${c.name}` : sealed ? `Unlock ${c.is_group ? 'group' : 'collection'} ${c.name}` : `Open ${c.is_group ? 'group' : 'collection'} ${c.name}`}
            aria-pressed={canPick ? isPicked : undefined}
            onclick={() => (canPick ? togglePick(c) : openClick(c, sealed))}
            onpointerdown={(e) => peekDown(c, e)}
            onpointermove={peekMove}
            oncontextmenu={(e) => { if (peekTimer != null || peek) e.preventDefault(); }}></button>

          <!-- Secondary actions: top-right, revealed on hover / keyboard focus anywhere in the
               card (group-focus-within), always shown for touch. Hidden while sealed (so a
               lock can't be bypassed by deleting the collection or queueing its videos). -->
          {#if !selecting}
          <div class="absolute right-2 top-2 z-10 flex gap-1.5 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100">
            {#if !sealed && !c.is_group && ((c.video_count ?? 0) >= 1 || (c.image_count ?? 0) >= 1)}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-[var(--media-control-border)] bg-[var(--media-control-bg)] text-[var(--media-control-ink)] backdrop-blur-sm transition hover:border-[var(--media-control-border-hover)] hover:bg-[var(--media-control-bg-hover)]"
                title="Add this collection's videos and photos to the montage queue" aria-label="Add this collection's videos and photos to the montage queue" onclick={() => onqueue(c)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
              </button>
            {/if}
            {#if activeGroup && !sealed && !c.is_group && (c.cover || c.covers?.length)}
              <!-- Inside a group: pin this member's covers as the group card's cover
                   (unpinned, the card shows the newest images across the group). -->
              {@const pinned = activePin?.id === c.id}
              <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border bg-[var(--media-control-bg)] backdrop-blur-sm transition hover:border-[var(--accent)] hover:text-[var(--accent)] {pinned ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--media-control-border)] text-[var(--media-control-ink)]'}"
                title={pinned ? 'Group cover — click to show the newest images instead' : 'Use as the group cover'}
                aria-label={pinned ? 'Unpin group cover' : 'Use as group cover'} aria-pressed={pinned}
                onclick={() => setGroupCover(c.id, !pinned)}>
                <svg viewBox="0 0 24 24" class="h-4 w-4" fill={pinned ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>
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
          {/if}

          <!-- Primary actions: bottom-right, only when accessible and has videos. "Add to
               Play Queue" and "Shuffle" (compact, secondary) sit to the LEFT of the accent
               "Play" — queue these videos onto the cross-library Play Queue, or play this
               collection now in order / at random. An icon rather than a caret menu: the card
               is overflow-hidden for its mosaic, which would clip a dropdown panel. -->
          {#if !selecting && !sealed && !c.is_group && c.video_count}
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
          {:else if sealed && !selecting}
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

{#if movePrompt}
  <!-- Select mode → Move to group: pick an existing group or type a name for a new one. -->
  {@const typed = movePrompt.name.trim()}
  {@const exact = groupEntries.find((g) => g.group_key === groupKey(typed))}
  {@const choices = groupEntries
    .filter((g) => !isSealed(g) && (!typed || g.name.toLowerCase().includes(typed.toLowerCase())))
    .sort((a, b) => a.name.localeCompare(b.name))}
  <Modal onclose={() => (movePrompt = null)} ariaLabel="Move to a group" z="z-[70]" panelClass="panel flex max-h-[80dvh] w-full max-w-sm flex-col rounded-2xl p-6">
    <h2 class="mb-1 text-lg font-bold">Move {pickedList.length} collection{pickedList.length === 1 ? '' : 's'} to a group</h2>
    <p class="mb-3 text-sm leading-relaxed text-muted">Pick a group, or type a name to start a new one. Sub-collections come along with their parent.</p>
    <input use:autofocus bind:value={movePrompt.name} placeholder="Group name" maxlength="60" aria-label="Group name"
      onkeydown={(e) => { if (e.key === 'Enter' && typed) confirmMove(typed); }}
      class="mb-3 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
    {#if choices.length}
      <ul class="mb-4 min-h-0 flex-1 space-y-1 overflow-y-auto">
        {#each choices as g (g.group_key)}
          <li>
            <button type="button" onclick={() => confirmMove(g.name)}
              class="flex w-full items-center justify-between gap-3 rounded-lg border border-line px-3 py-2 text-left text-sm font-semibold transition hover:border-[var(--accent)]">
              <span class="min-w-0 truncate">{g.name}</span>
              <span class="shrink-0 text-xs font-normal text-muted">{g.collection_count} collection{g.collection_count === 1 ? '' : 's'}</span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}
    <div class="flex gap-2">
      <Button variant="secondary" size="lg" class="flex-1" onclick={() => (movePrompt = null)}>Cancel</Button>
      <Button variant="primary" size="lg" class="min-w-0 flex-1 truncate" disabled={!typed || (exact && isSealed(exact))} onclick={() => confirmMove(typed)}>
        {exact ? 'Move' : 'Create group'}
      </Button>
    </div>
  </Modal>
{/if}

{#if confirmUngroup}
  <ConfirmDialog danger={false} title={`Ungroup “${activeGroup}”?`}
    message={`Its ${activeMembers.length} collection${activeMembers.length === 1 ? '' : 's'} go back to the Library page just as they are — nothing is deleted.`}
    confirmLabel="Ungroup"
    onconfirm={ungroupActive}
    oncancel={() => (confirmUngroup = false)} />
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

  /* Select mode: a picked card gets a solid accent ring. */
  .picked {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent);
  }

  /* Card under a dragged collection: accent ring + glow says "drop to group". */
  .drop-target {
    border-color: var(--accent);
    box-shadow:
      0 0 0 2px color-mix(in srgb, var(--accent) 55%, transparent),
      0 0 26px color-mix(in srgb, var(--accent) 32%, transparent);
  }
</style>
