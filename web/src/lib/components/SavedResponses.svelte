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

  // Long responses are clamped to a few lines and roll open on click — the same reveal the
  // Playlist Editor uses for prompts. Short ones render in full with no expand affordance.
  let expanded = $state({}); // id -> bool
  function toggleExpand(id) { expanded = { ...expanded, [id]: !expanded[id] }; }
  const needsClamp = (t) => {
    const s = String(t || '');
    return s.length > 180 || (s.match(/\n/g)?.length || 0) >= 3;
  };

  // CSS can't transition height:auto, so animate max-height between the collapsed floor and the
  // measured scrollHeight, then release to `none` when open so it can reflow freely.
  const RESP_COLLAPSED = 66; // px ≈ 3 lines at text-sm / leading-relaxed
  function reveal(node, open) {
    node.style.overflow = 'hidden';
    node.style.maxHeight = open ? 'none' : RESP_COLLAPSED + 'px';
    let cur = !!open;
    return {
      update(next) {
        next = !!next;
        if (next === cur) return;
        cur = next;
        node.style.maxHeight = node.offsetHeight + 'px'; // pin current height
        void node.offsetHeight;                          // force reflow
        requestAnimationFrame(() => {
          node.style.maxHeight = (next ? node.scrollHeight : RESP_COLLAPSED) + 'px';
        });
        const done = () => {
          if (cur) node.style.maxHeight = 'none';
          node.removeEventListener('transitionend', done);
        };
        node.addEventListener('transitionend', done);
      }
    };
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
          {#if needsClamp(r.text)}
            <div class="min-w-0 flex-1">
              <button type="button" class="block w-full cursor-pointer text-left" aria-expanded={expanded[r.id] || false}
                title={expanded[r.id] ? 'Collapse' : 'Expand full prompt'} onclick={() => toggleExpand(r.id)}>
                <span class="resp-roll block whitespace-pre-wrap break-words text-sm leading-relaxed text-ink" use:reveal={expanded[r.id] || false}>{r.text}</span>
                <span class="mt-1 inline-block text-xs font-semibold text-[var(--accent)]">{expanded[r.id] ? 'Show less ⌃' : 'Show more ⌄'}</span>
              </button>
            </div>
          {:else}
            <p class="min-w-0 flex-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-ink">{r.text}</p>
          {/if}
          <div class="flex shrink-0 flex-col gap-1">
            <button type="button" onclick={() => copy(r.text)} class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]">Copy</button>
            <button type="button" onclick={() => removeSavedResponse(r.id)} class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold text-[var(--danger)] transition hover:border-[var(--danger)]">Delete</button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  /* reveal() animates max-height between the clamped floor and full height; this is the easing. */
  .resp-roll { transition: max-height 220ms ease; }
</style>
