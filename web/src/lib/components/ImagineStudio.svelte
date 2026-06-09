<script>
  import { onMount } from 'svelte';
  import {
    activeImagineSession, imagineSessions, imagineJobs,
    refreshImagineSessions, ensureImaginePolling, newImagineWorkspace
  } from '$lib/state.js';
  import { toast } from '$lib/toast.js';
  import ImaginePanel from './ImaginePanel.svelte';

  // Top-level Grok Imagine view: a workspace switcher above the generation panel.
  // Each workspace is an independent staging session; several can render at once.
  const activeId = $derived($activeImagineSession);

  // The persisted sessions, plus the active one if it's brand-new (no generations yet
  // so the server doesn't list it) — so you always see the workspace you're in.
  const chips = $derived.by(() => {
    const list = $imagineSessions || [];
    if (!activeId || list.some((s) => s.session_id === activeId)) return list;
    return [{ session_id: activeId, source: null, count: 0, cover: '', current: true }, ...list];
  });

  function label(s) {
    if (s.source?.prompt) return s.source.prompt;
    return String(s.session_id).startsWith('src:') ? 'Image workspace' : 'Text workspace';
  }
  const cover = (s) => s.source?.thumb || s.cover || '';
  const rendering = (sid) => !!$imagineJobs[sid]?.running;

  function handleNew() {
    // If you're already on an empty (not-yet-persisted) workspace, reuse it instead of
    // minting another id that would orphan this one.
    const list = $imagineSessions || [];
    if (activeId && !list.some((s) => s.session_id === activeId)) {
      toast("You're already on a new workspace.", { type: 'info' });
      return;
    }
    newImagineWorkspace();
  }

  onMount(() => {
    refreshImagineSessions();
    ensureImaginePolling();
  });
</script>

<div class="mx-auto w-full max-w-[1100px]">
  <div class="mb-3">
    <h1 class="text-xl font-extrabold tracking-tight text-ink">Grok Imagine</h1>
    <p class="text-sm text-muted">Generate images &amp; video. Each workspace keeps its own history — switch freely; several can render at once.</p>
  </div>

  <!-- Workspace switcher -->
  <div class="mb-4 flex gap-2 overflow-x-auto pb-1">
    <button type="button" onclick={handleNew} title="Start a new workspace"
      class="flex h-14 shrink-0 items-center gap-1.5 rounded-xl border border-dashed border-line px-3 text-sm font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink">
      <span class="text-lg leading-none">+</span> New
    </button>
    {#each chips as s (s.session_id)}
      <button type="button" onclick={() => activeImagineSession.set(s.session_id)}
        class="flex h-14 shrink-0 items-center gap-2 rounded-xl border px-2 pr-3 text-left transition {activeId === s.session_id ? 'border-[var(--accent)] bg-[var(--accent)]/10' : 'border-line hover:border-[var(--accent)]'}">
        <span class="relative grid h-10 w-10 shrink-0 place-items-center overflow-hidden rounded-lg bg-[var(--media-bg)]">
          {#if cover(s)}
            <img src={cover(s)} alt="" class="h-full w-full object-cover" />
          {:else}
            <svg viewBox="0 0 24 24" class="h-4 w-4 text-muted" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/></svg>
          {/if}
          {#if rendering(s.session_id)}
            <span class="absolute inset-0 grid place-items-center bg-black/50">
              <span class="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"></span>
            </span>
          {/if}
        </span>
        <span class="min-w-0 max-w-[9rem]">
          <span class="block truncate text-xs font-semibold text-ink">{label(s)}</span>
          <span class="block text-[0.625rem] text-muted">{s.current ? 'new' : `${s.count} item${s.count === 1 ? '' : 's'}`}{rendering(s.session_id) ? ' · rendering' : ''}</span>
        </span>
      </button>
    {/each}
  </div>

  <ImaginePanel />
</div>
