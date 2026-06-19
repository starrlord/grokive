<script>
  // Folder import overlay. Receives the FileList from a webkitdirectory picker,
  // filters to supported media, then streams each file to /api/import/file with live
  // progress before committing them into a new collection named after the top folder.
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import ParticleField from './ParticleField.svelte';
  import { importFile, importCommit, importCancel } from '$lib/api.js';
  import { collections } from '$lib/state.js';

  let { files, onclose = () => {}, oncreated = () => {} } = $props();

  // Where the imported files land: a brand-new collection, or appended to an existing
  // one. Sealed collections (locked + not unlocked this session) can't be a target —
  // they're hidden here just as they are in the "Add to Collection" picker.
  let target = $state('new'); // 'new' | 'existing'
  let existingId = $state('');
  const pickable = $derived(($collections || []).filter((c) => !(c.locked && !c.unlocked)));
  // Keep the destination valid as the collections store changes underneath (a relock or
  // unlock-expiry can drop the chosen collection): clear a selection that left the list,
  // and fall back to "new" when there's nothing eligible to add to.
  $effect(() => {
    if (!pickable.length) { target = 'new'; existingId = ''; }
    else if (existingId && !pickable.some((c) => c.id === existingId)) existingId = '';
  });

  const VIDEO = ['mp4', 'webm', 'm4v', 'mov'];
  const IMAGE = ['jpg', 'jpeg', 'png', 'webp', 'gif', 'avif'];
  const ALLOWED = new Set([...VIDEO, ...IMAGE]);
  const ext = (n) => { const i = n.lastIndexOf('.'); return i >= 0 ? n.slice(i + 1).toLowerCase() : ''; };
  // Skip any file inside a sub-folder whose name contains "Training" or "Archive"
  // (case-insensitive). Only sub-folders are checked — never the top folder, which
  // becomes the collection name (so importing a folder literally named "Archive" works).
  const SKIP_DIRS = ['training', 'archive'];
  const inSkippedDir = (f) => (f.webkitRelativePath || '').split('/').slice(1, -1)
    .some((seg) => SKIP_DIRS.some((w) => seg.toLowerCase().includes(w)));
  const genId = () => {
    try { if (crypto?.randomUUID) return crypto.randomUUID(); } catch { /* insecure ctx */ }
    return 'imp-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  };

  const all = Array.from(files || []);
  const queue = all.filter((f) => ALLOWED.has(ext(f.name)) && !inSkippedDir(f));
  const topFolder = (all[0]?.webkitRelativePath?.split('/')[0]) || 'Imported';
  const videoCount = queue.filter((f) => VIDEO.includes(ext(f.name))).length;
  const imageCount = queue.length - videoCount;
  const skipped = all.length - queue.length;
  // Why files were dropped before upload (disjoint: unsupported = wrong extension;
  // skippedDir = supported but inside a Training/Archive sub-folder).
  const skippedDir = all.filter((f) => ALLOWED.has(ext(f.name)) && inSkippedDir(f));
  const unsupported = all.filter((f) => !ALLOWED.has(ext(f.name)));
  const unsupportedExts = [...new Set(unsupported.map((f) => `.${ext(f.name) || '(none)'}`))].sort();
  if (skippedDir.length || unsupported.length) {
    // Full per-file detail in devtools, complementing the server log for upload-time skips.
    console.warn('[import] skipping before upload', {
      unsupported: unsupported.map((f) => f.webkitRelativePath || f.name),
      inTrainingOrArchive: skippedDir.map((f) => f.webkitRelativePath || f.name),
    });
  }

  let name = $state(topFolder);
  let phase = $state('confirm'); // confirm | running | committing | done | error
  let doneCount = $state(0);     // files processed (uploaded or skipped-on-error)
  let curName = $state('');
  let curPct = $state(0);        // 0..1 byte progress of the current file
  let imported = $state([]);     // { id, thumb } per successful upload
  let errors = $state([]);       // { name, message }
  let result = $state(null);     // { count, collection_id, name }
  let errMsg = $state('');
  let burst = $state(0);
  let cancelling = $state(false);
  let ctrl = null;

  const overallPct = $derived(queue.length ? Math.round((doneCount / queue.length) * 100) : 0);
  // Group upload failures by reason (e.g. "3× File is larger than the 5 GB upload limit").
  const errorSummary = $derived.by(() => {
    const m = new Map();
    for (const e of errors) m.set(e.message, (m.get(e.message) || 0) + 1);
    return [...m.entries()].map(([msg, n]) => `${n}× ${msg}`);
  });

  async function start() {
    if (!queue.length) { errMsg = 'That folder has no supported videos or images.'; phase = 'error'; return; }
    phase = 'running';
    ctrl = new AbortController();
    const importId = genId();
    for (let i = 0; i < queue.length; i++) {
      if (cancelling) break;
      const file = queue[i];
      curName = file.name;
      curPct = 0;
      try {
        const r = await importFile(importId, file, { onProgress: (p) => (curPct = p), signal: ctrl.signal });
        imported = [...imported, { id: r.id, thumb: r.thumb }];
      } catch (e) {
        if (e?.name === 'AbortError') { cancelling = true; break; }
        errors = [...errors, { name: file.name, message: e?.message || 'failed' }];
      }
      doneCount++;
    }
    if (cancelling) { importCancel(importId); onclose(); return; }
    phase = 'committing';
    try {
      result = await importCommit(importId,
        target === 'existing' && existingId
          ? { collectionId: existingId }
          : { name: name.trim() || topFolder });
      phase = 'done';
      burst++;
    } catch (e) {
      // Reclaim the buffered upload so a failed commit (e.g. the target was deleted or
      // got locked between picking it and committing) doesn't strand the files on disk.
      // Safe: a validation failure returns before the server consumes the buffer, and
      // once a commit has succeeded this is a no-op (nothing left to cancel).
      importCancel(importId);
      errMsg = e?.message || 'Could not finish the import.';
      phase = 'error';
    }
  }

  function cancel() {
    if (phase === 'running') { cancelling = true; ctrl?.abort(); return; }
    onclose();
  }
  function onkey(e) {
    // Don't let a stray Escape abort a long upload — only close in the calm phases.
    if (e.key === 'Escape' && phase !== 'running' && phase !== 'committing') onclose();
  }
  const finish = (open) => { oncreated(result, open); onclose(); };
</script>

<svelte:window onkeydown={onkey} />

<div use:portal class="fixed inset-0 z-[70] grid place-items-center bg-[var(--overlay-strong)] p-4 backdrop-blur-sm" role="presentation"
     transition:fade={{ duration: 120 }} onclick={(e) => { if (e.target === e.currentTarget && phase !== 'running' && phase !== 'committing') onclose(); }}>
  {#if phase === 'running' || phase === 'done'}
    <ParticleField active={phase === 'running'} animate={phase === 'running'} layers={3} intensity={0.8} auroraAlpha={0.28} aurora
      burst={burst} class="pointer-events-none absolute inset-0 z-0 h-full w-full" />
  {/if}

  <div class="relative z-10 flex max-h-[90dvh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-line bg-[var(--surface-solid)] shadow-[0_30px_80px_-20px_rgba(0,0,0,0.7)]"
       role="dialog" aria-modal="true" aria-label="Import folder" tabindex="-1"
       use:trapFocus transition:fly={{ y: 18, duration: 180 }}>
    <header class="flex items-center justify-between border-b border-line px-5 py-4">
      <div class="min-w-0">
        <h2 class="text-lg font-extrabold tracking-tight">Import folder</h2>
        <p class="truncate text-sm text-muted">
          {#if phase === 'done'}Imported into “{result?.name}”.
          {:else}{queue.length} file{queue.length === 1 ? '' : 's'} · {videoCount} video{videoCount === 1 ? '' : 's'} · {imageCount} image{imageCount === 1 ? '' : 's'}{/if}
        </p>
      </div>
      {#if phase !== 'running' && phase !== 'committing'}
        <button type="button" class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-line" aria-label="Close" onclick={onclose}>✕</button>
      {/if}
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-5">
      {#if phase === 'confirm'}
        <div class="space-y-5">
          <div class="flex items-center gap-3 rounded-xl border border-line bg-[var(--surface-2)] px-4 py-4">
            <span class="grid h-11 w-11 shrink-0 place-items-center rounded-lg bg-[var(--accent)]/15 text-[var(--accent)]">
              <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
            </span>
            <div class="min-w-0">
              <p class="font-semibold text-ink">{target === 'existing' ? 'Add this folder to a collection' : 'Create a new collection from this folder'}</p>
              <p class="text-sm text-muted">{queue.length} supported file{queue.length === 1 ? '' : 's'} found.</p>
              {#if skipped}
                <p class="mt-1 text-xs text-muted">{skipped} skipped — {#if unsupported.length}{unsupported.length} unsupported ({unsupportedExts.join(', ')}){/if}{#if unsupported.length && skippedDir.length} · {/if}{#if skippedDir.length}{skippedDir.length} in Training/Archive folders{/if}.</p>
              {/if}
            </div>
          </div>

          <!-- Destination: new vs. existing collection. Only offered when there's an
               eligible (non-sealed) collection to add to; otherwise import always
               creates a new one. -->
          {#if pickable.length}
            <div class="grid grid-cols-2 gap-1.5">
              <button type="button" onclick={() => (target = 'new')}
                class="rounded-lg border px-3 py-2 text-sm font-semibold transition {target === 'new' ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}">New collection</button>
              <button type="button" onclick={() => (target = 'existing')}
                class="rounded-lg border px-3 py-2 text-sm font-semibold transition {target === 'existing' ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}">Add to existing</button>
            </div>
          {/if}

          {#if target === 'existing'}
            <div>
              <label for="import-existing" class="mb-2 block text-xs font-bold uppercase tracking-wider text-muted">Collection</label>
              <select id="import-existing" bind:value={existingId}
                class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2.5 text-base font-bold outline-none focus:border-[var(--accent)]">
                <option value="" disabled>Choose a collection…</option>
                {#each pickable as c (c.id)}
                  <option value={c.id}>{c.name} ({(c.item_count ?? c.ids?.length ?? 0).toLocaleString()})</option>
                {/each}
              </select>
              <p class="mt-1.5 text-xs text-muted">The imported files are appended to this collection and archived (kept out of Recent).</p>
            </div>
          {:else}
            <div>
              <label for="import-name" class="mb-2 block text-xs font-bold uppercase tracking-wider text-muted">Collection name</label>
              <input id="import-name" bind:value={name} maxlength="80"
                class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2.5 text-base font-extrabold outline-none focus:border-[var(--accent)]" />
              <p class="mt-1.5 text-xs text-muted">Defaults to the folder name — rename now or later.</p>
            </div>
          {/if}
        </div>
      {:else if phase === 'running' || phase === 'committing'}
        <div class="space-y-5">
          <div class="flex items-center justify-between text-sm font-semibold">
            <span>{phase === 'committing' ? 'Finalizing collection…' : `Importing ${doneCount} / ${queue.length}`}</span>
            <span class="text-muted">{phase === 'committing' ? '' : `${overallPct}%`}</span>
          </div>
          <div class="h-2.5 w-full overflow-hidden rounded-full bg-[var(--surface-2)]">
            <div class="h-full rounded-full bg-[var(--accent)] transition-[width] duration-300" style="width: {phase === 'committing' ? 100 : overallPct}%"></div>
          </div>
          {#if phase === 'running'}
            <div>
              <p class="truncate text-sm font-medium text-ink/90" title={curName}>{curName || 'Starting…'}</p>
              <div class="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-[var(--surface-2)]">
                <div class="h-full rounded-full bg-[var(--accent)]/70 transition-[width] duration-150" style="width: {Math.round(curPct * 100)}%"></div>
              </div>
            </div>
          {:else}
            <p class="text-sm text-muted">Building thumbnails and the search index…</p>
          {/if}
          {#if imported.length}
            <div class="grid grid-cols-6 gap-1.5 sm:grid-cols-8">
              {#each imported.slice(-24) as it (it.id)}
                <span class="aspect-square overflow-hidden rounded-md bg-[var(--media-bg)]" transition:fade={{ duration: 200 }}>
                  <img src={it.thumb} alt="" loading="lazy" class="h-full w-full object-cover object-top" onerror={(e) => (e.currentTarget.style.visibility = 'hidden')} />
                </span>
              {/each}
            </div>
          {/if}
          {#if errors.length}
            <div class="text-xs text-[var(--danger-ink-soft)]">
              <p class="font-semibold">{errors.length} couldn’t be imported:</p>
              <ul class="mt-0.5 list-disc pl-4">{#each errorSummary as line (line)}<li>{line}</li>{/each}</ul>
            </div>
          {/if}
        </div>
      {:else if phase === 'done'}
        <div class="space-y-4 text-center">
          <span class="mx-auto grid h-14 w-14 place-items-center rounded-full bg-[var(--accent)] text-[var(--on-accent)]">
            <svg viewBox="0 0 24 24" class="h-7 w-7" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>
          </span>
          <div>
            <p class="text-lg font-extrabold text-ink">Imported {result?.count} item{result?.count === 1 ? '' : 's'}</p>
            <p class="text-sm text-muted">into the “{result?.name}” collection{errors.length ? ` · ${errors.length} skipped` : ''}.</p>
            {#if errors.length}
              <ul class="mt-2 inline-block list-disc pl-5 text-left text-xs text-[var(--danger-ink-soft)]">{#each errorSummary as line (line)}<li>{line}</li>{/each}</ul>
            {/if}
          </div>
        </div>
      {:else}
        <p class="rounded-lg border border-[var(--danger-border)] bg-[var(--danger-bg)] px-3 py-2 text-sm text-[var(--danger-ink-soft)]">{errMsg}</p>
      {/if}
    </div>

    <footer class="flex items-center justify-end gap-3 border-t border-line px-5 py-4">
      {#if phase === 'confirm'}
        <button type="button" class="rounded-lg border border-line px-4 py-2.5 text-sm font-semibold" onclick={onclose}>Cancel</button>
        <button type="button" class="rounded-lg bg-[var(--accent)] px-5 py-2.5 text-sm font-bold text-[var(--on-accent)] disabled:opacity-45"
          disabled={!queue.length || (target === 'existing' && !existingId)} onclick={start}>Import {queue.length} file{queue.length === 1 ? '' : 's'}</button>
      {:else if phase === 'running'}
        <button type="button" class="rounded-lg border border-line px-4 py-2.5 text-sm font-semibold disabled:opacity-50" disabled={cancelling} onclick={cancel}>{cancelling ? 'Cancelling…' : 'Cancel'}</button>
      {:else if phase === 'committing'}
        <span class="text-sm text-muted">Almost done…</span>
      {:else if phase === 'done'}
        <button type="button" class="rounded-lg border border-line px-4 py-2.5 text-sm font-semibold" onclick={() => finish(false)}>Done</button>
        <button type="button" class="rounded-lg bg-[var(--accent)] px-5 py-2.5 text-sm font-bold text-[var(--on-accent)]" onclick={() => finish(true)}>Open collection</button>
      {:else}
        <button type="button" class="rounded-lg border border-line px-4 py-2.5 text-sm font-semibold" onclick={onclose}>Close</button>
      {/if}
    </footer>
  </div>
</div>
