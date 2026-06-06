<script>
  import { savedResponses, removeSavedResponse } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import { toast } from '$lib/toast.js';

  // A library of starred Prompt Studio outputs (Scene beats, Freeform items, Variations). Server-
  // persisted via state.js, so they survive reloads and follow you across devices.
  let q = $state('');
  const shown = $derived(
    ($savedResponses || []).filter((r) => !q.trim() || r.text.toLowerCase().includes(q.trim().toLowerCase()))
  );
  async function copy(t) {
    const ok = await copyText(t);
    toast(ok ? 'Copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }
</script>

<div class="mx-auto w-full max-w-3xl">
  <div class="mb-3 text-sm text-muted">{$savedResponses.length} saved {$savedResponses.length === 1 ? 'response' : 'responses'}</div>
  <input type="search" placeholder="Search saved responses…" bind:value={q}
    class="mb-3 w-full rounded-full border border-line bg-[var(--surface-2)] px-3.5 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
  {#if shown.length === 0}
    <p class="py-12 text-center text-sm text-muted">
      {$savedResponses.length ? 'No matches.' : 'No saved responses yet — hit ★ Save on any result in Scene or Freeform.'}
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
