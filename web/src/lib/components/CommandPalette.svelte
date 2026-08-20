<script>
  // Ctrl/Cmd+K command palette: fuzzy jump-to-anything plus verb commands. Data comes
  // straight from the client stores, so opening costs nothing and sealed (locked,
  // not-unlocked) collections never appear — their identity is redacted server-side
  // and surfacing "Locked collection" rows here would only be noise.
  import { tick } from 'svelte';
  import { collections, playlists, setView } from '$lib/state.js';

  let {
    onopencollection = () => {},
    onplaycollection = () => {},
    onqueuecollection = () => {},
    onplaylist = () => {},
    onplayrandom = () => {},
    onmontage = () => {}
  } = $props();

  let open = $state(false);
  let query = $state('');
  let active = $state(0);
  let inputEl = $state(null);
  let listEl = $state(null);

  const VIEWS = [
    { key: 'recent', label: 'Recent' },
    { key: 'all', label: 'All Media' },
    { key: 'collections', label: 'Library' },
    { key: 'favorites', label: 'Favorites' },
    { key: 'archive', label: 'Archive' },
    { key: 'canvases', label: 'Canvases' }
  ];

  // Substring match beats subsequence; earlier and full-word matches rank higher.
  function score(q, text) {
    const t = String(text || '').toLowerCase();
    const s = q.toLowerCase();
    if (!s) return 0;
    const idx = t.indexOf(s);
    if (idx !== -1) return 100 - Math.min(idx, 40) + (t.length === s.length ? 20 : 0);
    let ti = 0;
    for (const ch of s) {
      const found = t.indexOf(ch, ti);
      if (found === -1) return -1;
      ti = found + 1;
    }
    return 20 - Math.min(t.length - s.length, 15);
  }

  const usable = $derived(($collections || []).filter((c) => !(c.locked && !c.unlocked)));
  const verbMatch = $derived.by(() => {
    const m = query.trim().match(/^(play|shuffle|queue)(?:\s+(.*))?$/i);
    return m ? { verb: m[1].toLowerCase(), rest: (m[2] || '').trim() } : null;
  });

  const results = $derived.by(() => {
    const q = query.trim();
    const out = [];
    if (verbMatch) {
      // Verb mode: "play|shuffle|queue <collection>" — rank collections under the verb.
      const { verb, rest } = verbMatch;
      const label = verb === 'play' ? 'Play' : verb === 'shuffle' ? 'Shuffle' : 'Queue';
      for (const c of usable) {
        if (!(c.video_count ?? 0)) continue;
        const s = rest ? score(rest, c.name) : 0;
        if (s < 0) continue;
        out.push({
          id: `${verb}:${c.id}`, type: 'action', s: s + 500,
          label: `${label} “${c.name}”`,
          hint: verb === 'queue' ? 'Play Queue' : verb === 'shuffle' ? 'Random order' : 'Collection',
          run: () => (verb === 'queue' ? onqueuecollection(c) : onplaycollection(c, verb === 'shuffle'))
        });
      }
      out.sort((a, b) => b.s - a.s);
      return out.slice(0, 12);
    }
    for (const v of VIEWS) {
      const s = q ? score(q, v.label) : 1;
      if (s >= 0) out.push({ id: `view:${v.key}`, type: 'view', s: s + (q ? 0 : 200), label: v.label, hint: 'View', run: () => setView(v.key) });
    }
    const colls = q
      ? usable
      : [...usable].sort((a, b) => (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || '')).slice(0, 6);
    for (const c of colls) {
      const s = q ? score(q, c.name) : 100;
      if (s >= 0) out.push({ id: `c:${c.id}`, type: 'collection', s: s + 10, label: c.name, hint: `${c.item_count ?? c.ids?.length ?? 0} items`, run: () => onopencollection(c) });
    }
    const pls = q ? ($playlists || []) : ($playlists || []).slice(0, 3);
    for (const pl of pls) {
      const s = q ? score(q, pl.name) : 50;
      if (s >= 0) out.push({ id: `pl:${pl.id}`, type: 'playlist', s, label: pl.name, hint: `Playlist · ${pl.ids?.length ?? 0}`, run: () => onplaylist(pl) });
    }
    const ACTIONS = [
      { id: 'act:random', label: 'Play random across the library', run: onplayrandom },
      { id: 'act:montage', label: 'Generate a montage', run: onmontage }
    ];
    for (const a of ACTIONS) {
      const s = q ? score(q, a.label) : 40;
      if (s >= 0) out.push({ ...a, type: 'action', s, hint: 'Action' });
    }
    out.sort((a, b) => b.s - a.s);
    return out.slice(0, 14);
  });

  // Keep the highllight in range as the result set changes under it.
  $effect(() => { if (active > results.length - 1) active = Math.max(0, results.length - 1); });

  async function show() {
    query = '';
    active = 0;
    open = true;
    await tick();
    inputEl?.focus();
  }
  function close() { open = false; }
  function run(r) {
    close();
    r.run();
  }
  function onWindowKey(e) {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      open ? close() : show();
    } else if (open && e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  }
  function onInputKey(e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!results.length) return;
      active = (active + (e.key === 'ArrowDown' ? 1 : results.length - 1)) % results.length;
      listEl?.children[active]?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (results[active]) run(results[active]);
    }
  }
