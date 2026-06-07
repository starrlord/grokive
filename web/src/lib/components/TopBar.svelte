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
  <!-- Mobile/tablet menu: opens the sidebar drawer (filters + playlists + models).
       The desktop sidebar is always visible, so this is lg:hidden. Pulled out as a
       prominent leading control on small screens. -->
  <button type="button" class="topbar-menu grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-line bg-[var(--surface-2)] text-lg lg:hidden" aria-label="Open menu" title="Menu" onclick={onmenu}>☰</button>
  <button type="button" class="topbar-brand text-lg font-extrabold tracking-tight hover:opacity-80" title="Reset — show recent media" onclick={resetAll}>Grokive</button>

  <div class="topbar-search order-3 w-full min-w-0 sm:order-none sm:w-auto sm:max-w-[460px] sm:flex-1">
    <input
      class="w-full rounded-full border border-line bg-[var(--surface-2)] px-4 py-2 text-sm outline-none placeholder:text-muted"
      type="search" placeholder="Search prompts, tags, models…" value={q} oninput={onInput} />
  </div>

  <nav class="topbar-views flex max-w-full gap-1 overflow-x-auto rounded-full border border-line bg-[var(--surface-2)] p-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
    {#each views as v (v.id)}
      <button type="button"
        class="shrink-0 whitespace-nowrap rounded-full px-3 py-1.5 text-sm font-semibold transition {$filters.view === v.id ? 'bg-[var(--surface-solid)] text-ink shadow-sm' : 'text-muted hover:text-ink'}"
        aria-current={$filters.view === v.id ? 'page' : undefined}
        onclick={() => setView(v.id)}>{label(v)}</button>
    {/each}
  </nav>

  <button type="button"
    class="topbar-workspace inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 py-1.5 text-sm font-bold transition {$filters.view === 'studio' ? 'topbar-workspace-active border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line text-muted'}"
    aria-label="Open Prompt Studio"
    aria-current={$filters.view === 'studio' ? 'page' : undefined}
    title="Open Prompt Studio"
    onclick={() => setView('studio')}>
    <span aria-hidden="true">✦</span>
    <span aria-hidden="true" class="topbar-workspace-label"></span>
  </button>

  <div class="topbar-tools flex flex-wrap items-center gap-1.5">
    <select class="rounded-lg border bg-[var(--surface-2)] px-2 py-1.5 text-sm {$filters.period !== 'all' ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-line'}"
      title="Time period" value={$filters.period} onchange={(e) => setPeriod(e.target.value)}>
      {#each periods as p (p.id)}<option value={p.id}>{p.label}</option>{/each}
    </select>
    <select class="rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm"
      value={$filters.sort} onchange={(e) => setSort(e.target.value)}>
      <option value="new">Newest</option>
      <option value="old">Oldest</option>
      <option value="prompt">Prompt A–Z</option>
      <option value="model">Model A–Z</option>
    </select>
    <button type="button" title="Layout mode" aria-label="Toggle layout mode" class="grid h-9 w-9 place-items-center rounded-lg border border-line text-sm" onclick={toggleMode}>
      {$mode === 'cinematic' ? '▦' : '▤'}
    </button>
    <button type="button" title="Light / dark" aria-label="Toggle light / dark theme" class="grid h-9 w-9 place-items-center rounded-lg border border-line" onclick={toggleLight}>
      {$theme === 'light' ? '☀' : '☾'}
    </button>
    <span class="mx-0.5 hidden h-6 w-px self-center bg-line sm:block" aria-hidden="true"></span>
    <button type="button"
      class="topbar-action rounded-lg border px-3 py-1.5 text-sm font-semibold transition {$selectMode ? 'topbar-action-active border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line'}"
      onclick={() => setSelectMode(!$selectMode)}>{$selectMode ? 'Done' : 'Select'}</button>
  </div>

  <div class="topbar-system w-full sm:w-auto"><SystemControls {onrefresh} /></div>
</header>

<style>
  /* Phones: a tidy stack instead of a ragged flex-wrap. Brand + primary actions on
     row 1 (menu leading), then tabs, then the display tools, then search. */
  @media (max-width: 767px) {
    .topbar {
      display: grid;
      grid-template-columns: auto 1fr auto;
      grid-template-areas:
        "menu  brand   system"
        "views views   workspace"
        "tools tools   tools"
        "search search search";
      column-gap: 0.5rem;
      row-gap: 0.5rem;
      align-items: center;
    }
    .topbar-menu { grid-area: menu; }
    .topbar-brand { grid-area: brand; }
    .topbar-system { grid-area: system; justify-self: end; min-width: 0; }
    .topbar-views { grid-area: views; width: 100%; min-width: 0; }
    .topbar-workspace { grid-area: workspace; justify-self: end; }
    .topbar-tools {
      grid-area: tools;
      width: 100%;
      min-width: 0;
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 0.0625rem;
      scrollbar-width: none;
    }
    .topbar-tools::-webkit-scrollbar { display: none; }
    .topbar-tools :global(select) { max-width: 7.5rem; }
    .topbar-search { grid-area: search; width: 100%; min-width: 0; }
  }

  .topbar-action {
    background: color-mix(in srgb, var(--surface-2) 72%, transparent);
    box-shadow: inset 0 1px 0 var(--surface-highlight);
  }

  .topbar-action:hover,
  .topbar-action:focus-visible {
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
    border-color: var(--accent);
    color: var(--ink);
  }

  .topbar-action-active:hover,
  .topbar-action-active:focus-visible {
    background: color-mix(in srgb, var(--accent) 88%, var(--ink) 12%);
    color: var(--on-accent);
  }

  .topbar-workspace {
    background: color-mix(in srgb, var(--surface-2) 68%, transparent);
    box-shadow: inset 0 1px 0 var(--surface-highlight);
  }

  .topbar-workspace:hover,
  .topbar-workspace:focus-visible {
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
    border-color: var(--accent);
    color: var(--ink);
  }

  .topbar-workspace-active:hover,
  .topbar-workspace-active:focus-visible {
    background: color-mix(in srgb, var(--accent) 88%, var(--ink) 12%);
    color: var(--on-accent);
  }

  .topbar-workspace-label::before {
    content: 'Studio';
  }

  @media (min-width: 640px) {
    .topbar-workspace-label::before {
      content: 'Prompt Studio';
    }
  }

  @media (min-width: 768px) and (max-width: 1279px) {
    .topbar {
      display: grid;
      grid-template-columns: auto auto minmax(12rem, 1fr) auto;
      grid-template-areas:
        "menu brand search system"
        "views views views workspace"
        "tools tools tools tools";
      column-gap: 0.75rem;
      row-gap: 0.5rem;
      align-items: center;
    }

    .topbar-menu { grid-area: menu; }
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
    .topbar-workspace {
      grid-area: workspace;
      justify-self: end;
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
        "views workspace tools";
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
    .topbar-workspace {
      grid-area: workspace;
      justify-self: start;
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
