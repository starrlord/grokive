<script>
  import { onMount } from 'svelte';
  import { generateScene, fetchScenes, saveScenes } from '$lib/api.js';
  import { addSavedResponse } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import { toast } from '$lib/toast.js';

  // Scripts a continuous multi-clip scene. Grok builds long video by chaining ~6s or 10s clips
  // ("extend from frame"), so we divide the target length by the increment to get the beat count,
  // and the model keeps character/outfit/setting consistent across beats. `base` is prefilled from
  // the composer's Image + Motion prompts; the component remounts on entering Scene mode so it
  // always seeds from the current composer.
  let { base = '', llmReady = false, persona = '', personaAnchor = '' } = $props();

  // A neutral example base for first-time users (when nothing is prefilled from the composer), so the
  // Scene tab is usable out of the box. Pairs with the seeded "Noir Detective" persona. Editable/clearable.
  const EXAMPLE_BASE = 'A weary 1940s private detective in a rumpled trench coat and fedora stands under a flickering streetlamp outside a rain-slicked jazz club at night, neon signs reflecting in the puddles.';

  let sceneBase = $state(base.trim() ? base : EXAMPLE_BASE);
  let length = $state(60);     // seconds
  let increment = $state(10);  // 6 | 10
  let instruction = $state('');
  let anchor = $state(personaAnchor); // constant kept at the start of every beat (prefilled from the persona)
  let detail = $state('concise');     // 'concise' | 'detailed'
  let arc = $state(true);             // shape the scene as a building progression
  let running = $state(false);
  let beats = $state([]);
  let meta = $state(null);

  // Saved scenes (server-persisted, shared across devices).
  let saved = $state([]);
  let saveName = $state('');
  onMount(async () => { saved = await fetchScenes(); });

  const LENGTHS = [
    { s: 30, label: '30s' }, { s: 60, label: '1 min' }, { s: 90, label: '1½ min' },
    { s: 120, label: '2 min' }, { s: 180, label: '3 min' }
  ];
  const clips = $derived(Math.max(1, Math.ceil(length / increment)));

  async function run() {
    if (!sceneBase.trim() || running) return;
    running = true; beats = []; meta = null;
    try {
      const r = await generateScene({ base: sceneBase, length_seconds: length, increment, instruction, persona, anchor, detail, arc });
      beats = r.beats || [];
      meta = { clips: r.clips, increment: r.increment, length_seconds: r.length_seconds };
      if (!beats.length) toast('No beats returned — try again.', { type: 'error' });
    } catch (e) {
      toast(e.message || 'Scene generation failed.', { type: 'error' });
    } finally {
      running = false;
    }
  }
  async function copyBeat(b) {
    const ok = await copyText(b);
    toast(ok ? 'Beat copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }
  async function copyAll() {
    const text = beats.map((b, i) => `Beat ${i + 1}: ${b}`).join('\n\n');
    const ok = await copyText(text);
    toast(ok ? 'All beats copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }

  function saveScene() {
    if (!beats.length) return;
    const scene = {
      id: 'sc-' + Math.random().toString(36).slice(2, 9),
      name: saveName.trim() || `Scene ${saved.length + 1}`,
      base: sceneBase, instruction, anchor, detail, arc, length_seconds: length, increment, beats: [...beats],
      created_at: new Date().toISOString().slice(0, 10)
    };
    saved = [scene, ...saved];
    saveScenes(saved);
    saveName = '';
    toast('Scene saved', { type: 'success' });
  }
  function loadScene(s) {
    beats = s.beats || [];
    meta = { clips: (s.beats || []).length, increment: s.increment, length_seconds: s.length_seconds };
    sceneBase = s.base || sceneBase;
    instruction = s.instruction || '';
    anchor = s.anchor || '';
    detail = s.detail === 'detailed' ? 'detailed' : 'concise';
    arc = !!s.arc;
    if (s.length_seconds) length = s.length_seconds;
    if (s.increment) increment = s.increment;
    if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  function deleteScene(id) {
    saved = saved.filter((s) => s.id !== id);
    saveScenes(saved);
  }
</script>

<div class="mx-auto w-full max-w-3xl">
  {#if !llmReady}
    <p class="rounded-card border border-dashed border-line p-4 text-center text-sm text-muted">
      Set <code class="rounded bg-[var(--surface-2)] px-1">LLM_SERVER_URL</code> to use the Scene Builder.
    </p>
  {:else}
    {#if saved.length}
      <div class="mb-4">
        <div class="mb-1.5 text-[0.625rem] font-bold uppercase tracking-wider text-muted">Saved scenes</div>
        <div class="flex flex-wrap gap-1.5">
          {#each saved as s (s.id)}
            <span class="flex items-center gap-1 rounded-full border border-line py-1 pl-3 pr-1 text-xs transition hover:border-[var(--accent)]">
              <button type="button" onclick={() => loadScene(s)} title={`${s.beats?.length || 0} beats · ${s.length_seconds}s`} class="max-w-[12rem] truncate font-semibold">{s.name}</button>
              <button type="button" onclick={() => deleteScene(s.id)} aria-label={`Delete ${s.name}`} class="grid h-5 w-5 place-items-center rounded-full text-muted transition hover:bg-[var(--surface-solid)] hover:text-[var(--danger)]">✕</button>
            </span>
          {/each}
        </div>
      </div>
    {/if}

    <label class="mb-1 block text-[0.625rem] font-bold uppercase tracking-wider text-muted" for="scene-base">Base scene — character + opening</label>
    <textarea id="scene-base" rows="8" bind:value={sceneBase}
      placeholder="Prefilled from your composer — the character, look, and setting to hold across the whole scene."
      class="w-full resize-y rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]"></textarea>

    <div class="mt-3 flex flex-wrap items-end gap-x-5 gap-y-3">
      <div>
        <div class="mb-1 text-[0.625rem] font-bold uppercase tracking-wider text-muted">Length</div>
        <div class="flex flex-wrap gap-1.5">
          {#each LENGTHS as l (l.s)}
            <button type="button" onclick={() => (length = l.s)}
              class="rounded-full border px-3 py-1 text-sm font-semibold transition {length === l.s ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}">{l.label}</button>
          {/each}
        </div>
      </div>
      <div>
        <div class="mb-1 text-[0.625rem] font-bold uppercase tracking-wider text-muted">Clip length</div>
        <div class="flex rounded-full border border-line p-0.5">
          {#each [6, 10] as inc (inc)}
            <button type="button" onclick={() => (increment = inc)}
              class="rounded-full px-3 py-1 text-sm font-semibold transition {increment === inc ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}">{inc}s</button>
          {/each}
        </div>
      </div>
      <div class="text-sm text-muted">≈ <strong class="text-ink">{clips}</strong> clips to chain</div>
    </div>

    <div class="mt-3 flex flex-wrap items-end gap-x-5 gap-y-3">
      <div>
        <div class="mb-1 text-[0.625rem] font-bold uppercase tracking-wider text-muted"
          title="Concise gives each beat short, direct motion instructions. Detailed adds richer action, camera, dialogue, and continuity.">Beat detail</div>
        <div class="flex rounded-full border border-line p-0.5">
          {#each [['concise', 'Concise'], ['detailed', 'Detailed']] as [v, label] (v)}
            <button type="button" onclick={() => (detail = v)}
              title={v === 'concise' ? 'Short, direct beat prompts for quick chaining.' : 'Richer beat prompts with more action, camera, dialogue, and continuity.'}
              class="rounded-full px-3 py-1 text-sm font-semibold transition {detail === v ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}">{label}</button>
          {/each}
        </div>
      </div>
      <label class="flex cursor-pointer items-center gap-2 pb-1 text-sm font-semibold"
        title="Shape the generated beats as a progression: setup, escalation, peak, and resolution instead of unrelated clips.">
        <input type="checkbox" bind:checked={arc} aria-label="Build an arc" class="h-4 w-4 accent-[var(--accent)]" />
        Build an arc
      </label>
    </div>

    <input bind:value={anchor} maxlength="200" placeholder="Keep in every beat (optional) — e.g. rain falling around her"
      class="mt-3 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />

    <input bind:value={instruction} placeholder="Direction (optional) — e.g. start slow and build the energy toward the end"
      class="mt-3 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />

    <button type="button" onclick={run} disabled={!sceneBase.trim() || running}
      class="mt-3 rounded-lg bg-[var(--accent)] px-5 py-2.5 text-sm font-bold text-[var(--on-accent)] disabled:opacity-40">
      {running ? 'Scripting scene…' : `Generate ${clips}-beat scene`}
    </button>

    {#if beats.length}
      <div class="mt-5 flex flex-wrap items-center justify-between gap-2">
        <span class="text-xs font-bold uppercase tracking-wider text-muted">
          {beats.length} beats · {meta?.length_seconds}s @ {meta?.increment}s
        </span>
        <div class="flex items-center gap-2">
          <input bind:value={saveName} placeholder="Name this scene" maxlength="40"
            class="w-36 rounded-lg border border-line bg-[var(--surface-2)] px-2.5 py-1.5 text-xs outline-none placeholder:text-muted focus:border-[var(--accent)]" />
          <button type="button" onclick={saveScene} class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]">Save</button>
          <button type="button" onclick={copyAll} class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]">Copy all</button>
        </div>
      </div>
      <p class="mt-1 text-xs text-muted">Generate the first clip from your base image, then paste each beat into <strong class="text-ink">Extend from frame</strong> in order. Quality softens after ~2–3 extensions, so re-anchor with a fresh frame if it drifts.</p>
      <ol class="mt-3 space-y-2">
        {#each beats as b, i (i)}
          <li class="flex items-start gap-3 rounded-lg border border-line bg-[var(--surface-2)] p-3">
            <span class="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full bg-[var(--surface-solid)] text-xs font-bold text-muted">{i + 1}</span>
            <p class="min-w-0 flex-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-ink">{b}</p>
            <div class="flex shrink-0 flex-col gap-1">
              <button type="button" onclick={() => addSavedResponse(b)} title="Save this beat" class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">★ Save</button>
              <button type="button" onclick={() => copyBeat(b)} class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">Copy</button>
            </div>
          </li>
        {/each}
      </ol>
    {/if}
  {/if}
</div>
