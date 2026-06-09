<script>
  import { fly } from 'svelte/transition';
  import { describeImage } from '$lib/api.js';
  import { saveResponseToStudio } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';

  // Sits over the bottom of the lightbox (above the Info panel). The parent only renders
  // this for images, so `item` is always an image with an `id` + stored `prompt`.
  let { item, onclose = () => {} } = $props();

  let loading = $state(false);
  let error = $state('');
  let draft = $state('');
  let model = $state('');
  let saved = $state(false);
  let saving = $state(false);
  let copyLabel = $state('Copy');

  async function generate() {
    const id = item?.id;
    if (!id) return;
    loading = true;
    error = '';
    saved = false;
    try {
      const res = await describeImage(id);
      // The clip may have changed under us while the (slow) call was in flight.
      if (item?.id !== id) return;
      draft = res.prompt || '';
      model = res.model || '';
      if (!draft) error = 'The model returned nothing usable. Try Regenerate.';
    } catch (e) {
      if (item?.id !== id) return;
      error = e?.message || 'Generation failed.';
    } finally {
      if (item?.id === id) loading = false;
    }
  }

  // Auto-run on open and whenever the lightbox advances to a different image.
  $effect(() => {
    item?.id; // track the current image
    draft = '';
    error = '';
    saved = false;
    generate();
  });

  async function save() {
    if (saving) return;
    saving = true;
    // Server-side append (read-modify-write): never overwrites the user's other saved
    // prompts, even though this overlay never loads the full list.
    const ok = await saveResponseToStudio(draft, { folder: 'From Image' });
    saving = false;
    if (ok) saved = true;
  }

  async function copy() {
    const ok = await copyText(draft);
    copyLabel = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => (copyLabel = 'Copy'), 1200);
  }
</script>

<div class="panel absolute inset-x-0 bottom-0 z-30 max-h-[60dvh] overflow-auto px-6 py-4"
     transition:fly={{ y: 240, duration: 180 }}>
  <div class="mb-3 flex items-center gap-2">
    <svg viewBox="0 0 24 24" class="h-5 w-5 shrink-0 text-[var(--accent)]" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg>
    <h2 class="flex-1 text-xs font-bold uppercase tracking-wide text-muted">Describe for Grok</h2>
    <button class="shrink-0 rounded-sm border border-line px-2 py-0.5 text-xs" onclick={onclose}>Hide</button>
  </div>

  {#if loading}
    <p class="py-4 text-sm text-muted">Looking at the image and writing a prompt… a local vision model can take a little while.</p>
  {:else if error}
    <p class="mb-3 text-sm text-[var(--danger-ink)]">{error}</p>
    <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={generate}>Try again</button>
  {:else}
    <textarea bind:value={draft}
      class="mb-3 h-40 w-full resize-y rounded-lg border border-line bg-[var(--surface-2)] p-3 text-sm leading-relaxed outline-none"
      placeholder="The generated prompt will appear here — edit it freely before saving."></textarea>
    <div class="flex flex-wrap items-center gap-2">
      <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold disabled:opacity-50 {saved ? 'text-[var(--success-ink)]' : ''}"
        disabled={!draft.trim() || saving} onclick={save}>{saved ? '✓ Saved to Prompt Studio' : (saving ? 'Saving…' : 'Save to Prompt Studio')}</button>
      <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold disabled:opacity-50"
        disabled={!draft.trim()} onclick={copy}>{copyLabel}</button>
      <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" onclick={generate}>Regenerate</button>
      {#if model}<span class="ml-auto truncate text-xs text-muted" title={model}>{model}</span>{/if}
    </div>
  {/if}
</div>
