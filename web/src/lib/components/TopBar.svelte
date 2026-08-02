<script>
  import { filters, setView, setQuery, searchAllMedia, setSort, setPeriod, theme, mode, counts, selectMode, setSelectMode, resetAll, toggleLight, openStudio, studioTab, activeCollectionId } from '$lib/state.js';
  import SystemControls from './SystemControls.svelte';
  import SearchField from './SearchField.svelte';
  import Popover from './Popover.svelte';
  import QuotaBolts from './QuotaBolts.svelte';

  let { onrefresh = () => {}, onfilters = () => {}, onmenu = () => {}, onplay = () => {}, onmontage = () => {} } = $props();

  const periods = [
    { id: 'all', label: 'All time' },
    { id: 'hour1', label: 'Last hour' },
    { id: 'hour4', label: 'Last 4 hours' },
    { id: 'hour8', label: 'Last 8 hours' },
    { id: 'today', label: 'Today' },
    { id: 'yesterday', label: 'Yesterday' },
    { id: 'last7', label: 'Last 7 days' },
    { id: 'last14', label: 'Last 14 days' },
    { id: 'last30', label: 'Last 30 days' },
    { id: 'month', label: 'This month' },
    { id: 'year', label: 'This year' }
  ];

  const views = [
    { id: 'recent', label: 'Recent' },
    { id: 'all', label: 'All Media' },
    { id: 'collections', label: 'Library' },
    { id: 'favorites', label: 'Favorites' },
    { id: 'archive', label: 'Archive' },
    { id: 'canvases', label: 'Canvases' }
  ];

  let q = $state($filters.query);
  let lastSet = $filters.query;
  let timer;
  function onInput() {
    // `q` is already updated by bind:value; just debounce the push to the store.
    clearTimeout(timer);
    timer = setTimeout(() => { lastSet = q; setQuery(q); }, 250);
  }
  // Clearing skips the debounce so results reset the instant the × is clicked.
  function clearSearch() {
    clearTimeout(timer);
    lastSet = '';
    setQuery('');
  }
  // Views where the search box has nothing to filter (the query is ignored): the
  // Collections/Library landing and the Studio/Imagine authoring workspaces. Inside a
  // collection or a drilled canvas, search DOES refine live, so those aren't inert.
  const searchInert = $derived(
    ($filters.view === 'collections' && !$activeCollectionId) ||
    $filters.view === 'studio' ||
    $filters.view === 'imagine'
  );
  // Enter flushes the debounce immediately. From a search-inert context it also jumps to
  // All Media so the matches actually render as a grid instead of doing nothing.
  function onSearchKey(e) {
    if (e.key !== 'Enter') return;
    clearTimeout(timer);
    const query = q.trim();
    lastSet = query;
    if (query && searchInert) searchAllMedia(query);
    else setQuery(query);
  }
  // Keep the box in sync when the query is cleared elsewhere (Reset / logo).
  $effect(() => {
    if ($filters.query !== lastSet) { q = $filters.query; lastSet = $filters.query; }
  });
  // On the collections landing grid, media sort/period don't apply (you're looking at
  // collections, not media) — so they drop out of the Display menu there. The grid has
  // its own sort; they return inside a collection.
  const onCollectionsLanding = $derived($filters.view === 'collections' && !$activeCollectionId);
  // Period/sort active = a non-default value is set; surfaces a dot on the Display button.
  const displayActive = $derived(!onCollectionsLanding && ($filters.period !== 'all' || $filters.sort !== 'new'));

  function label(v) {
    if (v.id === 'favorites' && $counts.favorites) return `Favorites (${$counts.favorites})`;
    if (v.id === 'archive' && $counts.archived) return `Archive (${$counts.archived})`;
    return v.label;
  }

  // Mobile/tablet (<lg) collapses the pill row into a single "current view" menu — the active
  // view object backs its trigger label. studio/imagine aren't in `views` (entered via the
  // workspace icons), so the trigger falls back to "Browse" while one of those is open.
  const activeView = $derived(views.find((v) => v.id === $filters.view));
