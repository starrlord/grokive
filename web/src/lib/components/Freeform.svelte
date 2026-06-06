<script>
  import { generateFreeform } from '$lib/api.js';
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

    <label class="mb-1 block text-[0.625rem] font-bold uppercase tracking-wider text-muted" for="ff-instruction">Your request</label>
    <textarea id="ff-instruction" rows="3" bind:value={instruction}
      placeholder="What do you want, in character? e.g. give me vivid, in-character lines for the scene"
      class="w-full resize-y rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]"></textarea>

    <input bind:value={prefix} maxlength="200" placeholder="Start each with… (optional, exact text) — e.g. Captain's log:"
      class="mt-3 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />

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
            <p class="min-w-0 flex-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-ink">{it}</p>
            <div class="flex shrink-0 flex-col gap-1">
              <button type="button" onclick={() => addSavedResponse(it)} title="Save this response" class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">★ Save</button>
              <button type="button" onclick={() => copyItem(it)} class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">Copy</button>
            </div>
          </li>
        {/each}
      </ol>
    {/if}
  {/if}
</div>
