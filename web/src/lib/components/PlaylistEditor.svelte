<script>
  import { onMount } from 'svelte';
  import { updatePlaylist, removePlaylist, shuffled } from '$lib/state.js';
  import { mediaByIds, exportSelection } from '$lib/api.js';
  import { copyText } from '$lib/clipboard.js';
  import { toast } from '$lib/toast.js';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { playlist, onclose = () => {}, onplay = () => {}, onexportorder = null } = $props();

  let name = $state(playlist.name);
  let ids = $state([...playlist.ids]);
  let media = $state({});
  let dragId = $state(null);
  let busy = $state(false);
  let confirming = $state(false);
  let expanded = $state({}); // id -> bool: prompt rolled out to full text
  function toggleExpand(id) { expanded = { ...expanded, [id]: !expanded[id] }; }

  // Smoothly roll the prompt between its 2-line clamp and full height. CSS can't
  // transition `height: auto`, so we animate max-height between the collapsed
  // floor and the measured scrollHeight, then release to `none` when open.
  const PROMPT_COLLAPSED = 40; // px ≈ 2 lines at text-sm / leading-snug
  function reveal(node, open) {
    node.style.overflow = 'hidden';
    node.style.maxHeight = open ? 'none' : PROMPT_COLLAPSED + 'px';
    let cur = !!open;
    return {
      update(next) {
        next = !!next;
        if (next === cur) return;
        cur = next;
        node.style.maxHeight = node.offsetHeight + 'px'; // pin current height
        void node.offsetHeight;                          // force reflow
        requestAnimationFrame(() => {
          node.style.maxHeight = (next ? node.scrollHeight : PROMPT_COLLAPSED) + 'px';
        });
        const done = () => {
          if (cur) node.style.maxHeight = 'none';        // let open rows reflow freely
          node.removeEventListener('transitionend', done);
        };
        node.addEventListener('transitionend', done);
      }
    };
  }

  onMount(async () => {
    const list = await mediaByIds(ids);
    const m = {};
    for (const it of list) m[it.id] = it;
    media = m;
  });

  const videos = $derived(ids.map((id) => media[id]).filter((v) => v && v.media_type === 'video'));

  function move(id, d) {
    const a = [...ids];
    const from = a.indexOf(id);
    const to = from + d;
    if (to < 0 || to >= a.length) return;
    a.splice(to, 0, a.splice(from, 1)[0]);
    ids = a;
  }
  function remove(id) { ids = ids.filter((x) => x !== id); }
  // Randomize rewrites the SAVED order, same as a drag does — it flows out through the
  // ordinary commit() on Done/Play, so what you see here is what plays everywhere the
  // playlist is used. Re-clickable: each press is a fresh shuffle.
  function randomize() {
    ids = shuffled(ids);
    toast('Playlist order randomized', { type: 'success' });
  }
  async function copyPrompt(id) {
    const text = media[id]?.prompt;
    if (!text) return;
    const ok = await copyText(text);
    toast(ok ? 'Prompt copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }

  function onDrop(targetId) {
    if (!dragId || dragId === targetId) return;
    const a = [...ids];
    a.splice(a.indexOf(targetId), 0, a.splice(a.indexOf(dragId), 1)[0]);
    ids = a;
    dragId = null;
  }

  function commit() {
    updatePlaylist(playlist.id, { name: name.trim() || playlist.name, ids });
  }
  function close() { commit(); onclose(); }
  function play() { commit(); onplay(videos, name.trim() || playlist.name); }
  async function doExport() {
    // 2+ videos: hand off to the shared merge modal (final order check + the
    // Cinematic-intro option), committing the edited order first so the two agree.
    // The editor stays open underneath — cancelling the merge returns here.
    if (onexportorder && videos.length > 1) { commit(); onexportorder(videos, name.trim() || playlist.name); return; }
    busy = true;
    try { await exportSelection(videos.map((v) => v.id)); } catch (e) { toast(e.message || 'Export failed.', { type: 'error' }); } finally { busy = false; }
  }
  function del() {
    removePlaylist(playlist.id);
    confirming = false;
    onclose();
  }
</script>

<!-- closeOnEscape defers to the nested delete-confirm dialog: while it's open, Escape
     closes the confirm (its own handler) rather than the editor. -->
<Modal onclose={close} ariaLabel="Edit playlist" z="z-50" overlay="overlay-strong" closeOnEscape={!confirming}
       panelClass="panel flex max-h-[88dvh] w-full max-w-3xl flex-col overflow-hidden rounded-card">
    <div class="flex items-center gap-3 border-b border-line p-4">
      <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-base font-bold outline-none transition focus:border-[var(--accent)]" bind:value={name} maxlength="80" aria-label="Playlist name" />
      <Button class="shrink-0" onclick={close}>Done</Button>
    </div>
    <div class="flex items-center justify-between gap-3 px-4 pt-3 text-xs text-muted">
      <span>Drag the handle (or ▲/▼) to reorder · plays top to bottom</span>
      <span class="shrink-0 tabular-nums">{ids.length} {ids.length === 1 ? 'item' : 'items'} · {videos.length} video{videos.length === 1 ? '' : 's'}</span>
    </div>

    <div class="flex flex-col gap-2 overflow-auto p-4">
      {#each ids as id, idx (id)}
        {@const it = media[id]}
        <!-- Drag-to-reorder is a pointer enhancement; keyboard users reorder with the ▲/▼ buttons. -->
        <div class="group flex items-center gap-3 rounded-lg border border-line bg-[var(--surface-2)] p-2 transition hover:border-[color-mix(in_srgb,var(--accent)_45%,var(--line))]" role="presentation"
             draggable="true"
             ondragstart={() => (dragId = id)}
             ondragover={(e) => e.preventDefault()}
             ondrop={() => onDrop(id)}>
          <span class="cursor-grab select-none px-0.5 text-base text-muted transition hover:text-ink" title="Drag to reorder" aria-hidden="true">☰</span>
          <div class="relative h-16 w-28 shrink-0 overflow-hidden rounded-md bg-[var(--media-placeholder)]">
            {#if it?.thumb}<img src={it.thumb} alt="" class="h-full w-full object-cover" style="object-position: {it.media_h > it.media_w ? '50% 22%' : '50% 50%'}" />{/if}
            <span class="absolute left-1 top-1 grid h-4 min-w-4 place-items-center rounded-sm bg-[var(--overlay-strong)] px-1 text-[0.625rem] font-bold leading-none text-[var(--media-control-ink)] tabular-nums">{idx + 1}</span>
          </div>
          <button type="button"
            class="-mx-1 min-w-0 flex-1 cursor-pointer rounded-md px-1 py-0.5 text-left text-sm leading-snug transition-colors hover:bg-[color-mix(in_srgb,var(--ink)_7%,transparent)]"
            aria-expanded={expanded[id] || false}
            title={expanded[id] ? 'Collapse' : 'Expand full prompt'}
            onclick={() => toggleExpand(id)}><span class="prompt-roll block break-words" use:reveal={expanded[id] || false}>{it?.prompt || it?.local_path?.split('/').pop() || id}</span></button>
          <div class="flex shrink-0 items-center gap-1.5">
            <button class="grid h-7 w-7 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-md border border-line text-muted transition hover:border-[var(--accent)] hover:bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] hover:text-ink disabled:opacity-35 disabled:hover:border-line disabled:hover:bg-transparent disabled:hover:text-muted" disabled={!it?.prompt} onclick={() => copyPrompt(id)} aria-label="Copy prompt" title="Copy prompt">⧉</button>
            <div class="flex overflow-hidden rounded-md border border-line">
              <button class="grid h-7 w-7 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 text-xs transition hover:bg-[var(--surface-solid)] disabled:opacity-35 disabled:hover:bg-transparent" disabled={idx === 0} onclick={() => move(id, -1)} aria-label="Move up">▲</button>
              <span class="w-px self-stretch bg-line" aria-hidden="true"></span>
              <button class="grid h-7 w-7 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 text-xs transition hover:bg-[var(--surface-solid)] disabled:opacity-35 disabled:hover:bg-transparent" disabled={idx === ids.length - 1} onclick={() => move(id, 1)} aria-label="Move down">▼</button>
            </div>
            <button class="grid h-7 w-7 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-md border border-line text-muted transition hover:border-[var(--danger)] hover:bg-[color-mix(in_srgb,var(--danger)_14%,transparent)] hover:text-[var(--danger-ink)]" onclick={() => remove(id)} aria-label="Remove from playlist">×</button>
          </div>
        </div>
      {/each}
      {#if !ids.length}
        <div class="grid place-items-center rounded-lg border border-dashed border-line py-12 text-center text-muted">
          <p class="text-sm">This playlist is empty.</p>
        </div>
      {/if}
    </div>

    <div class="flex items-center gap-2 border-t border-line p-4">
      <Button disabled={!videos.length} onclick={play}>Play</Button>
      <Button variant="secondary" disabled={ids.length < 2} onclick={randomize} title="Shuffle this playlist into a new saved order">
        <span class="inline-flex items-center gap-1.5">
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"/><path d="m18 2 4 4-4 4"/><path d="M2 6h1.9c1.5 0 2.9.9 3.6 2.2"/><path d="M22 18h-5.9c-1.3 0-2.6-.7-3.3-1.8l-.5-.8"/><path d="m18 14 4 4-4 4"/></svg>
          Randomize
        </span>
      </Button>
      <Button variant="secondary" disabled={!videos.length || busy} onclick={doExport}>{busy ? 'Exporting…' : 'Export'}</Button>
      <button class="ml-auto rounded-lg border border-[var(--danger-border-strong)] px-4 py-2 font-semibold text-[var(--danger-ink)] transition hover:border-[var(--danger)] hover:bg-[var(--danger-bg)]" onclick={() => (confirming = true)}>Delete playlist</button>
    </div>
</Modal>

{#if confirming}
  <ConfirmDialog title="Delete playlist?" message={`“${playlist.name}” will be permanently removed. This can't be undone.`}
    confirmLabel="Delete" onconfirm={del} oncancel={() => (confirming = false)} />
{/if}

<style>
  /* The reveal() action animates max-height between the 2-line floor and the
     measured full height; this is the easing it transitions along. */
  .prompt-roll { transition: max-height 220ms ease; }
</style>