</script>

<header class="topbar glass sticky top-0 z-30 flex flex-col gap-2 px-4 py-2.5" style="padding-top: max(0.625rem, env(safe-area-inset-top))">
  <!-- Tier 1 — identity · search · system. Calm row: who you are, find, and status/settings. -->
  <div class="flex flex-wrap items-center gap-2 sm:gap-3">
    <!-- Mobile/tablet menu: opens the filter drawer; desktop sidebar is always visible. -->
    <button type="button" class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-line bg-[var(--surface-2)] text-lg lg:hidden" aria-label="Open menu" title="Menu" onclick={onmenu}>☰</button>
    <button type="button" class="shrink-0 text-lg font-extrabold tracking-tight hover:opacity-80" title="Reset — show recent media" onclick={resetAll}>Grokive</button>

    <div class="order-last w-full min-w-0 sm:order-none sm:w-auto sm:flex-1 sm:max-w-[520px]">
      <SearchField bind:value={q} oninput={onInput} onkeydown={onSearchKey} onclear={clearSearch}
        placeholder="Search prompts, tags, models…" wrapperClass="w-full"
        inputClass="rounded-full border border-line bg-[var(--surface-2)] py-2 pl-4 pr-10 text-sm outline-none placeholder:text-muted" />
    </div>

    <div class="ml-auto flex shrink-0 items-center gap-1.5 sm:ml-0">
      <!-- Montage maker, selection-free: opens Generate Movie with no clips (it
           defaults to Auto-pick there). Lives in TIER 1 deliberately — tier 2's
           workspace row is already at its iPhone-portrait width budget (one more
           button crushes the view-switcher pill to a bare chevron); this row has
           slack on every width because the search field wraps to its own line
           below sm. Icon-only below sm, like its Display neighbour. -->
      <button type="button"
        class="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-line bg-[var(--surface-2)] px-2.5 text-sm font-semibold transition hover:border-[var(--accent)]"
        aria-label="Create a montage"
        title="Create a montage — Auto-pick chooses clips for your song"
        onclick={() => onmontage()}>
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3Z"/><path d="m6.2 5.3 3.1 3.9"/><path d="m12.4 3.4 3.1 4"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>
        <span class="hidden sm:inline">Montage</span>
      </button>
      <!-- Grok weekly usage — one ⚡ bolt per active account, breakdown on click. -->
      <QuotaBolts />
      <!-- Display: period · sort · density · theme, tucked into one popover. -->
      <Popover align="right" title="Display options"
        ariaLabel={displayActive ? 'Display options — filters active' : 'Display options'}
        triggerClass="inline-flex h-9 items-center gap-1.5 rounded-lg border border-line bg-[var(--surface-2)] px-2.5 text-sm font-semibold transition hover:border-[var(--accent)]">
        {#snippet trigger()}
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/></svg>
          <span class="hidden sm:inline">Display</span>
          {#if displayActive}<span class="h-1.5 w-1.5 rounded-full bg-[var(--accent)]" aria-hidden="true"></span>{/if}
        {/snippet}
        {#snippet children()}
          <div class="w-[17rem] max-w-[calc(100vw-1rem)] rounded-card border border-line bg-[var(--surface-solid)] p-2.5 shadow-[0_18px_44px_-14px_rgba(0,0,0,0.6)]">
            {#if !onCollectionsLanding}
              <div class="mb-2 flex items-center justify-between gap-3">
                <span class="text-xs font-semibold text-muted">Time period</span>
                <select class="min-w-0 max-w-[10rem] flex-1 rounded-lg border bg-[var(--surface-2)] px-2 py-1.5 text-sm {$filters.period !== 'all' ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-line'}"
                  value={$filters.period} onchange={(e) => setPeriod(e.target.value)}>
                  {#each periods as p (p.id)}<option value={p.id}>{p.label}</option>{/each}
                </select>
              </div>
              <div class="mb-2 flex items-center justify-between gap-3">
                <span class="text-xs font-semibold text-muted">Sort</span>
                <select class="min-w-0 max-w-[10rem] flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm"
                  value={$filters.sort} onchange={(e) => setSort(e.target.value)}>
                  <option value="new">Newest</option>
                  <option value="old">Oldest</option>
                  <option value="size">Largest</option>
                  <option value="size_asc">Smallest</option>
                  <option value="prompt">Prompt A–Z</option>
                  <option value="model">Model A–Z</option>
                </select>
              </div>
            {/if}
            <div class="mb-2 flex items-center justify-between gap-3">
              <span class="text-xs font-semibold text-muted">Density</span>
              <div class="flex rounded-lg border border-line p-0.5 text-sm font-semibold">
                <button type="button" aria-pressed={$mode === 'cinematic'} class="rounded-md px-2.5 py-1 transition {$mode === 'cinematic' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}" onclick={() => mode.set('cinematic')}>Cinematic</button>
                <button type="button" aria-pressed={$mode === 'editorial'} class="rounded-md px-2.5 py-1 transition {$mode === 'editorial' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}" onclick={() => mode.set('editorial')}>Editorial</button>
              </div>
            </div>
            <div class="flex items-center justify-between gap-3">
              <span class="text-xs font-semibold text-muted">Theme</span>
              <div class="flex rounded-lg border border-line p-0.5 text-sm font-semibold">
                <button type="button" aria-pressed={$theme !== 'light'} class="rounded-md px-2.5 py-1 transition {$theme !== 'light' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}" onclick={() => { if ($theme === 'light') toggleLight(); }}>Dark</button>
                <button type="button" aria-pressed={$theme === 'light'} class="rounded-md px-2.5 py-1 transition {$theme === 'light' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}" onclick={() => { if ($theme !== 'light') toggleLight(); }}>Light</button>
              </div>
            </div>
          </div>
        {/snippet}
      </Popover>

      <SystemControls {onrefresh} />
    </div>
  </div>

  <!-- Tier 2 — navigation · workspaces · select. The "where you go" row. -->
  <div class="flex items-center gap-2">
    <!-- Below lg the six view-pills would starve inside an invisible horizontal scroll, so they
         collapse into one full-width "current view" menu (iPhone + iPad-portrait). The pill nav
         returns at lg, where the persistent sidebar appears and there's room for it. -->
    <div class="min-w-0 flex-1 lg:hidden">
      <Popover align="left" title="Switch view"
        triggerClass="inline-flex min-h-[44px] w-full items-center justify-between gap-2 rounded-full border border-line bg-[var(--surface-2)] px-4 text-sm font-semibold text-ink">
        {#snippet trigger()}
          <span class="truncate">{activeView ? label(activeView) : 'Browse'}</span>
          <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-muted" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
        {/snippet}
        {#snippet children(close)}
          <div class="w-[14rem] max-w-[calc(100vw-1rem)] rounded-card border border-line bg-[var(--surface-solid)] p-1.5 shadow-[0_18px_44px_-14px_rgba(0,0,0,0.6)]">
            {#each views as v (v.id)}
              <button type="button"
                class="flex min-h-[44px] w-full items-center justify-between gap-3 rounded-lg px-3 text-left text-sm font-semibold transition {$filters.view === v.id ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-ink hover:bg-[var(--surface-2)]'}"
                aria-current={$filters.view === v.id ? 'page' : undefined}
                onclick={() => { setView(v.id); close(); }}>
                <span class="truncate">{label(v)}</span>
                {#if $filters.view === v.id}<svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>{/if}
              </button>
            {/each}
          </div>
        {/snippet}
      </Popover>
    </div>

    <nav class="hidden min-w-0 flex-1 gap-1 rounded-full border border-line bg-[var(--surface-2)] p-1 lg:flex lg:overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      {#each views as v (v.id)}
        <button type="button"
          class="shrink-0 whitespace-nowrap rounded-full px-3 py-1.5 text-sm font-semibold transition {$filters.view === v.id ? 'bg-[var(--surface-solid)] text-ink shadow-sm' : 'text-muted hover:text-ink'}"
          aria-current={$filters.view === v.id ? 'page' : undefined}
          onclick={() => setView(v.id)}>{label(v)}</button>
      {/each}
    </nav>

    <div class="flex shrink-0 items-center gap-1.5">
      <div class="flex shrink-0 items-center gap-1.5">
        <button type="button"
          class="topbar-workspace inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-line px-3 py-1.5 text-sm font-bold text-muted transition"
          aria-label="Play random videos from your library"
          title="Play random videos across your library"
          onclick={() => onplay()}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"/></svg>
          <span aria-hidden="true" class="hidden sm:inline">Play</span>
        </button>
        <button type="button"
          class="topbar-workspace inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 py-1.5 text-sm font-bold transition {$filters.view === 'studio' && $studioTab !== 'saved' ? 'topbar-workspace-active border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line text-muted'}"
          aria-label="Open Prompt Studio"
          aria-current={$filters.view === 'studio' && $studioTab !== 'saved' ? 'page' : undefined}
          title="Open Prompt Studio"
          onclick={() => openStudio()}>
          <span aria-hidden="true">✦</span>
          <span aria-hidden="true" class="topbar-workspace-label"></span>
        </button>
        <button type="button"
          class="topbar-workspace inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 py-1.5 text-sm font-bold transition {$filters.view === 'studio' && $studioTab === 'saved' ? 'topbar-workspace-active border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line text-muted'}"
          aria-label="Open saved prompts"
          aria-current={$filters.view === 'studio' && $studioTab === 'saved' ? 'page' : undefined}
          title="Saved prompts"
          onclick={() => openStudio('saved')}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
          <span aria-hidden="true" class="hidden sm:inline">Prompts</span>
        </button>
        <button type="button"
          class="topbar-workspace inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 py-1.5 text-sm font-bold transition {$filters.view === 'imagine' ? 'topbar-workspace-active border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line text-muted'}"
          aria-label="Open Grok Imagine"
          aria-current={$filters.view === 'imagine' ? 'page' : undefined}
          title="Open Grok Imagine — generate images & video"
          onclick={() => setView('imagine')}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8 19 13"/><path d="M15 9h.01"/><path d="m3 21 9-9"/><path d="M12.2 6.2 11 5"/></svg>
          <span aria-hidden="true" class="hidden sm:inline">Imagine</span>
        </button>
      </div>
      <span class="mx-0.5 hidden h-6 w-px self-center bg-line sm:block" aria-hidden="true"></span>
      <button type="button"
        class="topbar-action shrink-0 rounded-lg border px-3 py-1.5 text-sm font-semibold transition {$selectMode ? 'topbar-action-active border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line'}"
        onclick={() => setSelectMode(!$selectMode)}>{$selectMode ? 'Done' : 'Select'}</button>
    </div>
  </div>
</header>

<style>
  .topbar-action,
  .topbar-workspace {
    background: color-mix(in srgb, var(--surface-2) 70%, transparent);
    box-shadow: inset 0 1px 0 var(--surface-highlight);
  }

  .topbar-action:hover,
  .topbar-action:focus-visible,
  .topbar-workspace:hover,
  .topbar-workspace:focus-visible {
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
    border-color: var(--accent);
    color: var(--ink);
  }

  .topbar-action-active:hover,
  .topbar-action-active:focus-visible,
  .topbar-workspace-active:hover,
  .topbar-workspace-active:focus-visible {
    background: color-mix(in srgb, var(--accent) 88%, var(--ink) 12%);
    color: var(--on-accent);
  }

  /* The ✦ workspace button labels itself responsively (icon stays, text grows). */
  .topbar-workspace-label::before {
    content: 'Studio';
  }

  @media (min-width: 640px) {
    .topbar-workspace-label::before {
      content: 'Prompt Studio';
    }
  }
</style>
