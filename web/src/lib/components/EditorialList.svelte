<script>
  import { favorites, toggleFavorite, removeMedia } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { items = [], onopen = () => {} } = $props();
  let confirming = $state(null);

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

<div class="mx-auto flex max-w-5xl flex-col" style="gap:var(--gap)">
  {#each items as it (it.id)}
    {@const fav = $favorites.has(it.id)}
    <article class="panel grid grid-cols-1 overflow-hidden rounded-card sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <button type="button" class="group relative block w-full overflow-hidden bg-black"
        style="aspect-ratio:{ratio(it)}; max-height:72vh" onclick={() => onopen(it, items)}>
        {#if it.thumb}
          <img src={it.thumb} alt="" loading="lazy" decoding="async"
               class="h-full w-full object-contain transition-transform duration-500 group-hover:scale-[1.02]" />
        {/if}
        {#if it.media_type === 'video'}
          <span class="absolute left-3 top-3 grid h-11 w-11 place-items-center rounded-full bg-black/55 text-white backdrop-blur">▶</span>
        {/if}
      </button>

      <div class="flex flex-col gap-3 p-5">
        <p class="text-lg font-semibold leading-snug">{it.prompt || 'Untitled prompt'}</p>
        <p class="text-sm text-muted">{[it.model, fmtDate(it.created_at), it.media_type].filter(Boolean).join(' · ')}</p>
        {#if it.tags?.length}
          <div class="flex flex-wrap gap-1.5">
            {#each it.tags.slice(0, 6) as tag}
              <span class="rounded-full border border-line px-2 py-0.5 text-xs text-muted">{tag}</span>
            {/each}
          </div>
        {/if}
        <div class="mt-auto flex gap-2 pt-2">
          <button type="button"
            class="rounded-lg border border-line px-3 py-2 text-sm font-semibold {fav ? 'text-[#ff5a7a]' : ''}"
            onclick={() => toggleFavorite(it.id)}>{fav ? '♥ Favorited' : '♡ Favorite'}</button>
          <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold"
            onclick={(e) => copy(e, it.prompt)}>Copy prompt</button>
          <button type="button" class="rounded-lg border border-red-500/50 px-3 py-2 text-sm font-semibold text-red-400 transition hover:bg-red-500/10"
            onclick={() => (confirming = it)}>Delete</button>
        </div>
      </div>
    </article>
  {/each}
</div>

{#if confirming}
  <ConfirmDialog title="Delete this item?"
    message="The file is permanently removed from disk and won't be re-downloaded on future syncs."
    confirmLabel="Delete"
    onconfirm={() => { removeMedia([confirming.id]); confirming = null; }}
    oncancel={() => (confirming = null)} />
{/if}