</script>

<svelte:window onkeydown={onWindowKey} />

{#if open}
  <!-- z-[85]: above the SelectBar dock and every panel, below nothing it should defer to. -->
  <!-- Backdrop closes only on a direct click (target check), so the panel needs no
       stopPropagation click handler of its own. -->
  <div class="fixed inset-0 z-[85] bg-black/55 backdrop-blur-[2px]" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) close(); }}>
    <div class="mx-auto mt-[11vh] w-[min(92vw,40rem)]" role="dialog" aria-modal="true" aria-label="Command palette" tabindex="-1">
      <div class="overflow-hidden rounded-2xl border border-line bg-[var(--surface-solid)] shadow-2xl">
        <div class="flex items-center gap-2.5 border-b border-line px-4">
          <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-muted" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
          <input bind:this={inputEl} bind:value={query} onkeydown={onInputKey} spellcheck="false"
            placeholder="Jump to a collection, playlist, or view — or type play / shuffle / queue…"
            class="w-full bg-transparent py-3.5 text-[15px] font-medium outline-none placeholder:text-muted" />
          <kbd class="shrink-0 rounded border border-line px-1.5 py-0.5 text-[10px] font-bold text-muted">esc</kbd>
        </div>
        <ul bind:this={listEl} class="max-h-[46vh] overflow-y-auto p-1.5">
          {#each results as r, i (r.id)}
            <li>
              <button type="button" onclick={() => run(r)} onpointerenter={() => (active = i)}
                class="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm {i === active ? 'bg-[var(--accent)] text-[var(--on-accent)]' : ''}">
                {#if r.type === 'collection'}
                  <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 opacity-75" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
                {:else if r.type === 'playlist'}
                  <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 opacity-75" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h13"/><path d="M3 12h9"/><path d="M3 18h9"/><path d="m16 12 5 3-5 3v-6Z"/></svg>
                {:else if r.type === 'view'}
                  <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 opacity-75" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/></svg>
                {:else}
                  <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 opacity-75" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                {/if}
                <span class="min-w-0 flex-1 truncate font-semibold">{r.label}</span>
                <span class="shrink-0 text-[11px] font-semibold {i === active ? 'opacity-80' : 'text-muted'}">{r.hint}</span>
              </button>
            </li>
          {/each}
          {#if !results.length}
            <li class="px-3 py-8 text-center text-sm text-muted">No matches for “{query.trim()}”.</li>
          {/if}
        </ul>
        <div class="flex items-center gap-3 border-t border-line px-4 py-2 text-[11px] font-semibold text-muted">
          <span><kbd class="rounded border border-line px-1 py-px">↑↓</kbd> navigate</span>
          <span><kbd class="rounded border border-line px-1 py-px">↵</kbd> select</span>
          <span class="ml-auto">play / shuffle / queue <span class="opacity-70">+ collection name</span></span>
        </div>
      </div>
    </div>
  </div>
{/if}
