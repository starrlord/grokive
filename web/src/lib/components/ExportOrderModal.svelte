<script>
  // Explicit "arrange before you merge" step. Videos arrive here in whatever order the
  // grid/selection produced (newest-first by default, and in the grouped view not even
  // in the on-screen family order) — so the merge used to come out back-to-front. Here
  // the user drags / ▲▼ them into the exact sequence the combined MP4 should play, then
  // exports. This is the ONLY place the merge order is chosen deliberately; the backend
  // concatenates ids in the order it's handed, so top-to-bottom here == playback order.
  import { exportSelection } from '$lib/api.js';
  import { toast } from '$lib/toast.js';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';

  let { items = [], name = 'selection', onclose = () => {} } = $props();

  // A local working copy: reordering or dropping a clip here never touches the real
  // selection store. Seeded once from `items` (the modal is remounted per open, so the
  // initial prop is always the fresh list) and video-only — a merge takes videos.
  let list = $state((items || []).filter((v) => v && v.media_type === 'video'));
  let dragId = $state(null);
  let busy = $state(false);

  function move(id, d) {
    const a = [...list];
    const from = a.findIndex((v) => v.id === id);
    const to = from + d;
    if (from < 0 || to < 0 || to >= a.length) return;
    a.splice(to, 0, a.splice(from, 1)[0]);
    list = a;
  }
  function remove(id) { list = list.filter((v) => v.id !== id); }
  // Firefox won't start an HTML5 drag session unless dragstart writes a payload (and
  // sets effectAllowed) — same quirk SavedResponses.svelte already works around. Without
  // it, drag-reorder silently no-ops on Firefox (the ▲/▼ buttons still work).
  function startDrag(e, id) {
    dragId = id;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', id); } catch {}
    }
  }
  function onDrop(targetId) {
    if (!dragId || dragId === targetId) return;
    const a = [...list];
    const from = a.findIndex((v) => v.id === dragId);
    const at = a.findIndex((v) => v.id === targetId);
    if (from < 0 || at < 0) return;
    a.splice(at, 0, a.splice(from, 1)[0]);
    list = a;
    dragId = null;
  }

  async function doExport() {
    if (!list.length || busy) return;
    busy = true;
    try {
      await exportSelection(list.map((v) => v.id), name);
      toast(`Exported ${list.length} video${list.length === 1 ? '' : 's'}`, { type: 'success' });
      onclose();
    } catch (e) {
      toast(e?.message || 'Export failed.', { type: 'error' });
    } finally {
      busy = false;
    }
  }
</script>

<!-- While a merge is preparing, freeze dismissal (Escape / click-outside) so the export
     can't be orphaned mid-request; the Cancel button is disabled to match. -->
<Modal onclose={busy ? () => {} : onclose} ariaLabel="Arrange videos before export" z="z-50" overlay="overlay-strong" closeOnEscape={!busy}
       panelClass="panel flex max-h-[88dvh] w-full max-w-2xl flex-col overflow-hidden rounded-card">
  <div class="flex items-center gap-3 border-b border-line p-4">
    <div class="min-w-0 flex-1">
      <p class="text-base font-bold text-ink">Arrange before merging</p>
      <p class="text-xs text-muted">Drag the handle (or ▲/▼) to set play order · the top clip plays first</p>
    </div>
    <span class="shrink-0 tabular-nums text-xs text-muted">{list.length} video{list.length === 1 ? '' : 's'}</span>
  </div>

  <div class="flex flex-col gap-2 overflow-auto p-4">
    {#each list as it, idx (it.id)}
      <!-- Drag-to-reorder is a pointer enhancement; keyboard users reorder with ▲/▼. -->
      <div class="group flex items-center gap-3 rounded-lg border border-line bg-[var(--surface-2)] p-2 transition hover:border-[color-mix(in_srgb,var(--accent)_45%,var(--line))]" role="presentation"
           draggable="true"
           ondragstart={(e) => startDrag(e, it.id)}
           ondragover={(e) => e.preventDefault()}
           ondrop={() => onDrop(it.id)}>
        <span class="cursor-grab select-none px-0.5 text-base text-muted transition hover:text-ink" title="Drag to reorder" aria-hidden="true">☰</span>
        <div class="relative h-16 w-28 shrink-0 overflow-hidden rounded-md bg-[var(--media-placeholder)]">
          {#if it.thumb}<img src={it.thumb} alt="" class="h-full w-full object-cover" style="object-position: {it.media_h > it.media_w ? '50% 22%' : '50% 50%'}" />{/if}
          <span class="absolute left-1 top-1 grid h-4 min-w-4 place-items-center rounded-sm bg-[var(--overlay-strong)] px-1 text-[0.625rem] font-bold leading-none text-[var(--media-control-ink)] tabular-nums">{idx + 1}</span>
        </div>
        <p class="min-w-0 flex-1 truncate text-sm leading-snug text-ink" title={it.prompt || ''}>{it.prompt || it.local_path?.split('/').pop() || it.id}</p>
        <div class="flex shrink-0 items-center gap-1.5">
          <div class="flex overflow-hidden rounded-md border border-line">
            <button type="button" class="grid h-7 w-7 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 text-xs transition hover:bg-[var(--surface-solid)] disabled:opacity-35 disabled:hover:bg-transparent" disabled={idx === 0} onclick={() => move(it.id, -1)} aria-label="Move up">▲</button>
            <span class="w-px self-stretch bg-line" aria-hidden="true"></span>
            <button type="button" class="grid h-7 w-7 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 text-xs transition hover:bg-[var(--surface-solid)] disabled:opacity-35 disabled:hover:bg-transparent" disabled={idx === list.length - 1} onclick={() => move(it.id, 1)} aria-label="Move down">▼</button>
          </div>
          <button type="button" class="grid h-7 w-7 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-md border border-line text-muted transition hover:border-[var(--danger)] hover:bg-[color-mix(in_srgb,var(--danger)_14%,transparent)] hover:text-[var(--danger-ink)]" onclick={() => remove(it.id)} aria-label="Remove from export">×</button>
        </div>
      </div>
    {/each}
    {#if !list.length}
      <div class="grid place-items-center rounded-lg border border-dashed border-line py-12 text-center text-muted">
        <p class="text-sm">Nothing to merge.</p>
      </div>
    {/if}
  </div>

  <div class="flex items-center gap-2 border-t border-line p-4">
    <span class="hidden text-xs text-muted sm:inline">Combined into one MP4, top to bottom.</span>
    <Button class="ml-auto" variant="secondary" disabled={busy} onclick={onclose}>Cancel</Button>
    <Button disabled={!list.length || busy} onclick={doExport}>{busy ? 'Preparing…' : 'Export merged'}</Button>
  </div>
</Modal>
