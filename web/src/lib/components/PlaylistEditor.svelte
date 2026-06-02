<script>
  import { onMount } from 'svelte';
  import { updatePlaylist, removePlaylist } from '$lib/state.js';
  import { mediaByIds, exportSelection } from '$lib/api.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  let { playlist, onclose = () => {}, onplay = () => {} } = $props();

  let name = $state(playlist.name);
  let ids = $state([...playlist.ids]);
  let media = $state({});
  let dragId = $state(null);
  let busy = $state(false);
  let confirming = $state(false);

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
    busy = true;
    try { await exportSelection(videos.map((v) => v.id)); } catch (e) { alert(e.message); } finally { busy = false; }
  }
  function del() {
    removePlaylist(playlist.id);
    confirming = false;
    onclose();
  }
</script>

<div class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur" onclick={(e) => { if (e.target === e.currentTarget) close(); }}>
  <div class="panel flex max-h-[86vh] w-full max-w-3xl flex-col overflow-hidden rounded-card">
    <div class="flex items-center gap-3 border-b border-line p-4">
      <input class="flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-base font-bold outline-none" bind:value={name} maxlength="80" />
      <button class="rounded-lg bg-[var(--accent)] px-4 py-2 font-bold text-white" onclick={close}>Done</button>
    </div>
    <p class="px-4 pt-3 text-xs text-muted">Drag the handle (or ▲/▼) to reorder. Videos play top to bottom.</p>

    <div class="flex flex-col gap-2 overflow-auto p-4">
      {#each ids as id, idx (id)}
        {@const it = media[id]}
        <div class="flex items-start gap-2.5 rounded-lg border border-line bg-[var(--surface-2)] p-2"
             draggable="true"
             ondragstart={() => (dragId = id)}
             ondragover={(e) => e.preventDefault()}
             ondrop={() => onDrop(id)}>
          <span class="cursor-grab px-1 pt-1.5 text-muted">☰</span>
          {#if it?.thumb}<img src={it.thumb} alt="" class="h-10 w-14 shrink-0 rounded object-cover" />{:else}<span class="h-10 w-14 shrink-0 rounded bg-black/40"></span>{/if}
          <span class="w-5 shrink-0 pt-0.5 text-right text-xs font-bold text-muted">{idx + 1}</span>
          <span class="min-w-0 flex-1 break-words pt-0.5 text-sm leading-snug">{it?.prompt || it?.local_path?.split('/').pop() || id}</span>
          <button class="grid h-7 w-7 shrink-0 place-items-center rounded border border-line" disabled={idx === 0} onclick={() => move(id, -1)}>▲</button>
          <button class="grid h-7 w-7 shrink-0 place-items-center rounded border border-line" disabled={idx === ids.length - 1} onclick={() => move(id, 1)}>▼</button>
          <button class="grid h-7 w-7 shrink-0 place-items-center rounded border border-line" onclick={() => remove(id)}>×</button>
        </div>
      {/each}
      {#if !ids.length}<p class="py-6 text-center text-sm text-muted">This playlist is empty.</p>{/if}
    </div>

    <div class="flex gap-2 border-t border-line p-4">
      <button class="rounded-lg bg-[var(--accent)] px-4 py-2 font-bold text-white disabled:opacity-50" disabled={!videos.length} onclick={play}>Play</button>
      <button class="rounded-lg border border-line px-4 py-2 font-semibold disabled:opacity-50" disabled={!videos.length || busy} onclick={doExport}>{busy ? 'Exporting…' : 'Export'}</button>
      <button class="ml-auto rounded-lg border border-line px-4 py-2 font-semibold" onclick={() => (confirming = true)}>Delete playlist</button>
    </div>
  </div>
</div>

{#if confirming}
  <ConfirmDialog title="Delete playlist?" message={`“${playlist.name}” will be permanently removed. This can't be undone.`}
    confirmLabel="Delete" onconfirm={del} oncancel={() => (confirming = false)} />
{/if}
