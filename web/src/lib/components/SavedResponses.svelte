<script>
  import { savedResponses, addSavedResponse, removeSavedResponse } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import { toast } from '$lib/toast.js';

  // A library of starred Prompt Studio outputs (Scene beats, Freeform items, Variations) plus any
  // prompt you add by hand here. Server-persisted via state.js, so they survive reloads and follow
  // you across devices.
  let q = $state('');
  let draft = $state('');
  const shown = $derived(
    ($savedResponses || []).filter((r) => !q.trim() || r.text.toLowerCase().includes(q.trim().toLowerCase()))
  );
  async function copy(t) {
    const ok = await copyText(t);
    toast(ok ? 'Copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }
  function add() {
    if (addSavedResponse(draft)) draft = ''; // clear only when it actually saved (not on dupe/empty)
  }
  // Ctrl/⌘+Enter saves without reaching for the button.
  function onKey(e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); add(); }
  }
</script>

<div class="mx-auto w-full max-w-3xl">
  <div class="mb-3 text-sm text-muted">{$savedResponses.length} saved {$savedResponses.length === 1 ? 'response' : 'responses'}</div>

  <!-- Manually add a prompt straight to the library (no generation needed). -->
  <div class="mb-3 rounded-lg border border-line bg-[var(--surface-2)] p-3">
    <textarea bind:value={draft} onkeydown={onKey} rows="2" placeholder="Add a prompt by hand…"
      class="w-full resize-y rounded-md border border-line bg-[var(--surface)] px-3 py-2 text-sm leading-relaxed text-ink outline-none placeholder:text-muted focus:border-[var(--accent)]"></textarea>
    <div class="mt-2 flex items-center justify-between gap-2">
      <span class="text-[0.6875rem] text-muted">⌘/Ctrl + Enter to save</span>
      <button type="button" onclick={add} disabled={!draft.trim()}
        class="rounded-md border border-line px-3 py-1 text-xs font-semibold transition enabled:hover:border-[var(--accent)] disabled:opacity-40">+ Add prompt</button>
    </div>
  </div>

  <input type="search" placeholder="Search saved responses…" bind:value={q}
    class="mb-3 w-full rounded-full border border-line bg-[var(--surface-2)] px-3.5 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
  {#if shown.length === 0}
    <p class="py-12 text-center text-sm text-muted">
      {$savedResponses.length ? 'No matches.' : 'No saved responses yet — add one above, or hit ★ Save on any result in Scene or Freeform.'}
    </p>
  {:else}
    <ul class="space-y-2">
      {#each shown as r (r.id)}
        <li class="flex items-start gap-3 rounded-lg border border-line bg-[var(--surface-2)] p-3">
          <p class="min-w-0 flex-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-ink">{r.text}</p>
          <div class="flex shrink-0 flex-col gap-1">
            <button type="button" onclick={() => copy(r.text)} class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">Copy</button>
            <button type="button" onclick={() => removeSavedResponse(r.id)} class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold text-[var(--danger)] transition hover:border-[var(--danger)]">Delete</button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>
