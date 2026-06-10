<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    savedResponses, addSavedResponse, removeSavedResponse, updateSavedResponse, setSavedResponses, importLibraryIntoSaved
  } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import { auditPromptLabels, autotagPrompt, enhancePrompt, importLibraryPrompts } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import SearchField from './SearchField.svelte';

  let { llmReady = false, onRemix = null } = $props(); // llmReady -> auto-tag affordances; onRemix -> load a saved prompt back into the Compose composer

  // A library of starred Prompt Studio outputs (Scene beats, Freeform items, Variations) plus any
  // prompt you add by hand here. Server-persisted via state.js, so they survive reloads and follow
  // you across devices. Organised two ways: one FOLDER per item (the primary bucket) and any number
  // of cross-cutting TAGS. Drag-to-reorder works in a plain folder view (no tag/search filter active).
  const ALL = '__all';
  const UNFILED = '__unfiled';
  const ENHANCED_FOLDER = 'Enhanced';
  const LAST_FOLDER_KEY = 'ga.savedResponses.activeFolder';
  const DIALOGUE_LEVELS = [
    { key: 'normal', label: 'Natural' },
    { key: 'dirtier', label: 'Suggestive' },
    { key: 'filthier', label: 'Unfiltered' }
  ];
  let q = $state('');
  let draft = $state('');
  let activeFolder = $state(ALL);
  let activeTags = $state([]); // selected tag filters — match-ANY (OR)
  let tagCloudExpanded = $state(false);
  let extraFolders = $state([]); // folders created here but not yet holding an item (vanish on reload)
  let defaultFolderChosen = $state(false);

  const isResponse = (r) => r && typeof r === 'object';
  const items = $derived(($savedResponses || []).filter(isResponse));
  const rowId = (r, fallback = '') => String(r?.id || fallback);
  const textOf = (r) => String(r?.text || '');
  const tagsOf = (r) => (Array.isArray(r?.tags) ? r.tags : []);
  const folderOf = (r) => String(r?.folder || '').trim();

  // Distinct folders that actually contain items, with counts.
  const folderCounts = $derived.by(() => {
    const m = new Map();
    for (const r of items) { const f = folderOf(r); if (f) m.set(f, (m.get(f) || 0) + 1); }
    return m;
  });
  // Folder list for the rail / pickers = folders in use ∪ just-created empties, sorted.
  const folderNames = $derived.by(() => {
    const names = new Set(folderCounts.keys());
    for (const e of extraFolders) names.add(e);
    return [...names].sort((a, b) => a.localeCompare(b));
  });
  const unfiledCount = $derived(items.filter((r) => !folderOf(r)).length);
  const largestFolder = $derived.by(() => {
    let best = '';
    let bestCount = 0;
    for (const [name, count] of folderCounts) {
      if (count > bestCount) { best = name; bestCount = count; }
    }
    return best;
  });

  // Full tag vocabulary across ALL saved responses — used only for auto-tag/audit reuse
  // suggestions (so the model reuses existing labels regardless of the current folder).
  const allTags = $derived.by(() => {
    const m = new Map();
    for (const r of items) for (const t of tagsOf(r)) m.set(t, (m.get(t) || 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([name, count]) => ({ name, count }));
  });

  // Filter pipeline: folder → tags (OR) → text search. Array order is preserved throughout.
  const inFolder = $derived(items.filter((r) => {
    if (activeFolder === ALL) return true;
    if (activeFolder === UNFILED) return !folderOf(r);
    return folderOf(r) === activeFolder;
  }));
  const shown = $derived(inFolder.filter((r) => {
    if (activeTags.length && !activeTags.some((t) => tagsOf(r).includes(t))) return false;
    if (q.trim() && !textOf(r).toLowerCase().includes(q.trim().toLowerCase())) return false;
    return true;
  }));

  // The tag-filter cloud reflects only the tags present in the CURRENT folder, with folder-scoped
  // counts (in "All" it spans everything). Built from inFolder — the folder filter only, not the
  // active tag/search — so selecting one tag never hides the others you might also want to toggle.
  const folderTags = $derived.by(() => {
    const m = new Map();
    for (const r of inFolder) for (const t of tagsOf(r)) m.set(t, (m.get(t) || 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([name, count]) => ({ name, count }));
  });
  const selectedTagItems = $derived(folderTags.filter((t) => activeTags.includes(t.name)));
  const availableTagItems = $derived(folderTags.filter((t) => !activeTags.includes(t.name)));
  const hasCollapsibleTags = $derived(availableTagItems.length > 14);

  // Reordering is only unambiguous when the view isn't sparsely filtered — i.e. no tag filter and no
  // search. A single folder (or All) is fine: the visible rows map cleanly back onto their slots.
  const canReorder = $derived(activeTags.length === 0 && !q.trim());
  const activeFolderLabel = $derived(activeFolder === ALL ? 'All' : activeFolder === UNFILED ? 'Unfiled' : activeFolder);

  function chooseDefaultFolder() {
    if (unfiledCount) return UNFILED;
    try {
      const saved = localStorage.getItem(LAST_FOLDER_KEY);
      if (saved === UNFILED && unfiledCount) return UNFILED;
      if (saved && folderNames.includes(saved)) return saved;
    } catch {}
    return largestFolder || ALL;
  }

  function selectFolder(folder) {
    activeFolder = folder;
    activeTags = [];
    try { localStorage.setItem(LAST_FOLDER_KEY, folder); } catch {}
  }

  $effect(() => {
    if (defaultFolderChosen || !items.length) return;
    activeFolder = chooseDefaultFolder();
    defaultFolderChosen = true;
  });
  $effect(() => {
    if (!defaultFolderChosen || activeFolder === ALL || activeFolder === UNFILED) return;
    if (!folderNames.includes(activeFolder)) activeFolder = chooseDefaultFolder();
  });

  // Incremental rendering — only the first `visibleCount` rows are in the DOM; a sentinel below the
  // list grows the cap as it scrolls into view (same pattern as the browse grid). Keeps a 500-item
  // library light. The cap resets whenever the filter changes so a new view starts at the top.
  const PAGE = 60;
  let visibleCount = $state(PAGE);
  const pageItems = $derived(shown.slice(0, visibleCount));
  let sentinel = $state(null);
  $effect(() => { activeFolder; activeTags; q; visibleCount = PAGE; }); // reset on filter change
  $effect(() => {
    if (!sentinel) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && visibleCount < shown.length) {
        visibleCount = Math.min(visibleCount + PAGE, shown.length);
      }
    }, { rootMargin: '600px' });
    io.observe(sentinel);
    return () => io.disconnect();
  });

  async function copy(t) {
    const ok = await copyText(t);
    toast(ok ? 'Copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }
  function add() {
    const folder = activeFolder === ALL || activeFolder === UNFILED ? '' : activeFolder;
    if (addSavedResponse(draft, { folder })) draft = ''; // clear only when it actually saved (not on dupe/empty)
  }
  // Ctrl/⌘+Enter saves without reaching for the button.
  function onKey(e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); add(); }
  }

  // --- Import library prompts into Saved (server-side merge: backed up, deduped, additive) ----
  let importPreview = $state(null); // { missing, library_unique, saved } | null
  let importing = $state(false);
  let confirmImport = $state(false);
  async function refreshImportPreview() {
    try { importPreview = await importLibraryPrompts({ preview: true }); }
    catch { importPreview = null; }
  }
  onMount(refreshImportPreview);
  async function doImport() {
    if (importing) return;
    importing = true;
    try {
      const d = await importLibraryIntoSaved({ folder: 'Library' });
      confirmImport = false;
      if (d.added) {
        toast(`Added ${d.added} prompt${d.added === 1 ? '' : 's'} from your library${d.backup ? ' · previous list backed up' : ''}`, { type: 'success' });
        selectFolder('Library');
      } else {
        toast('Your library prompts are already all saved', { type: 'info' });
      }
      await refreshImportPreview();
    } catch (e) {
      toast(e.message || 'Import failed', { type: 'error' });
    } finally {
      importing = false;
    }
  }

  function toggleTagFilter(t) {
    activeTags = activeTags.includes(t) ? activeTags.filter((x) => x !== t) : [...activeTags, t];
  }
  function showRelatedTag(t) {
    q = '';
    activeFolder = ALL;
    activeTags = [t];
    try { localStorage.setItem(LAST_FOLDER_KEY, ALL); } catch {}
  }

  // Inline folder creation — mirrors the `+ tag` editor and persona cards (no native prompt).
  let newFolderOpen = $state(false);
  let newFolderDraft = $state('');
  function openNewFolder() { newFolderOpen = true; newFolderDraft = ''; }
  function commitNewFolder() {
    const name = newFolderDraft.trim().slice(0, 40);
    newFolderDraft = '';
    newFolderOpen = false;
    if (!name) return;
    if (!folderNames.includes(name)) extraFolders = [...extraFolders, name];
    activeFolder = name;
  }
  function onNewFolderKey(e) {
    if (e.key === 'Enter') { e.preventDefault(); commitNewFolder(); }
    else if (e.key === 'Escape') { newFolderOpen = false; newFolderDraft = ''; }
  }

  // Inline folder rename — same pattern as creation. Renaming retags every item in the folder in one
  // write; renaming onto an existing folder merges into it. Empty (item-less) folders rename too.
  let renamingFolder = $state(null);
  let renameDraft = $state('');
  function startRename(f) { renamingFolder = f; renameDraft = f; }
  function commitRename() {
    const from = renamingFolder;
    const to = renameDraft.trim().slice(0, 40);
    renamingFolder = null;
    renameDraft = '';
    if (!from || !to || to === from) return;
    let touched = 0;
    const next = items.map((r) => {
      if (folderOf(r) !== from) return r;
      touched++;
      return { ...r, folder: to };
    });
    if (touched) setSavedResponses(next);
    // Keep an empty folder visible after rename (it has no items to carry the name).
    const stillHasItems = next.some((r) => folderOf(r) === to);
    extraFolders = extraFolders.filter((e) => e !== from && e !== to);
    if (!stillHasItems) extraFolders = [...extraFolders, to];
    if (activeFolder === from) selectFolder(to);
    else { try { if (localStorage.getItem(LAST_FOLDER_KEY) === from) localStorage.setItem(LAST_FOLDER_KEY, to); } catch {} }
  }
  function onRenameKey(e) {
    if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
    else if (e.key === 'Escape') { renamingFolder = null; renameDraft = ''; }
  }
  function moveToFolder(r, folder) {
    updateSavedResponse(rowId(r), { folder });
  }

  // --- Per-item tag editing (one card open at a time) -----------------------
  let tagEditId = $state(null);
  let tagDraft = $state('');
  function startTag(id) { tagEditId = id; tagDraft = ''; }
  function commitTag(r) {
    const t = tagDraft.trim().toLowerCase().replace(/\s+/g, '-').slice(0, 24);
    if (t && !tagsOf(r).includes(t)) updateSavedResponse(rowId(r), { tags: [...tagsOf(r), t] });
    tagDraft = '';
    tagEditId = null;
  }
  function onTagKey(e, r) {
    if (e.key === 'Enter') { e.preventDefault(); commitTag(r); }
    else if (e.key === 'Escape') { tagEditId = null; tagDraft = ''; }
  }
  function removeTag(r, t) {
    updateSavedResponse(rowId(r), { tags: tagsOf(r).filter((x) => x !== t) });
  }
  // Existing tags (most-used first) not already on this item, matching what's typed — click to reuse
  // instead of retyping (also prevents near-duplicate tags from typos).
  function tagSuggestions(r) {
    const have = new Set(tagsOf(r));
    const d = tagDraft.trim().toLowerCase();
    return allTags.filter((t) => !have.has(t.name) && (!d || t.name.includes(d))).slice(0, 8).map((t) => t.name);
  }
  function applyTag(r, name) {
    if (!tagsOf(r).includes(name)) updateSavedResponse(rowId(r), { tags: [...tagsOf(r), name] });
    tagDraft = ''; // keep the editor open (input keeps focus) so several tags can be added quickly
  }
  function focusOnMount(node) { node.focus(); }

  // --- Auto-tag (local LLM suggests a folder + tags; you review, then accept) ----------------
  let suggest = $state({}); // id -> { loading, folder, tags } — model suggestions awaiting review
  let bulkRunning = $state(false);
  let bulkProgress = $state({ done: 0, total: 0 });
  let auditRunning = $state(false);
  let auditProgress = $state({ done: 0, total: 0 });
  function hasSuggestion(s) {
    if (!s || s.loading) return false;
    return Boolean((s.tags || []).length || (s.remove_tags || []).length || s.folder);
  }
  const suggestCount = $derived(Object.values(suggest).filter(hasSuggestion).length);

  function dropSuggest(id) { const { [id]: _omit, ...rest } = suggest; suggest = rest; }
  function clearIfEmpty(id) {
    const s = suggest[id];
    if (s && !(s.tags || []).length && !(s.remove_tags || []).length && !s.folder) dropSuggest(id);
  }

  // Fetch suggestions for one item — filters out labels it already has so only NEW ones surface.
  async function autotag(r) {
    const id = rowId(r);
    suggest = { ...suggest, [id]: { loading: true, folder: '', tags: [] } };
    try {
      const res = await autotagPrompt(textOf(r), { folders: folderNames, tags: allTags.map((t) => t.name) });
      const have = new Set(tagsOf(r));
      const tags = (res.tags || []).filter((t) => !have.has(t));
      const folder = res.folder && res.folder !== folderOf(r) ? res.folder : '';
      suggest = { ...suggest, [id]: { loading: false, folder, tags } };
      if (!tags.length && !folder) { dropSuggest(id); return false; }
      return true;
    } catch (e) {
      dropSuggest(id);
      toast(e.message || 'Auto-tag failed', { type: 'error' });
      return false;
    }
  }
  async function auditLabels(r) {
    const id = rowId(r);
    suggest = { ...suggest, [id]: { loading: true, folder: '', tags: [], remove_tags: [], reason: '', audit: true } };
    try {
      const res = await auditPromptLabels(textOf(r), {
        folder: folderOf(r),
        current_tags: tagsOf(r),
        folders: folderNames,
        tags: allTags.map((t) => t.name)
      });
      const have = new Set(tagsOf(r));
      const tags = (res.tags || []).filter((t) => !have.has(t));
      const remove_tags = (res.remove_tags || []).filter((t) => have.has(t));
      const folder = res.folder && res.folder !== folderOf(r) ? res.folder : '';
      suggest = { ...suggest, [id]: { loading: false, folder, tags, remove_tags, reason: res.reason || '', audit: true } };
      if (!tags.length && !remove_tags.length && !folder) { dropSuggest(id); return false; }
      return true;
    } catch (e) {
      dropSuggest(id);
      toast(e.message || 'Label audit failed', { type: 'error' });
      return false;
    }
  }
  function acceptTag(r, t) {
    const id = rowId(r);
    if (!tagsOf(r).includes(t)) updateSavedResponse(id, { tags: [...tagsOf(r), t] });
    const s = suggest[id]; if (!s) return;
    suggest = { ...suggest, [id]: { ...s, tags: (s.tags || []).filter((x) => x !== t) } };
    clearIfEmpty(id);
  }
  function acceptRemoveTag(r, t) {
    const id = rowId(r);
    updateSavedResponse(id, { tags: tagsOf(r).filter((x) => x !== t) });
    const s = suggest[id]; if (!s) return;
    suggest = { ...suggest, [id]: { ...s, remove_tags: (s.remove_tags || []).filter((x) => x !== t) } };
    clearIfEmpty(id);
  }
  function acceptFolder(r) {
    const id = rowId(r);
    const s = suggest[id]; if (!s?.folder) return;
    moveToFolder(r, s.folder);
    suggest = { ...suggest, [id]: { ...s, folder: '' } };
    clearIfEmpty(id);
  }
  function acceptOne(r) {
    const id = rowId(r);
    const s = suggest[id]; if (!s) return;
    const remove = new Set(s.remove_tags || []);
    const tags = tagsOf(r).filter((t) => !remove.has(t));
    for (const t of s.tags || []) if (!tags.includes(t)) tags.push(t);
    updateSavedResponse(id, s.folder ? { tags, folder: s.folder } : { tags });
    dropSuggest(id);
  }
  // Accept every outstanding suggestion in ONE write (not one POST per item).
  function acceptAll() {
    const map = suggest;
    const next = items.map((r) => {
      const s = map[rowId(r)];
      if (!s || s.loading) return r;
      const remove = new Set(s.remove_tags || []);
      const tags = tagsOf(r).filter((t) => !remove.has(t));
      for (const t of s.tags || []) if (!tags.includes(t)) tags.push(t);
      return { ...r, tags, folder: s.folder || folderOf(r) };
    });
    setSavedResponses(next);
    suggest = {};
  }
  function dismissAll() { suggest = {}; }

  // Bulk: walk every untagged item in the current view, fetching suggestions sequentially (gentle
  // on Ollama). Results queue up as review chips; accept individually or via "Accept all".
  async function autotagBulk() {
    const targets = shown.filter((r) => !tagsOf(r).length && !suggest[rowId(r)]);
    if (!targets.length) { toast('Nothing untagged to tag here', { type: 'info' }); return; }
    bulkRunning = true;
    bulkProgress = { done: 0, total: targets.length };
    for (const r of targets) {
      if (!bulkRunning) break; // Stop pressed
      await autotag(r);
      bulkProgress = { ...bulkProgress, done: bulkProgress.done + 1 };
    }
    bulkRunning = false;
  }
  async function auditBulk() {
    const targets = pageItems.filter((r) => !suggest[rowId(r)]);
    if (!targets.length) { toast('Nothing visible to audit here', { type: 'info' }); return; }
    auditRunning = true;
    auditProgress = { done: 0, total: targets.length };
    for (const r of targets) {
      if (!auditRunning) break;
      await auditLabels(r);
      auditProgress = { ...auditProgress, done: auditProgress.done + 1 };
    }
    auditRunning = false;
  }

  // --- Enhance prompt (local LLM expands detail, then user saves a reviewed copy) --------------
  let enhance = $state({
    open: false,
    loading: false,
    source: '',
    text: '',
    level: '',
    dialogueOnly: false,
    model: ''
  });
  const canSaveEnhanced = $derived(Boolean(enhance.text.trim()));

  async function runEnhance(level = enhance.level || 'normal', source = enhance.source, dialogueOnly = enhance.dialogueOnly) {
    if (!source.trim()) return;
    enhance = { ...enhance, open: true, loading: true, level, source, dialogueOnly };
    try {
      const res = await enhancePrompt(source, { dialogue_level: level, dialogue_only: dialogueOnly });
      enhance = {
        ...enhance,
        loading: false,
        level: res.dialogue_level || level,
        dialogueOnly: Boolean(res.dialogue_only),
        text: res.prompt || '',
        model: res.model || ''
      };
      if (!enhance.text.trim()) toast('The model returned nothing usable — try again.', { type: 'error' });
    } catch (e) {
      enhance = { ...enhance, loading: false };
      toast(e.message || 'Enhance failed', { type: 'error' });
    }
  }
  function openEnhanceSource(source) {
    if (!source.trim()) return;
    enhance = { open: true, loading: false, source, text: '', level: '', dialogueOnly: false, model: '' };
  }
  function openEnhance(r) {
    openEnhanceSource(textOf(r));
  }
  function openDraftEnhance() {
    openEnhanceSource(draft);
  }
  function closeEnhance() {
    if (enhance.loading) return;
    enhance = { open: false, loading: false, source: '', text: '', level: '', dialogueOnly: false, model: '' };
  }
  function chooseEnhanceLevel(level) {
    if (enhance.loading) return;
    runEnhance(level);
  }
  function toggleDialogueOnly() {
    if (enhance.loading) return;
    const next = !enhance.dialogueOnly;
    enhance = { ...enhance, dialogueOnly: next, text: '', model: '' };
  }
  async function copyEnhanced() {
    if (!enhance.text.trim()) return;
    const ok = await copyText(enhance.text);
    toast(ok ? 'Enhanced prompt copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }
  function saveEnhanced() {
    if (!canSaveEnhanced) return;
    if (addSavedResponse(enhance.text, { folder: ENHANCED_FOLDER })) {
      activeFolder = ENHANCED_FOLDER;
      try { localStorage.setItem(LAST_FOLDER_KEY, ENHANCED_FOLDER); } catch {}
      closeEnhance();
    }
  }

  // Two-step delete: first click arms "Sure?", second confirms. Auto-disarms after a few seconds.
  // Inline (no modal) — deletes here are low-stakes (the text is re-addable) and per-row.
  let confirmDeleteId = $state(null);
  let confirmTimer;
  function askDelete(id) {
    confirmDeleteId = id;
    clearTimeout(confirmTimer);
    confirmTimer = setTimeout(() => (confirmDeleteId = null), 3000);
  }
  function confirmDelete(id) {
    clearTimeout(confirmTimer);
    confirmDeleteId = null;
    removeSavedResponse(id);
  }
  onDestroy(() => clearTimeout(confirmTimer));

  // --- Drag-to-reorder (native HTML5 DnD; the grip is the drag source) -------
  let dragId = $state(null);
  function onDragStart(e, id) {
    dragId = id;
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', id); } catch {} // Firefox needs payload to start a drag
  }
  function onDragOver(e) { if (canReorder && dragId) e.preventDefault(); } // allow drop
  function onDrop(e, overId) {
    e.preventDefault();
    const from = dragId;
    dragId = null;
    if (!from || from === overId) return;
    const order = shown.slice();
    const i = order.findIndex((r) => rowId(r) === from);
    const j = order.findIndex((r) => rowId(r) === overId);
    if (i < 0 || j < 0) return;
    const [moved] = order.splice(i, 1);
    order.splice(j, 0, moved);
    persistReorder(order);
  }
  // Write a reordered visible list back into the global array. In a folder view, only the slots that
  // belong to that folder are rewritten — items outside it keep their positions.
  function persistReorder(newShown) {
    if (activeFolder === ALL) { setSavedResponses(newShown); return; }
    const inView = (r) => (activeFolder === UNFILED ? !folderOf(r) : folderOf(r) === activeFolder);
    const next = items.slice();
    const slots = [];
    for (let k = 0; k < next.length; k++) if (inView(next[k])) slots.push(k);
    newShown.forEach((r, k) => { next[slots[k]] = r; });
    setSavedResponses(next);
  }

  // Long responses are clamped to a few lines and roll open on click — the same reveal the
  // Playlist Editor uses for prompts. Short ones render in full with no expand affordance.
  let expanded = $state({}); // id -> bool
  function toggleExpand(id) { expanded = { ...expanded, [id]: !expanded[id] }; }
  const needsClamp = (t) => {
    const s = String(t || '');
    return s.length > 360 || (s.match(/\n/g)?.length || 0) >= 6;
  };

  // CSS can't transition height:auto, so animate max-height between the collapsed floor and the
  // measured scrollHeight, then release to `none` when open so it can reflow freely.
  const RESP_COLLAPSED = 132; // px ≈ 6 lines at text-sm / leading-relaxed
  function reveal(node, open) {
    node.style.overflow = 'hidden';
    node.style.maxHeight = open ? 'none' : RESP_COLLAPSED + 'px';
    let cur = !!open;
    return {
      update(next) {
        next = !!next;
        if (next === cur) return;
        cur = next;
        node.style.maxHeight = node.offsetHeight + 'px'; // pin current height
        void node.offsetHeight;                          // force reflow
        requestAnimationFrame(() => {
          node.style.maxHeight = (next ? node.scrollHeight : RESP_COLLAPSED) + 'px';
        });
        const done = () => {
          if (cur) node.style.maxHeight = 'none';
          node.removeEventListener('transitionend', done);
        };
        node.addEventListener('transitionend', done);
      }
    };
  }
</script>

<div class="mx-auto grid w-full max-w-7xl grid-cols-1 gap-3 md:grid-cols-[12.5rem_minmax(0,1fr)]">
  <!-- Folder rail -->
  <aside class="rounded-xl border border-line bg-[var(--surface)]/35 p-2 md:sticky md:top-20 md:self-start">
    <div class="mb-2 flex items-center justify-between px-1 text-[0.625rem] font-bold uppercase tracking-wider text-muted">
      <span>Folders</span>
      <span class="max-w-[11rem] truncate md:hidden">{activeFolderLabel}</span>
    </div>
    <ul class="flex flex-row gap-1 overflow-x-auto pb-1 md:flex-col md:gap-0.5 md:overflow-visible md:pb-0">
      {#snippet folderBtn(key, label, count)}
        <li class="min-w-[8.5rem] max-w-[12rem] shrink-0 md:w-full md:min-w-0 md:max-w-none">
          <button type="button" onclick={() => selectFolder(key)} title={label}
            class="flex min-w-0 w-full items-center justify-between gap-2 rounded-lg border px-2.5 py-1.5 text-left text-[0.8125rem] font-semibold transition {activeFolder === key ? 'border-line border-l-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] text-ink shadow-sm' : 'border-transparent text-muted hover:bg-[var(--surface-2)] hover:text-ink'}">
            <span class="min-w-0 flex-1 truncate">{label}</span>
            <span class="shrink-0 text-xs opacity-70">{count}</span>
          </button>
        </li>
      {/snippet}
      {@render folderBtn(ALL, 'All', items.length)}
      {#each folderNames as f (f)}
        <li class="min-w-[8.5rem] max-w-[12rem] shrink-0 md:w-full md:min-w-0 md:max-w-none">
          {#if renamingFolder === f}
            <input use:focusOnMount bind:value={renameDraft} onkeydown={onRenameKey} onblur={commitRename}
              maxlength="40" aria-label={`Rename folder ${f}`}
              class="w-full rounded-lg border border-[var(--accent)] bg-[var(--surface)] px-2.5 py-1.5 text-[0.8125rem] font-semibold text-ink outline-none" />
          {:else}
            <div class="group flex items-stretch gap-0.5">
              <button type="button" onclick={() => selectFolder(f)} ondblclick={() => startRename(f)} title={f}
                class="flex min-w-0 flex-1 items-center justify-between gap-2 rounded-lg border px-2.5 py-1.5 text-left text-[0.8125rem] font-semibold transition {activeFolder === f ? 'border-line border-l-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] text-ink shadow-sm' : 'border-transparent text-muted hover:bg-[var(--surface-2)] hover:text-ink'}">
                <span class="min-w-0 flex-1 truncate">{f}</span>
                <span class="shrink-0 text-xs opacity-70">{folderCounts.get(f) || 0}</span>
              </button>
              <button type="button" onclick={() => startRename(f)} aria-label={`Rename folder ${f}`} title="Rename folder"
                class="grid w-6 shrink-0 place-items-center rounded-md text-xs text-muted opacity-60 transition hover:bg-[var(--surface-2)] hover:text-ink focus:opacity-100 group-hover:opacity-100">✎</button>
            </div>
          {/if}
        </li>
      {/each}
      {#if unfiledCount}
        {@render folderBtn(UNFILED, 'Unfiled', unfiledCount)}
      {/if}
      <li class="min-w-[8.5rem] max-w-[12rem] shrink-0 md:w-full md:min-w-0 md:max-w-none">
        {#if newFolderOpen}
          <input use:focusOnMount bind:value={newFolderDraft} onkeydown={onNewFolderKey} onblur={commitNewFolder}
            maxlength="40" placeholder="Folder name…"
            class="w-full rounded-lg border border-[var(--accent)] bg-[var(--surface)] px-2.5 py-1.5 text-[0.8125rem] font-semibold text-ink outline-none placeholder:text-muted" />
        {:else}
          <button type="button" onclick={openNewFolder}
            class="w-full rounded-lg border border-dashed border-line px-2.5 py-1.5 text-left text-[0.8125rem] font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink">+ New folder</button>
        {/if}
      </li>
    </ul>
  </aside>

  <!-- Content -->
  <div class="min-w-0 flex-1">
    <div class="mb-3 flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
      <div class="min-w-0">
        <div class="text-sm font-semibold text-ink">
          {shown.length} saved {shown.length === 1 ? 'response' : 'responses'}
          {#if activeFolder !== ALL}<span class="font-normal text-muted">· in {activeFolderLabel}</span>{/if}
        </div>
        {#if pageItems.length < shown.length || activeTags.length}
          <div class="mt-0.5 text-[0.6875rem] text-muted">
            {#if pageItems.length < shown.length}showing {pageItems.length}{/if}
            {#if pageItems.length < shown.length && activeTags.length}<span> · </span>{/if}
            {#if activeTags.length}{activeTags.length} tag {activeTags.length === 1 ? 'filter' : 'filters'} active{/if}
          </div>
        {/if}
      </div>

      <div class="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center lg:flex-1">
        <SearchField bind:value={q} placeholder="Search saved responses…" ariaLabel="saved responses"
          wrapperClass="w-full sm:min-w-0 sm:flex-1"
          inputClass="rounded-full border border-line bg-[var(--surface-2)] py-2 pl-3.5 pr-10 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />

        <!-- All action buttons sit in one no-wrap, no-shrink cluster so they always stay on a
             single line; the search above flexes to give up width instead of letting these wrap. -->
        <div class="flex shrink-0 flex-nowrap items-center gap-2">
          {#if importPreview && importPreview.missing > 0}
            <button type="button" onclick={() => (confirmImport = true)} disabled={importing}
              title="Add prompts from your media library that aren't saved yet (your list is backed up first)"
              class="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-line px-3 py-1.5 text-xs font-semibold transition hover:border-[var(--accent)] disabled:opacity-50">
              <span aria-hidden="true">⇪</span> Import {importPreview.missing} from library
            </button>
          {/if}

          {#if llmReady}
            {#if bulkRunning}
              <span class="whitespace-nowrap text-xs font-semibold text-[var(--accent)]">Auto-tagging… {bulkProgress.done}/{bulkProgress.total}</span>
              <button type="button" onclick={() => (bulkRunning = false)}
                class="shrink-0 whitespace-nowrap rounded-md border border-line px-2.5 py-1 text-xs font-semibold transition hover:border-[var(--danger)]">Stop</button>
            {:else}
              <button type="button" onclick={autotagBulk} title="Suggest tags & a folder for every untagged prompt in this view"
                class="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-line px-3 py-1.5 text-xs font-semibold transition hover:border-[var(--accent)]"><span aria-hidden="true">✦</span> Auto-tag</button>
            {/if}
            {#if auditRunning}
              <span class="whitespace-nowrap text-xs font-semibold text-[var(--accent)]">Auditing… {auditProgress.done}/{auditProgress.total}</span>
              <button type="button" onclick={() => (auditRunning = false)}
                class="shrink-0 whitespace-nowrap rounded-md border border-line px-2.5 py-1 text-xs font-semibold transition hover:border-[var(--danger)]">Stop</button>
            {:else}
              <button type="button" onclick={auditBulk} title="Review visible prompts for overbroad folders/tags and suggest corrections"
                class="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-line px-3 py-1.5 text-xs font-semibold transition hover:border-[var(--accent)]"><span aria-hidden="true">◎</span> Audit labels</button>
            {/if}
            {#if suggestCount}
              <span class="whitespace-nowrap text-xs text-muted">{suggestCount} suggested</span>
              <button type="button" onclick={acceptAll} class="shrink-0 whitespace-nowrap rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-bold text-[var(--on-accent)] transition">Accept all</button>
              <button type="button" onclick={dismissAll} class="shrink-0 whitespace-nowrap text-xs text-muted transition hover:text-ink">Dismiss all</button>
            {/if}
          {/if}
        </div>
      </div>
    </div>

    <!-- Manually add a prompt straight to the library (no generation needed). -->
    <div class="mb-3 rounded-lg border border-line bg-[var(--surface)]/70 p-2.5 shadow-sm">
      <textarea bind:value={draft} onkeydown={onKey} rows="2" placeholder="Add a prompt by hand…"
        class="w-full resize-y rounded-md border border-line bg-[var(--surface-2)]/70 px-3 py-2 text-sm leading-relaxed text-ink outline-none placeholder:text-muted focus:border-[var(--accent)]"></textarea>
      <div class="mt-2 flex items-center justify-between gap-2">
        <span class="text-[0.6875rem] text-muted">
          ⌘/Ctrl + Enter to save{#if activeFolder !== ALL && activeFolder !== UNFILED} · to <strong class="font-semibold text-ink">{activeFolder}</strong>{/if}
        </span>
        <div class="flex shrink-0 items-center gap-2">
          {#if llmReady}
            <button type="button" onclick={openDraftEnhance} disabled={!draft.trim() || enhance.loading}
              class="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1 text-xs font-semibold transition enabled:hover:border-[var(--accent)] disabled:opacity-40">
              <span aria-hidden="true">✧</span> AI Enhance
            </button>
          {/if}
          <button type="button" onclick={add} disabled={!draft.trim()}
            class="rounded-md border border-line px-3 py-1 text-xs font-semibold transition enabled:hover:border-[var(--accent)] disabled:opacity-40">+ Add prompt</button>
        </div>
      </div>
    </div>

    <!-- Tag filter (match ANY) — scoped to the active folder's tags -->
    {#if folderTags.length}
      <div class="mb-3 rounded-lg border border-line bg-[var(--surface)]/25 px-2.5 py-2">
        <div class="mb-1.5 flex items-center justify-between gap-2">
          <span class="text-[0.625rem] font-bold uppercase tracking-wider text-muted">Filter by tag</span>
          <div class="flex items-center gap-2">
            {#if activeTags.length}
              <button type="button" onclick={() => (activeTags = [])} class="text-xs font-semibold text-muted transition hover:text-ink">clear</button>
            {/if}
            {#if hasCollapsibleTags}
              <button type="button" onclick={() => (tagCloudExpanded = !tagCloudExpanded)}
                class="text-xs font-semibold text-[var(--accent)] transition hover:brightness-110">
                {tagCloudExpanded ? 'Hide tags' : `More tags (${availableTagItems.length})`}
              </button>
            {/if}
          </div>
        </div>

        {#if selectedTagItems.length}
          <div class="mb-1.5 flex flex-wrap items-center gap-1.5">
            {#each selectedTagItems as t (t.name)}
              <button type="button" onclick={() => toggleTagFilter(t.name)}
                class="rounded-full border border-transparent bg-[var(--accent)] px-2.5 py-0.5 text-xs font-semibold text-[var(--on-accent)] transition hover:brightness-110">#{t.name} <span class="opacity-70">{t.count}</span></button>
            {/each}
          </div>
        {/if}

        {#if availableTagItems.length}
          <div class:tag-cloud-collapsed={!tagCloudExpanded && hasCollapsibleTags} class="tag-cloud flex flex-wrap items-center gap-1.5">
            {#each availableTagItems as t (t.name)}
              <button type="button" onclick={() => toggleTagFilter(t.name)}
                class="rounded-full border border-line px-2.5 py-0.5 text-xs font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink">#{t.name} <span class="opacity-60">{t.count}</span></button>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    {#if shown.length === 0}
      <div class="rounded-xl border border-dashed border-line bg-[var(--surface)]/35 px-4 py-10 text-center">
        <div class="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-full border border-line text-lg text-muted">□</div>
        <h3 class="text-sm font-bold text-ink">
          {#if items.length === 0}
            No saved responses yet
          {:else if activeFolder !== ALL && inFolder.length === 0}
            {activeFolderLabel} is empty
          {:else}
            No matches in {activeFolderLabel}
          {/if}
        </h3>
        <p class="mx-auto mt-1 max-w-md text-sm text-muted">
          {#if items.length === 0}
            Add a prompt above, or save a result from Scene or Freeform.
          {:else if activeFolder !== ALL && inFolder.length === 0}
            Add a prompt above to place it here, or move an existing prompt into this folder.
          {:else}
            Clear search or tag filters to get back to the full folder.
          {/if}
        </p>
        {#if items.length === 0 && importPreview && importPreview.missing > 0}
          <div class="mt-4 flex justify-center">
            <button type="button" onclick={() => (confirmImport = true)} disabled={importing}
              class="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-bold text-[var(--on-accent)] transition hover:brightness-110 disabled:opacity-50">
              {importing ? 'Importing…' : `Import ${importPreview.missing} prompts from your library`}
            </button>
          </div>
        {/if}
        {#if q.trim() || activeTags.length}
          <div class="mt-4 flex justify-center gap-2">
            <button type="button" onclick={() => (q = '')}
              class="rounded-md border border-line px-3 py-1.5 text-xs font-semibold transition hover:border-[var(--accent)]">Clear search</button>
            <button type="button" onclick={() => (activeTags = [])}
              class="rounded-md border border-line px-3 py-1.5 text-xs font-semibold transition hover:border-[var(--accent)]">Clear tags</button>
          </div>
        {/if}
      </div>
    {:else}
      <ul class="grid grid-cols-1 gap-2 xl:grid-cols-2">
        {#each pageItems as r, rowIndex (rowId(r, rowIndex))}
          {@const id = rowId(r, rowIndex)}
          {@const text = textOf(r)}
          <li class="flex min-h-[8.75rem] flex-col rounded-lg border border-line bg-[var(--surface)]/70 p-2.5 shadow-sm transition hover:border-[color-mix(in_srgb,var(--line)_70%,var(--accent)_30%)] {dragId === id ? 'opacity-40' : ''}"
            ondragover={onDragOver} ondrop={(e) => onDrop(e, id)}>
            <div class="flex min-w-0 items-start gap-2">
              {#if canReorder}
                <span class="mt-0.5 cursor-grab select-none text-base leading-none text-muted transition hover:text-ink"
                  draggable="true" ondragstart={(e) => onDragStart(e, id)} ondragend={() => (dragId = null)}
                  role="button" tabindex="-1" aria-label="Drag to reorder" title="Drag to reorder">⠿</span>
              {/if}
              <div class="min-w-0 flex-1">
                {#if needsClamp(text)}
                  <button type="button" class="block w-full cursor-pointer text-left" aria-expanded={expanded[id] || false}
                    title={expanded[id] ? 'Collapse' : 'Expand full prompt'} onclick={() => toggleExpand(id)}>
                    <span class="resp-roll block whitespace-pre-wrap break-words text-sm leading-relaxed text-ink" use:reveal={expanded[id] || false}>{text}</span>
                    <span class="mt-1 inline-block text-xs font-semibold text-[var(--accent)]">{expanded[id] ? 'Show less' : 'Show more'}</span>
                  </button>
                {:else}
                  <p class="whitespace-pre-wrap break-words text-sm leading-relaxed text-ink">{text}</p>
                {/if}
              </div>

              <div class="flex shrink-0 flex-row items-center gap-1">
                {#if llmReady}
                  <button type="button" onclick={() => openEnhance(r)} disabled={enhance.loading} title="Enhance prompt"
                    class="inline-flex h-7 items-center gap-1 rounded-md border border-line px-2 text-[0.6875rem] font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink disabled:opacity-40">
                    <span aria-hidden="true">✧</span>Enhance
                  </button>
                  <button type="button" onclick={() => autotag(r)} disabled={suggest[id]?.loading} title="Suggest tags & folder"
                    class="inline-flex h-7 items-center gap-1 rounded-md border border-line px-2 text-[0.6875rem] font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink disabled:opacity-40">
                    <span aria-hidden="true">✦</span>{suggest[id]?.loading ? '...' : 'AI'}
                  </button>
                {/if}
                {#if onRemix}
                  <button type="button" onclick={() => onRemix(text)} aria-label="Edit in Compose" title="Load into the Compose composer to remix"
                    class="grid h-7 w-7 shrink-0 place-items-center rounded-md border border-line text-[0.8125rem] text-muted transition hover:border-[var(--accent)] hover:text-ink">✎</button>
                {/if}
                <button type="button" onclick={() => copy(text)} title="Copy prompt" class="h-7 rounded-md border border-line px-2 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">Copy</button>
                {#if confirmDeleteId === id}
                  <button type="button" onclick={() => confirmDelete(id)} title="Click to permanently delete"
                    class="h-7 rounded-md bg-[var(--danger)] px-2 text-[0.6875rem] font-bold text-[var(--on-accent)] transition hover:bg-[var(--danger-hover)]">Sure?</button>
                {:else}
                  <button type="button" onclick={() => askDelete(id)} title="Delete prompt"
                    class="h-7 rounded-md border border-line px-2 text-[0.6875rem] font-semibold text-[var(--danger)] transition hover:border-[var(--danger)]">Del</button>
                {/if}
              </div>
            </div>

            <div class="mt-auto pt-2">
              <!-- Tags + folder picker -->
              <div class="mt-2 flex flex-wrap items-center gap-1.5">
                {#each tagsOf(r) as t (t)}
                  <span class="inline-flex items-center overflow-hidden rounded-full border text-[0.6875rem] {activeTags.includes(t) ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line bg-[var(--surface)] text-muted'}">
                    <button type="button" onclick={() => showRelatedTag(t)}
                      title={`Show prompts tagged #${t}`}
                      class="px-2 py-0.5 text-left transition hover:brightness-110">
                      #{t}
                    </button>
                    <button type="button" onclick={() => removeTag(r, t)} aria-label={`Remove tag ${t}`}
                      class="border-l px-1.5 py-0.5 leading-none transition hover:text-[var(--danger)] {activeTags.includes(t) ? 'border-white/25' : 'border-line'}">×</button>
                  </span>
                {/each}
                {#if tagEditId === id}
                  <input use:focusOnMount bind:value={tagDraft} onkeydown={(e) => onTagKey(e, r)} onblur={() => commitTag(r)}
                    maxlength="24" placeholder="tag…"
                    class="w-24 rounded-full border border-[var(--accent)] bg-[var(--surface)] px-2 py-0.5 text-[0.6875rem] outline-none" />
                {:else}
                  <button type="button" onclick={() => startTag(id)}
                    class="rounded-full border border-dashed border-line px-2 py-0.5 text-[0.6875rem] font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink">+ tag</button>
                {/if}

                <select value={folderOf(r)} onchange={(e) => moveToFolder(r, e.currentTarget.value)}
                  title="Move to folder"
                  class="ml-auto max-w-[10rem] truncate rounded-md border border-line bg-[var(--surface)] px-2 py-0.5 text-[0.6875rem] text-muted outline-none transition hover:border-[var(--accent)] focus:border-[var(--accent)]">
                  <option value="">Unfiled</option>
                  {#each folderNames as f (f)}<option value={f}>{f}</option>{/each}
                </select>
              </div>

              <!-- Reuse an existing tag (autocomplete) while the tag editor is open. -->
              {#if tagEditId === id}
                {@const sugg = tagSuggestions(r)}
                {#if sugg.length}
                  <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <span class="text-[0.625rem] text-muted">reuse:</span>
                    {#each sugg as s (s)}
                      <button type="button" onpointerdown={(e) => { e.preventDefault(); applyTag(r, s); }}
                        class="rounded-full border border-line px-2 py-0.5 text-[0.6875rem] text-muted transition hover:border-[var(--accent)] hover:text-ink">#{s}</button>
                    {/each}
                  </div>
                {/if}
              {/if}

              <!-- Auto-tag review: the model's suggested tags/folder — click to accept. -->
              {#if hasSuggestion(suggest[id])}
                {@const rowSuggest = suggest[id]}
                <div class:review-audit={rowSuggest.audit} class:review-ai={!rowSuggest.audit} class="review-strip mt-1.5 flex flex-wrap items-center gap-1.5 p-1.5">
                  <span class="review-label">{rowSuggest.audit ? 'audit' : 'ai suggested'}</span>
                  {#each rowSuggest.tags || [] as t (t)}
                    <button type="button" onclick={() => acceptTag(r, t)} title="Add this tag"
                      class="review-chip review-chip-add">+ #{t}</button>
                  {/each}
                  {#each rowSuggest.remove_tags || [] as t (t)}
                    <button type="button" onclick={() => acceptRemoveTag(r, t)} title="Remove this tag"
                      class="review-chip review-chip-remove">- #{t}</button>
                  {/each}
                  {#if rowSuggest.folder}
                    <button type="button" onclick={() => acceptFolder(r)} title="Move to this folder"
                      class="review-chip review-chip-folder">to {rowSuggest.folder}</button>
                  {/if}
                  {#if rowSuggest.reason}
                    <span class="review-reason min-w-full">{rowSuggest.reason}</span>
                  {/if}
                  <button type="button" onclick={() => acceptOne(r)} class="review-link ml-auto">accept all</button>
                  <button type="button" onclick={() => dropSuggest(id)} class="text-[0.6875rem] text-muted transition hover:text-ink">dismiss</button>
                </div>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
      <!-- Grows the rendered window as it scrolls into view (keeps a big library light). -->
      {#if pageItems.length < shown.length}
        <div bind:this={sentinel} class="h-10"></div>
      {/if}
    {/if}
  </div>
</div>

{#if enhance.open}
  <div class="fixed inset-0 z-[70] flex items-center justify-center bg-black/55 p-3 backdrop-blur-sm">
    <div role="dialog" aria-modal="true" aria-labelledby="enhance-title"
      class="flex max-h-[calc(100dvh-1.5rem)] w-full max-w-3xl flex-col rounded-xl border border-line bg-[var(--surface-solid)] shadow-2xl">
      <header class="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div class="min-w-0">
          <h3 id="enhance-title" class="text-sm font-extrabold text-ink">Enhanced prompt</h3>
          <p class="mt-0.5 text-xs text-muted">
            Save as a new prompt in <span class="font-semibold text-ink">{ENHANCED_FOLDER}</span>{#if enhance.model} · {enhance.model}{/if}
          </p>
        </div>
        <button type="button" onclick={closeEnhance} disabled={enhance.loading} aria-label="Close"
          class="rounded-md border border-line px-2 py-1 text-xs font-bold text-muted transition hover:border-[var(--accent)] hover:text-ink disabled:opacity-40">×</button>
      </header>

      <div class="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <span class="text-[0.6875rem] font-bold uppercase tracking-wider text-muted">Dialogue</span>
          <div class="flex rounded-lg border border-line p-0.5">
            {#each DIALOGUE_LEVELS as option (option.key)}
              <button type="button" onclick={() => chooseEnhanceLevel(option.key)} disabled={enhance.loading}
                class="rounded-md px-2.5 py-1 text-xs font-semibold transition disabled:opacity-40 {enhance.level === option.key ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}">
                {option.label}
              </button>
            {/each}
          </div>
          <button type="button" onclick={toggleDialogueOnly} disabled={enhance.loading}
            class="rounded-md border px-3 py-1.5 text-xs font-semibold transition disabled:opacity-40 {enhance.dialogueOnly ? 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] text-ink' : 'border-line text-muted hover:border-[var(--accent)] hover:text-ink'}">
            Dialogue only
          </button>
          <button type="button" onclick={() => runEnhance()} disabled={enhance.loading || !enhance.level}
            class="ml-auto rounded-md border border-line px-3 py-1.5 text-xs font-semibold transition hover:border-[var(--accent)] disabled:opacity-40">
            {enhance.loading ? 'Enhancing...' : 'Regenerate'}
          </button>
        </div>

        <div class="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div class="min-w-0">
            <div class="mb-1.5 text-[0.625rem] font-bold uppercase tracking-wider text-muted">Original</div>
            <div class="max-h-72 overflow-y-auto rounded-lg border border-line bg-[var(--surface)]/60 p-3 text-sm leading-relaxed text-muted">
              <p class="whitespace-pre-wrap break-words">{enhance.source}</p>
            </div>
          </div>
          <div class="min-w-0">
            <div class="mb-1.5 flex items-center justify-between gap-2">
              <span class="text-[0.625rem] font-bold uppercase tracking-wider text-muted">Enhanced</span>
              <span class="text-[0.625rem] text-muted">{enhance.text.length} chars</span>
            </div>
            {#if enhance.loading}
              <div class="flex min-h-48 items-center justify-center rounded-lg border border-dashed border-line bg-[var(--surface)]/45 text-sm font-semibold text-[var(--accent)]">
                Enhancing...
              </div>
            {:else if !enhance.text}
              <div class="flex min-h-48 items-center justify-center rounded-lg border border-dashed border-line bg-[var(--surface)]/45 px-4 text-center text-sm font-semibold text-muted">
                Choose Natural, Suggestive, or Unfiltered to enhance this prompt.
              </div>
            {:else}
              <textarea bind:value={enhance.text} rows="10" maxlength="2000" use:focusOnMount
                class="min-h-48 w-full resize-y rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm leading-relaxed text-ink outline-none focus:border-[var(--accent)]"></textarea>
            {/if}
          </div>
        </div>
      </div>

      <footer class="flex flex-wrap items-center justify-end gap-2 border-t border-line px-4 py-3">
        <button type="button" onclick={copyEnhanced} disabled={enhance.loading || !enhance.text.trim()}
          class="rounded-md border border-line px-3 py-1.5 text-xs font-semibold transition hover:border-[var(--accent)] disabled:opacity-40">Copy</button>
        <button type="button" onclick={saveEnhanced} disabled={enhance.loading || !canSaveEnhanced}
          class="rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-bold text-[var(--on-accent)] transition hover:brightness-110 disabled:opacity-40">Save as new</button>
      </footer>
    </div>
  </div>
{/if}

{#if confirmImport}
  <ConfirmDialog danger={false} title="Import library prompts?"
    message={`Add ${importPreview?.missing ?? ''} prompt${(importPreview?.missing ?? 0) === 1 ? '' : 's'} from your media library that aren't saved yet, into a “Library” folder. Your current saved list is backed up first.`}
    confirmLabel={importing ? 'Importing…' : 'Import'}
    onconfirm={doImport} oncancel={() => { if (!importing) confirmImport = false; }} />
{/if}

<style>
  /* reveal() animates max-height between the clamped floor and full height; this is the easing. */
  .resp-roll { transition: max-height 220ms ease; }

  .tag-cloud {
    position: relative;
    transition: max-height 180ms ease;
  }

  .tag-cloud-collapsed {
    max-height: 1.75rem;
    overflow: hidden;
  }

  .tag-cloud-collapsed::after {
    background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--surface-solid) 90%, transparent));
    bottom: 0;
    content: "";
    height: 1.75rem;
    pointer-events: none;
    position: absolute;
    right: 0;
    width: 4rem;
  }

  @media (min-width: 640px) {
    .tag-cloud-collapsed {
      max-height: 3.625rem;
    }

    .tag-cloud-collapsed::after {
      height: 1.75rem;
    }
  }

  .review-strip {
    border: 1px solid color-mix(in srgb, var(--line) 78%, var(--review-tone) 22%);
    border-left: 2px solid color-mix(in srgb, var(--review-tone) 70%, var(--line) 30%);
    border-radius: 0.5rem;
    background:
      linear-gradient(90deg, color-mix(in srgb, var(--review-tone) 9%, transparent), transparent 58%),
      color-mix(in srgb, var(--surface) 86%, transparent);
  }

  .review-ai { --review-tone: var(--accent); }
  .review-audit { --review-tone: #d5a44f; }

  .review-label {
    color: color-mix(in srgb, var(--review-tone) 88%, var(--ink) 12%);
    font-size: 0.625rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    line-height: 1;
    text-transform: uppercase;
  }

  .review-chip {
    border: 1px solid color-mix(in srgb, var(--review-tone) 34%, var(--line) 66%);
    border-radius: 9999px;
    color: color-mix(in srgb, var(--review-tone) 82%, var(--ink) 18%);
    font-size: 0.6875rem;
    font-weight: 700;
    line-height: 1;
    padding: 0.25rem 0.5rem;
    transition: background-color 140ms ease, border-color 140ms ease, color 140ms ease;
  }

  .review-chip:hover {
    background: color-mix(in srgb, var(--review-tone) 18%, transparent);
    border-color: color-mix(in srgb, var(--review-tone) 70%, var(--line) 30%);
    color: var(--ink);
  }

  .review-chip-remove {
    border-color: color-mix(in srgb, var(--danger) 34%, var(--line) 66%);
    color: color-mix(in srgb, var(--danger) 82%, var(--ink) 18%);
  }

  .review-chip-remove:hover {
    background: color-mix(in srgb, var(--danger) 14%, transparent);
    border-color: color-mix(in srgb, var(--danger) 70%, var(--line) 30%);
  }

  .review-chip-folder { border-radius: 0.375rem; }

  .review-reason {
    color: color-mix(in srgb, var(--muted) 82%, var(--review-tone) 18%);
    font-size: 0.6875rem;
    line-height: 1.35;
  }

  .review-link {
    color: color-mix(in srgb, var(--review-tone) 86%, var(--ink) 14%);
    font-size: 0.6875rem;
    font-weight: 800;
    transition: color 140ms ease;
  }

  .review-link:hover { color: var(--ink); text-decoration: underline; }
</style>
