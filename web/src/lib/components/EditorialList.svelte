<script>
  import { favorites, filters, toggleTag, toggleFavorite, removeMedia, collections, deleteMembershipNote } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  // `filterable` turns the tag chips into buttons that toggle the grid's tag filter.
  // Off by default — and intentionally left off in collection views, where a filter
  // change doesn't refetch the collection's items, so a click would do nothing.
  // `collection` + `onremovefromcollection` (collection views only) let the delete
  // confirm offer "remove from this collection" as the safe alternative.
  let { items = [], onopen = () => {}, filterable = false, collection = null, onremovefromcollection = () => {} } = $props();
  let confirming = $state(null);
  // Members only: grouped view can surface lineage items not in the open collection.
  const confirmInCollection = $derived(!!(confirming && collection &&
    (collection.ids || []).some((mid) => String(mid) === String(confirming.id))));
  const confirmNote = $derived(confirming ? deleteMembershipNote($collections, [confirming.id], collection?.id || '') : '');

  const fmtDate = (s) => (s || '').slice(0, 10);
  const ratio = (it) => (it.thumb_w && it.thumb_h ? `${it.thumb_w}/${it.thumb_h}` : '16/9');

  async function copy(e, text) {
    const b = e.currentTarget;
    const ok = await copyText(text);
    const prev = b.textContent;
    b.textContent = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => (b.textContent = prev), 1200);
  }
</script>

<!-- Magazine tiles: 1 col on phones (image-on-top, same as before), 2 cols from md up.
     items-start keeps each tile its natural height (ragged bottoms) so portrait and
     landscape neighbours don't stretch each other — and it stays stable under the
     page's infinite scroll, unlike a column/masonry layout that rebalances on append. -->
<div class="mx-auto grid max-w-6xl grid-cols-1 items-start md:grid-cols-2" style="gap:var(--gap)">
  {#each items as it (it.id)}
    {@const fav = $favorites.has(it.id)}
    <article class="panel flex flex-col overflow-hidden rounded-card">
      <button type="button" class="group relative block w-full overflow-hidden bg-[var(--media-bg)]"
        style="aspect-ratio:{ratio(it)}; max-height:56vh" onclick={() => onopen(it, items)}>
        {#if it.thumb}
          <img src={it.thumb} alt="" loading="lazy" decoding="async"
               class="h-full w-full object-contain transition-transform duration-500 group-hover:scale-[1.02]" />
        {/if}
        {#if it.media_type === 'video'}
          <span class="absolute left-3 top-3 grid h-11 w-11 place-items-center rounded-full bg-[var(--media-play-bg)] text-[var(--media-control-ink)] backdrop-blur-sm">▶</span>
        {/if}
      </button>

      <div class="flex flex-1 flex-col gap-3 p-5">
        <p class="text-lg font-semibold leading-snug">{it.prompt || 'Untitled prompt'}</p>
        <p class="text-sm text-muted">{[it.model, fmtDate(it.created_at), it.media_type].filter(Boolean).join(' · ')}</p>
        {#if it.tags?.length}
          <div class="flex flex-wrap gap-1.5">
            {#each it.tags.slice(0, 6) as tag (tag)}
              {#if filterable}
                {@const active = $filters.tags.includes(tag)}
                <button type="button"
                  class="rounded-full border px-2 py-0.5 text-xs transition {active ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line text-muted hover:border-[var(--accent)]'}"
                  onclick={() => toggleTag(tag)}>{tag}</button>
              {:else}
                <span class="rounded-full border border-line px-2 py-0.5 text-xs text-muted">{tag}</span>
              {/if}
            {/each}
          </div>
        {/if}
        <div class="mt-auto flex flex-wrap gap-2 pt-2">
          <button type="button"
            class="rounded-lg border border-line px-3 py-2 text-sm font-semibold transition hover:border-[var(--accent)] {fav ? 'text-[var(--favorite)]' : ''}"
            onclick={() => toggleFavorite(it.id)}>{fav ? '♥ Favorited' : '♡ Favorite'}</button>
          <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold transition hover:border-[var(--accent)]"
            onclick={(e) => copy(e, it.prompt)}>Copy prompt</button>
          <button type="button" class="rounded-lg border border-[var(--danger-border-strong)] px-3 py-2 text-sm font-semibold text-[var(--danger-ink)] transition hover:bg-[var(--danger-bg)]"
            onclick={() => (confirming = it)}>Delete</button>
        </div>
      </div>
    </article>
  {/each}
</div>

{#if confirming}
  <ConfirmDialog title="Delete this item?"
    message="The file is permanently removed from disk and won't be re-downloaded on future syncs."
    note={confirmNote}
    confirmLabel={confirmInCollection ? 'Delete permanently' : 'Delete'}
    altLabel={confirmInCollection ? 'Remove from this collection' : ''}
    onalt={() => { onremovefromcollection(confirming.id); confirming = null; }}
    onconfirm={() => { removeMedia([confirming.id]); confirming = null; }}
    oncancel={() => (confirming = null)} />
{/if}
