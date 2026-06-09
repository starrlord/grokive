<script>
  import { onMount } from 'svelte';
  import { generateFreeform, fetchFreeformPresets, saveFreeformPresets } from '$lib/api.js';
  import { addSavedResponse } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import { toast } from '$lib/toast.js';

  // Direct, unconstrained generation in the active persona's voice — no beat/JSON/anchor scaffolding,
  // closer to querying the model directly. The persona supplies voice + register; your instruction
  // supplies the ask; the model's numbered list comes back as items you can copy.
  let { persona = '', llmReady = false } = $props();

  let instruction = $state('');
  let prefix = $state('');
  let count = $state(10);
  let running = $state(false);
  let items = $state([]);
  let presets = $state([]);
  let selectedPresetId = $state('');
  let presetName = $state('');

  const presetId = () => 'ffp-' + Math.random().toString(36).slice(2, 9);

  onMount(async () => {
    presets = await fetchFreeformPresets();
  });

  // Grow an editable result textarea to its content so it reads like the old static <p>, re-fitting
  // when the bound value changes programmatically (a fresh generation).
  function autosize(node) {
    const fit = () => { node.style.height = 'auto'; node.style.height = node.scrollHeight + 'px'; };
    fit();
    node.addEventListener('input', fit);
    return { update: fit, destroy: () => node.removeEventListener('input', fit) };
  }

  function nameFromFields() {
    return (presetName.trim() || instruction.trim() || prefix.trim() || 'Freeform setup').slice(0, 80);
  }

  function loadPreset(id) {
    const p = presets.find((x) => x.id === id);
    if (!p) return;
    selectedPresetId = id;
    presetName = p.name || '';
    instruction = p.instruction || '';
    prefix = p.prefix || '';
    count = p.count || 10;
    items = [];
    toast('Setup loaded', { type: 'success' });
  }

  function onPresetSelect(id) {
    if (id) loadPreset(id);
    else newPreset();
  }

  function savePreset() {
    if (!instruction.trim() && !prefix.trim()) {
      toast('Add a request or required text first.', { type: 'error' });
      return;
    }
    const entry = {
      id: selectedPresetId || presetId(),
      name: nameFromFields(),
      instruction: instruction.trim(),
      prefix: prefix.trim(),
      count: Math.max(1, Math.min(30, Number(count) || 10)),
      created_at: new Date().toISOString(),
    };
    const idx = presets.findIndex((p) => p.id === entry.id);
    presets = idx >= 0
      ? presets.map((p) => (p.id === entry.id ? { ...p, ...entry } : p))
      : [entry, ...presets];
    selectedPresetId = entry.id;
    presetName = entry.name;
    saveFreeformPresets(presets);
    toast(idx >= 0 ? 'Setup updated' : 'Setup saved', { type: 'success' });
  }

  function newPreset() {
    selectedPresetId = '';
    presetName = '';
  }

  function deletePreset() {
    if (!selectedPresetId) return;
    presets = presets.filter((p) => p.id !== selectedPresetId);
    selectedPresetId = '';
    presetName = '';
    saveFreeformPresets(presets);
    toast('Setup deleted', { type: 'success' });
  }

  async function run() {
    if (!instruction.trim() || running) return;
    running = true; items = [];
    try {
      const r = await generateFreeform({ instruction: instruction, persona, n: count, prefix });
      items = r.items || [];
      if (!items.length) toast('Nothing returned — try again.', { type: 'error' });
    } catch (e) {
      toast(e.message || 'Generation failed.', { type: 'error' });
    } finally {
      running = false;
    }
  }
  async function copyItem(t) {
    const ok = await copyText(t);
    toast(ok ? 'Copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }
  async function copyAll() {
    const ok = await copyText(items.map((t, i) => `${i + 1}. ${t}`).join('\n'));
    toast(ok ? 'All copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }
</script>

<div class="mx-auto w-full max-w-3xl">
  {#if !llmReady}
    <p class="rounded-card border border-dashed border-line p-4 text-center text-sm text-muted">
      Set <code class="rounded bg-[var(--surface-2)] px-1">LLM_SERVER_URL</code> to use Freeform.
    </p>
  {:else}
    <p class="mb-3 text-sm text-muted">Ask the model directly, in your active persona's voice. No beat/format rules — it just answers.{#if !persona.trim()} <span class="text-[var(--accent)]">Select a Persona above for an in-character voice.</span>{/if}</p>

    <div class="mb-3 rounded-lg border border-line bg-[var(--surface-2)]/40 p-3">
      <div class="flex flex-col gap-2 md:flex-row md:items-end">
        <label class="min-w-0 flex-1" for="ff-preset">
          <span class="mb-1 block text-[0.625rem] font-bold uppercase tracking-wider text-muted">Saved setup</span>
          <select id="ff-preset" value={selectedPresetId} onchange={(e) => onPresetSelect(e.currentTarget.value)}
            class="w-full rounded-lg border border-line bg-[var(--surface)] px-3 py-2 text-sm font-semibold text-ink outline-none focus:border-[var(--accent)]">
            <option value="">Load saved request...</option>
            {#each presets as p (p.id)}
              <option value={p.id}>{p.name || 'Untitled setup'}</option>
            {/each}
          </select>
        </label>
        <label class="min-w-0 flex-1">
          <span class="mb-1 block text-[0.625rem] font-bold uppercase tracking-wider text-muted">Setup name</span>
          <input bind:value={presetName} maxlength="80" placeholder="Name this setup"
            class="w-full rounded-lg border border-line bg-[var(--surface)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
        </label>
        <div class="flex shrink-0 items-center gap-2">
          <button type="button" onclick={savePreset} title="Save this request, required text, and count"
            class="rounded-lg border border-line px-3 py-2 text-sm font-semibold transition hover:border-[var(--accent)]">{selectedPresetId ? 'Update' : 'Save'}</button>
          <button type="button" onclick={newPreset}
            class="rounded-lg border border-line px-3 py-2 text-sm font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink">New</button>
          {#if selectedPresetId}
            <button type="button" onclick={deletePreset}
              class="rounded-lg border border-line px-3 py-2 text-sm font-semibold text-[var(--danger)] transition hover:border-[var(--danger)]">Delete</button>
          {/if}
        </div>
      </div>
    </div>

    <label class="mb-1 block text-[0.625rem] font-bold uppercase tracking-wider text-muted" for="ff-instruction">Your request</label>
    <textarea id="ff-instruction" rows="3" bind:value={instruction}
      placeholder="What do you want, in character? e.g. give me vivid, in-character lines for the scene"
      class="w-full resize-y rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]"></textarea>

    <label class="mb-1 mt-3 block text-[0.625rem] font-bold uppercase tracking-wider text-muted" for="ff-prefix">Must be said each time</label>
    <input bind:value={prefix} maxlength="200" placeholder="Start each with… (optional, exact text) — e.g. Captain's log:"
      id="ff-prefix"
      class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />

    <div class="mt-3 flex flex-wrap items-center gap-3">
      <label class="flex items-center gap-2 text-sm font-semibold text-muted">
        How many
        <input type="number" min="1" max="30" bind:value={count}
          class="w-16 rounded-lg border border-line bg-[var(--surface-2)] px-2 py-1.5 text-sm text-ink outline-none focus:border-[var(--accent)]" />
      </label>
      <button type="button" onclick={run} disabled={!instruction.trim() || running}
        class="rounded-lg bg-[var(--accent)] px-5 py-2 text-sm font-bold text-[var(--on-accent)] disabled:opacity-40">
        {running ? 'Generating…' : 'Generate'}
      </button>
    </div>

    {#if items.length}
      <div class="mt-5 flex items-center justify-between">
        <span class="text-xs font-bold uppercase tracking-wider text-muted">{items.length} results</span>
        <button type="button" onclick={copyAll} class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]">Copy all</button>
      </div>
      <ol class="mt-3 space-y-2">
        {#each items as it, i (i)}
          <li class="flex items-start gap-3 rounded-lg border border-line bg-[var(--surface-2)] p-3">
            <span class="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full bg-[var(--surface-solid)] text-xs font-bold text-muted">{i + 1}</span>
            <textarea bind:value={items[i]} use:autosize={items[i]} rows="1" aria-label={`Result ${i + 1}`}
              class="min-w-0 flex-1 resize-none overflow-hidden whitespace-pre-wrap break-words rounded-md border border-transparent bg-transparent px-1.5 py-1 text-sm leading-relaxed text-ink outline-none transition focus:border-line focus:bg-[var(--surface-solid)]"></textarea>
            <div class="flex shrink-0 flex-col gap-1">
              <button type="button" onclick={() => addSavedResponse(items[i])} title="Save this response" class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">★ Save</button>
              <button type="button" onclick={() => copyItem(items[i])} class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">Copy</button>
            </div>
          </li>
        {/each}
      </ol>
    {/if}
  {/if}
</div>
