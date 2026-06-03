<script>
  import { filters, setView, setQuery, setSort, setPeriod, theme, mode, counts, selectMode, setSelectMode, resetAll, toggleLight } from '$lib/state.js';
  import SystemControls from './SystemControls.svelte';

  let { onrefresh = () => {}, onfilters = () => {}, onmenu = () => {} } = $props();

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
    { id: 'collections', label: 'Collections' },
    { id: 'favorites', label: 'Favorites' },
    { id: 'archive', label: 'Archive' },
    { id: 'canvases', label: 'Canvases' }
  ];

  let q = $state($filters.query);
  let lastSet = $filters.query;
  let timer;
  function onInput(e) {
    q = e.target.value;
    clearTimeout(timer);
    timer = setTimeout(() => { lastSet = q; setQuery(q); }, 250);
  }
  // Keep the box in sync when the query is cleared elsewhere (Reset / logo).
  $effect(() => {
    if ($filters.query !== lastSet) { q = $filters.query; lastSet = $filters.query; }
  });
  const toggleMode = () => mode.update((m) => (m === 'cinematic' ? 'editorial' : 'cinematic'));

  function label(v) {
    if (v.id === 'favorites' && $counts.favorites) return `Favorites (${$counts.favorites})`;
    if (v.id === 'archive' && $counts.archived) return `Archive (${$counts.archived})`;
    return v.label;
  }
</script>

<header class="topbar glass sticky top-0 z-30 flex flex-wrap items-center gap-3 px-4 py-2.5" style="padding-top: max(0.625rem, env(safe-area-inset-top))">
  <button type="button" class="topbar-brand text-lg font-extrabold tracking-tight hover:opacity-80" title="Reset — show recent media" onclick={resetAll}>Grokive</button>

  <div class="topbar-search order-3 w-full min-w-0 sm:order-none sm:w-auto sm:flex-1">
    <input
      class="w-full rounded-full border border-line bg-[var(--surface-2)] px-4 py-2 text-sm outline-none placeholder:text-muted"
      type="search" placeholder="Search prompts, tags, models…" value={q} oninput={onInput} />
  </div>

  <nav class="topbar-views flex max-w-full gap-1 overflow-x-auto rounded-full border border-line bg-[var(--surface-2)] p-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
    {#each views as v}
      <button type="button"
        class="shrink-0 whitespace-nowrap rounded-full px-3 py-1.5 text-sm font-semibold transition {$filters.view === v.id ? 'bg-[var(--accent)] text-white' : 'text-muted hover:text-ink'}"
        onclick={() => setView(v.id)}>{label(v)}</button>
    {/each}
  </nav>

  <div class="topbar-tools flex flex-wrap items-center gap-1.5">
    <!-- Mobile: opens the sidebar (filters + playlists + models) as a drawer.
         The desktop sidebar is always visible, so this is lg:hidden. -->
    <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line text-base lg:hidden" aria-label="Open menu" title="Menu" onclick={onmenu}>☰</button>
    <select class="rounded-lg border bg-[var(--surface-2)] px-2 py-1.5 text-sm {$filters.period !== 'all' ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-line'}"
      title="Time period" value={$filters.period} onchange={(e) => setPeriod(e.target.value)}>
      {#each periods as p}<option value={p.id}>{p.label}</option>{/each}
    </select>
    <select class="rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm"
      value={$filters.sort} onchange={(e) => setSort(e.target.value)}>
      <option value="new">Newest</option>
      <option value="old">Oldest</option>
      <option value="prompt">Prompt A–Z</option>
      <option value="model">Model A–Z</option>
    </select>
    <button type="button" title="Layout mode" class="grid h-9 w-9 place-items-center rounded-lg border border-line text-sm" onclick={toggleMode}>
      {$mode === 'cinematic' ? '▦' : '▤'}
    </button>
    <button type="button" title="Light / dark" class="grid h-9 w-9 place-items-center rounded-lg border border-line" onclick={toggleLight}>
      {$theme === 'light' ? '☀' : '☾'}
    </button>
    <button type="button"
      class="rounded-lg border px-3 py-1.5 text-sm font-semibold {$selectMode ? 'border-transparent bg-[var(--accent)] text-white' : 'border-line'}"
      onclick={() => setSelectMode(!$selectMode)}>{$selectMode ? 'Done' : 'Select'}</button>
  </div>

  <div class="topbar-system w-full sm:w-auto"><SystemControls {onrefresh} /></div>
</header>

<style>
  @media (min-width: 768px) and (max-width: 1279px) {
    .topbar {
      display: grid;
      grid-template-columns: auto minmax(12rem, 1fr) auto;
      grid-template-areas:
        "brand search system"
        "views views views"
        "tools tools tools";
      column-gap: 0.75rem;
      row-gap: 0.5rem;
      align-items: center;
    }

    .topbar-brand { grid-area: brand; }
    .topbar-search {
      grid-area: search;
      min-width: 0;
      width: auto;
    }
    .topbar-views {
      grid-area: views;
      justify-self: stretch;
      width: 100%;
      min-width: 0;
    }
    .topbar-views :global(button) {
      padding-left: 0.625rem;
      padding-right: 0.625rem;
      font-size: 0.8125rem;
    }
    .topbar-tools {
      grid-area: tools;
      flex-wrap: nowrap;
      min-width: 0;
      overflow-x: auto;
      padding-bottom: 0.0625rem;
      scrollbar-width: none;
    }
    .topbar-tools::-webkit-scrollbar { display: none; }
    .topbar-tools :global(select) { max-width: 8.5rem; }
    .topbar-system {
      grid-area: system;
      justify-self: end;
      width: auto;
      min-width: 0;
    }
  }

  @media (min-width: 1280px) and (max-width: 1535px) {
    .topbar {
      display: grid;
      grid-template-columns: auto minmax(16rem, 1fr) auto;
      grid-template-areas:
        "brand search system"
        "views views tools";
      column-gap: 0.75rem;
      row-gap: 0.5rem;
      align-items: center;
    }

    .topbar-brand { grid-area: brand; }
    .topbar-search {
      grid-area: search;
      min-width: 0;
      width: auto;
    }
    .topbar-views {
      grid-area: views;
      min-width: 0;
      width: 100%;
    }
    .topbar-tools {
      grid-area: tools;
      justify-self: end;
      flex-wrap: nowrap;
    }
    .topbar-system {
      grid-area: system;
      justify-self: end;
      width: auto;
      min-width: 0;
    }
  }
</style>
