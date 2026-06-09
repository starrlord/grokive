<script>
  import { onMount } from 'svelte';
  import {
    generateImage, startImagineVideo, saveImagineGen, clearImagineSession,
    discardImagineGen, getImagineSession, ackImagineVideo, uploadImagineImage
  } from '$lib/api.js';
  import {
    settings, activeImagineSession, imagineJobs, ensureImaginePolling,
    refreshImagineSessions, requestGalleryReload
  } from '$lib/state.js';
  import { toast } from '$lib/toast.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  // One workspace (staging session). Generations accumulate in a running history and
  // stay here until Saved. The active session id comes from the store (the switcher /
  // "Use as source" set it); video progress comes from the global per-session job map.
  const IMAGE_RATIOS = ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3', '2:1', '1:2', 'auto'];
  const VIDEO_RATIOS = ['16:9', '9:16', '1:1', '4:3', '3:4', '3:2', '2:3'];
  const IDLE_JOB = { running: false, status: 'idle', detail: '', progress: 0, error: '', result: null, acknowledged: true };

  let mode = $state('image');
  let prompt = $state('');
  let n = $state(1);
  let iAspect = $state('1:1');
  let iResolution = $state('1k');
  let vAspect = $state('16:9');
  let vResolution = $state('480p');
  let vDuration = $state(6);

  let session = $state(null);   // { session_id, source, generations: [] }
  let selected = $state(null);
  let submitting = $state(false);
  let confirmingClear = $state(false);
  let confirmingDelete = $state(null);
  let uploading = $state(false);
  let dragOver = $state(false);
  let fileInput;

  const activeSessionId = $derived($activeImagineSession);
  const videoJob = $derived($imagineJobs[activeSessionId] || IDLE_JOB);
  const keyReady = $derived(!!$settings.xai_api_key_configured);
  const busy = $derived(submitting || videoJob.running);
  const generations = $derived(session?.generations || []);
  // An image is *available* to use as a source (selected generation, or the gallery
  // image this workspace is rooted on) — but using it is opt-in via `useSource`, so you
  // can always generate fresh from text instead. Default on only for image workspaces.
  let useSource = $state(false);
  const sourceCandidate = $derived.by(() => {
    if (selected && selected.media_type === 'image')
      return { id: selected.gen_id, thumb: selected.thumb_url, label: 'this image' };
    if (session?.source?.id)
      return { id: session.source.id, thumb: session.source.thumb, label: 'the source image' };
    return null;
  });
  const activeSource = $derived(useSource && sourceCandidate ? sourceCandidate : null);

  let seeded = false;
  $effect(() => {
    const s = $settings;
    if (seeded || !s) return;
    seeded = true;
    iResolution = s.xai_image_resolution || '1k';
    iAspect = s.xai_image_aspect_ratio || '1:1';
    vResolution = s.xai_video_resolution || '480p';
    vAspect = s.xai_video_aspect_ratio || '16:9';
    vDuration = s.xai_video_duration || 6;
  });

  // Default the Video aspect to "Match source" (auto = omit, keeps the source's ratio)
  // whenever an image source is active; restore the configured default otherwise.
  let lastHadSource;
  $effect(() => {
    const has = !!activeSource;
    if (has === lastHadSource) return;
    lastHadSource = has;
    vAspect = has ? 'auto' : ($settings.xai_video_aspect_ratio || '16:9');
  });

  // Load whichever session is active.
  let loadToken = 0;
  let lastLoadedFor;
  $effect(() => {
    const sid = $activeImagineSession;
    if (sid === lastLoadedFor) return;
    lastLoadedFor = sid;
    loadSession(sid);
  });

  async function loadSession(sid) {
    const t = ++loadToken;
    try {
      const d = await getImagineSession(sid);
      if (t !== loadToken) return;
      session = d.session;
      // Image workspaces default to editing their source; text workspaces start fresh.
      useSource = !!session?.source?.id;
      const gens = session?.generations || [];
      selectGen(gens.length ? gens[gens.length - 1] : null);
      if (!gens.length) prompt = session?.source?.prompt || '';
    } catch {
      if (t !== loadToken) return;
      session = { session_id: sid, source: null, generations: [] };
      selected = null;
    }
  }

  // When the active session's video finishes, fold the result into the history once.
  $effect(() => {
    const sid = activeSessionId;
    const job = $imagineJobs[sid];
    if (!job || job.status !== 'done' || !job.result) return;
    if (session?.session_id !== sid) return;
    const g = job.result;
    if (!(session.generations || []).some((x) => x.gen_id === g.gen_id)) {
      session.generations = [...(session.generations || []), g];
      // A render finishes in the background while the user may be mid-edit in this
      // workspace — don't hijack their selection/prompt. Only jump to it if nothing
      // is selected; otherwise it just appears in the filmstrip to click.
      if (!selected) selectGen(session.generations[session.generations.length - 1]);
      refreshImagineSessions();
    }
    if (!job.acknowledged) ackJob(sid);
  });

  function ackJob(sid) {
    ackImagineVideo(sid);
    imagineJobs.update((m) => (m[sid] ? { ...m, [sid]: { ...m[sid], acknowledged: true } } : m));
  }

  function selectGen(gen) {
    // Note: don't ack a stale video error here — selecting/loading shouldn't silently
    // dismiss it (the error overlay is mode-gated, so it hides on an image selection
    // anyway). Only Dismiss or starting a new generation acks it.
    selected = gen;
    if (!gen) return;
    prompt = gen.prompt || '';
    const p = gen.params || {};
    mode = gen.media_type === 'video' ? 'video' : 'image';
    if (gen.media_type === 'video') {
      if (p.resolution) vResolution = p.resolution;
      if (p.duration) vDuration = Math.max(1, Math.min(15, Number(p.duration) || vDuration));
    } else if (p.resolution) {
      iResolution = p.resolution;
    }
  }

  onMount(() => ensureImaginePolling());

  async function genImage() {
    if (busy || !keyReady || !session) return;
    if (!prompt.trim() && !activeSource) { toast('Enter a prompt.', { type: 'error' }); return; }
    if (videoJob.status === 'error' && !videoJob.acknowledged) ackJob(activeSessionId);
    submitting = true;
    try {
      const d = await generateImage({
        session_id: session.session_id, source: activeSource?.id || '',
        prompt, n, aspect_ratio: iAspect, resolution: iResolution
      });
      const made = d.generations || [];
      session.generations = [...(session.generations || []), ...made];
      if (made.length) selectGen(session.generations[session.generations.length - 1]);
      refreshImagineSessions();
      const skip = d.skipped || 0;
      toast(`Generated ${made.length} image${made.length === 1 ? '' : 's'}${skip ? ` · ${skip} filtered` : ''}`, { type: 'success' });
    } catch (e) { toast(e?.message || 'Generation failed.', { type: 'error' }); }
    finally { submitting = false; }
  }

  async function genVideo() {
    if (busy || !keyReady || !session) return;
    if (!prompt.trim() && !activeSource) { toast('Enter a prompt.', { type: 'error' }); return; }
    submitting = true;
    const sid = session.session_id;
    try {
      await startImagineVideo({
        session_id: sid, source: activeSource?.id || '',
        prompt, duration: vDuration, aspect_ratio: vAspect, resolution: vResolution
      });
      // Optimistically mark it running so the overlay shows without a poll gap.
      imagineJobs.update((m) => ({ ...m, [sid]: { session_id: sid, running: true, status: 'queued', detail: 'Queued…', progress: 0.02, error: '', result: null, acknowledged: false } }));
      ensureImaginePolling();
      refreshImagineSessions();
    } catch (e) { toast(e?.message || 'Could not start the video.', { type: 'error' }); }
    finally { submitting = false; }
  }

  const generate = () => (mode === 'video' ? genVideo() : genImage());
  function makeVideo() { mode = 'video'; genVideo(); }
  const dismissError = () => ackJob(activeSessionId);

  // --- Upload an existing image as a generation (to edit / animate) ---
  async function uploadFile(file) {
    if (!file || uploading || !session) return;
    const okType = (file.type || '').startsWith('image/') || /\.(jpe?g|png|webp|gif|bmp|tiff?|heic|heif|avif)$/i.test(file.name || '');
    if (!okType) { toast('Please choose an image file.', { type: 'error' }); return; }
    uploading = true;
    try {
      const d = await uploadImagineImage(session.session_id, file);
      const g = d.generation;
      if (g) {
        session.generations = [...(session.generations || []), g];
        selectGen(session.generations[session.generations.length - 1]);
        useSource = true;   // ready to edit / animate the uploaded image
        mode = 'image';
        refreshImagineSessions();
        toast('Uploaded — edit it, or switch to Video to animate.', { type: 'success' });
      }
    } catch (e) { toast(e?.message || 'Upload failed.', { type: 'error' }); }
    finally { uploading = false; }
  }
  function onFilePick(e) {
    const file = e.currentTarget.files?.[0];
    e.currentTarget.value = ''; // allow re-picking the same file
    if (file) uploadFile(file);
  }
  function onDrop(e) {
    e.preventDefault();
    dragOver = false;
    const file = e.dataTransfer?.files?.[0];
    if (file) uploadFile(file);
  }
  function onDragOver(e) {
    if (e.dataTransfer?.types?.includes('Files')) { e.preventDefault(); dragOver = true; }
  }
  function onDragLeave(e) {
    if (e.relatedTarget == null || !e.currentTarget.contains(e.relatedTarget)) dragOver = false;
  }

  async function saveSelected() {
    if (!selected || selected.saved) return;
    try {
      const d = await saveImagineGen(selected.gen_id);
      selected.saved = true;
      selected.saved_media_id = d.item?.id || 'saved';
      requestGalleryReload();
      toast(d.already ? 'Already in gallery' : 'Saved to gallery', { type: 'success' });
    } catch (e) { toast(e?.message || 'Save failed.', { type: 'error' }); }
  }

  async function doDelete() {
    const g = confirmingDelete;
    confirmingDelete = null;
    if (!g || !session) return;
    try {
      await discardImagineGen(g.gen_id);
      const remaining = (session.generations || []).filter((x) => x.gen_id !== g.gen_id);
      session.generations = remaining;
      if (selected?.gen_id === g.gen_id) selectGen(remaining.length ? remaining[remaining.length - 1] : null);
      refreshImagineSessions();
      toast('Deleted', { type: 'success' });
    } catch (e) { toast(e?.message || 'Delete failed.', { type: 'error' }); }
  }

  async function doClear() {
    confirmingClear = false;
    if (!session) return;
    try {
      const sid = session.session_id;
      await clearImagineSession(sid);
      // Drop any finished job locally too, so the done-folding effect can't re-add the
      // just-cleared video as a ghost item (its staged file is gone now).
      imagineJobs.update((m) => { const next = { ...m }; delete next[sid]; return next; });
      session.generations = [];
      selected = null;
      refreshImagineSessions();
      toast('Workspace cleared', { type: 'success' });
    } catch (e) { toast(e?.message || 'Clear failed.', { type: 'error' }); }
  }
</script>

<div class="w-full">
  <div class="mb-3 flex flex-wrap items-center gap-2">
    <div class="inline-flex rounded-lg border border-line p-0.5">
      <button type="button" class="rounded-md px-4 py-1.5 text-sm font-semibold transition {mode === 'image' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}"
        onclick={() => (mode = 'image')}>Image</button>
      <button type="button" class="rounded-md px-4 py-1.5 text-sm font-semibold transition {mode === 'video' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}"
        onclick={() => (mode = 'video')}>Video</button>
    </div>
    <button type="button" onclick={() => fileInput?.click()} disabled={uploading}
      title="Upload an image to edit or animate"
      class="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)] disabled:opacity-50">
      <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 13v8"/><path d="m8 17 4-4 4 4"/><path d="M20.88 18.09A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.29"/></svg>
      {uploading ? 'Uploading…' : 'Upload'}
    </button>
    <input type="file" accept="image/*" class="hidden" bind:this={fileInput} onchange={onFilePick} />
    <p class="text-xs text-muted">Generations stay here until you Save them to the gallery.</p>
    {#if generations.length}
      <button type="button" class="ml-auto rounded-lg border border-line px-3 py-1.5 text-xs font-semibold transition hover:border-[var(--danger)] hover:text-[var(--danger-ink)]"
        onclick={() => (confirmingClear = true)}>Clear workspace</button>
    {/if}
  </div>

  {#if !keyReady}
    <div class="mb-3 rounded-card border border-dashed border-line bg-[var(--surface-2)]/40 p-3 text-sm text-muted">
      Add your <strong class="text-ink">xAI API key</strong> in <strong class="text-ink">Config → Grok Imagine API</strong> to generate.
    </div>
  {/if}

  <div class="grid gap-4 sm:grid-cols-[104px_1fr]">
    <!-- History filmstrip -->
    <div class="flex gap-2 overflow-x-auto pb-1 sm:flex-col sm:overflow-x-visible sm:overflow-y-auto sm:max-h-[72dvh] sm:pb-0 sm:pr-1">
      {#if session?.source}
        <button type="button" title="Source image" onclick={() => selectGen(null)}
          class="relative aspect-square w-20 shrink-0 overflow-hidden rounded-lg border bg-[var(--media-bg)] sm:w-auto {!selected ? 'border-[var(--accent)]' : 'border-line'}">
          {#if session.source.thumb}<img src={session.source.thumb} alt="" class="h-full w-full object-cover" />{/if}
          <span class="absolute inset-x-0 bottom-0 bg-black/55 px-1 py-0.5 text-[0.5rem] font-bold uppercase tracking-wide text-white">Source</span>
        </button>
      {/if}
      {#each [...generations].reverse() as g (g.gen_id)}
        <div class="group relative aspect-square w-20 shrink-0 sm:w-auto">
          <button type="button" onclick={() => selectGen(g)}
            class="absolute inset-0 overflow-hidden rounded-lg border bg-[var(--media-bg)] {selected?.gen_id === g.gen_id ? 'border-[var(--accent)]' : 'border-line'}">
            {#if g.thumb_url}<img src={g.thumb_url} alt="" loading="lazy" class="h-full w-full object-cover" />{/if}
            {#if g.media_type === 'video'}
              <span class="absolute left-1 top-1 grid h-4 w-4 place-items-center rounded bg-black/55 text-white">
                <svg viewBox="0 0 24 24" class="h-2.5 w-2.5" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>
              </span>
            {/if}
            {#if g.saved}
              <span class="absolute right-1 top-1 grid h-4 w-4 place-items-center rounded-full bg-[var(--accent-2)] text-[0.6rem] font-bold text-white" title="Saved to gallery">✓</span>
            {/if}
          </button>
          <button type="button" title="Delete this generation" aria-label="Delete this generation"
            class="absolute bottom-1 right-1 z-10 grid h-5 w-5 place-items-center rounded bg-black/60 text-white opacity-0 transition after:absolute after:-inset-2 after:content-[''] hover:bg-[var(--danger)] group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:h-8 pointer-coarse:w-8 pointer-coarse:opacity-100"
            onclick={(e) => { e.stopPropagation(); confirmingDelete = g; }}>
            <svg viewBox="0 0 24 24" class="h-3 w-3" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
          </button>
        </div>
      {/each}
      {#if !generations.length && !session?.source}
        <p class="text-xs text-muted">No generations yet.</p>
      {/if}
    </div>

    <!-- Main: preview (capped height) + controls -->
    <div class="min-w-0 space-y-3">
      <div class="relative mx-auto flex w-full max-w-[760px] items-center justify-center overflow-hidden rounded-card border bg-[var(--media-bg)] transition {dragOver ? 'border-[var(--accent)]' : 'border-line'}"
           style="height: clamp(240px, 52dvh, 560px)"
           role="region" aria-label="Preview — drop an image here to upload it"
           ondragover={onDragOver} ondragleave={onDragLeave} ondrop={onDrop}>
        {#if dragOver}
          <div class="pointer-events-none absolute inset-0 z-30 grid place-items-center bg-[var(--overlay)] backdrop-blur-sm">
            <p class="rounded-full border-2 border-dashed border-[var(--accent)] px-5 py-2 text-sm font-bold text-ink">Drop image to edit or animate</p>
          </div>
        {/if}
        {#if busy}
          <div class="imagine-shimmer absolute inset-0"></div>
          <div class="absolute inset-0 grid place-items-center p-4">
            {#if mode === 'video' && videoJob.running}
              <div class="w-full max-w-xs rounded-card border border-line bg-[var(--surface-solid)]/85 p-3 text-center backdrop-blur-sm">
                <p class="mb-2 text-sm font-semibold text-ink">{videoJob.detail || 'Generating…'}</p>
                <div class="h-1.5 w-full overflow-hidden rounded-full bg-[var(--surface-2)]">
                  <div class="h-full rounded-full bg-[var(--accent)] transition-all" style={`width:${Math.max(4, Math.round((videoJob.progress || 0) * 100))}%`}></div>
                </div>
                <p class="mt-1 text-xs text-muted">{Math.round((videoJob.progress || 0) * 100)}%</p>
              </div>
            {:else}
              <p class="rounded-full bg-[var(--surface-solid)]/85 px-4 py-1.5 text-sm font-semibold text-ink backdrop-blur-sm">Generating…</p>
            {/if}
          </div>
        {:else if videoJob.status === 'error' && mode === 'video' && !videoJob.acknowledged}
          <div class="grid place-items-center p-6">
            <div class="max-w-sm rounded-card border border-line bg-[var(--surface-solid)]/85 p-4 text-center backdrop-blur-sm">
              <p class="mb-3 text-sm text-[var(--danger-ink)]">{videoJob.error || 'Generation failed.'}</p>
              <button type="button" class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]" onclick={dismissError}>Dismiss</button>
            </div>
          </div>
        {:else if selected}
          {#if selected.media_type === 'video'}
            <video src={selected.staged_url} controls playsinline class="h-full w-full object-contain"></video>
          {:else}
            <img src={selected.staged_url} alt="" class="h-full w-full object-contain" />
          {/if}
          <div class="absolute right-2 top-2 flex items-center gap-2">
            {#if selected.saved}
              <span class="rounded-lg bg-[var(--accent-2)] px-3 py-1.5 text-xs font-bold text-white">✓ In gallery</span>
            {:else}
              <button type="button" class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-bold text-[var(--on-accent)] shadow" onclick={saveSelected}>Save to Gallery</button>
            {/if}
            <button type="button" title="Delete this generation" aria-label="Delete this generation"
              class="grid h-8 w-8 place-items-center rounded-lg border border-line bg-[var(--surface-solid)]/85 backdrop-blur-sm transition hover:border-[var(--danger)] hover:text-[var(--danger-ink)]"
              onclick={() => (confirmingDelete = selected)}>
              <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
            </button>
          </div>
        {:else if session?.source?.thumb}
          <img src={session.source.thumb} alt="" class="h-full w-full object-contain" />
        {:else}
          <p class="px-6 text-center text-sm text-muted">Generate an image or video — or drop / <button type="button" class="font-semibold text-[var(--accent)] hover:underline" onclick={() => fileInput?.click()}>upload</button> an image to edit or animate. Results appear here.</p>
        {/if}
      </div>

      {#if sourceCandidate}
        <div class="flex flex-wrap items-center gap-2">
          <div class="inline-flex rounded-lg border border-line p-0.5 text-sm">
            <button type="button" class="rounded-md px-3 py-1 font-semibold transition {!useSource ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}"
              onclick={() => (useSource = false)}>From text</button>
            <button type="button" class="rounded-md px-3 py-1 font-semibold transition {useSource ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}"
              onclick={() => (useSource = true)}>Use this image</button>
          </div>
          {#if useSource && sourceCandidate.thumb}<img src={sourceCandidate.thumb} alt="" class="h-7 w-7 rounded object-cover" />{/if}
          <span class="text-xs text-muted">{useSource ? `${mode === 'video' ? 'Animating' : 'Editing'} ${sourceCandidate.label}` : 'Generating from your prompt only'}</span>
        </div>
      {/if}

      <textarea bind:value={prompt} rows="3"
        placeholder={activeSource ? (mode === 'video' ? 'How should it move? (optional)' : 'How should it change? (optional)') : 'Describe what to generate…'}
        class="w-full resize-y rounded-card border border-line bg-[var(--surface-2)] p-3 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]"></textarea>

      {#if mode === 'image'}
        <div class="grid gap-3 sm:grid-cols-3">
          <label class="block text-xs font-semibold text-muted">Count
            <div class="mt-1 inline-grid w-full grid-cols-4 gap-0.5 rounded-lg border border-line bg-[var(--surface-2)] p-0.5">
              {#each [1, 2, 3, 4] as c (c)}
                <button type="button" class="rounded-md py-1.5 text-sm font-semibold transition {n === c ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted'}"
                  onclick={() => (n = c)}>{c}</button>
              {/each}
            </div>
          </label>
          <label class="block text-xs font-semibold text-muted">Aspect ratio
            {#if activeSource}
              <div class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-muted" title="An edit keeps the source image's aspect ratio">Match source</div>
            {:else}
              <select bind:value={iAspect} class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-ink outline-none">
                {#each IMAGE_RATIOS as r (r)}<option value={r}>{r}</option>{/each}
              </select>
            {/if}
          </label>
          <label class="block text-xs font-semibold text-muted">Resolution
            <select bind:value={iResolution} class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-ink outline-none">
              <option value="1k">1k</option>
              <option value="2k">2k</option>
            </select>
          </label>
        </div>
      {:else}
        <div class="grid gap-3 sm:grid-cols-3">
          <label class="block text-xs font-semibold text-muted">Duration · {vDuration}s
            <input type="range" min="1" max="15" step="1" bind:value={vDuration} class="mt-2 w-full accent-[var(--accent)]" />
          </label>
          <label class="block text-xs font-semibold text-muted">Aspect ratio
            <select bind:value={vAspect} class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-ink outline-none">
              <option value="auto">Match source</option>
              {#each VIDEO_RATIOS as r (r)}<option value={r}>{r}</option>{/each}
            </select>
          </label>
          <label class="block text-xs font-semibold text-muted">Resolution
            <select bind:value={vResolution} class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-ink outline-none">
              <option value="480p">480p</option>
              <option value="720p">720p</option>
            </select>
          </label>
        </div>
      {/if}

      <div class="flex flex-wrap items-center gap-2">
        <button type="button" onclick={generate} disabled={busy || !keyReady}
          class="rounded-lg bg-[var(--accent)] px-5 py-2.5 text-sm font-bold text-[var(--on-accent)] transition disabled:opacity-40">
          {#if submitting}Starting…{:else if videoJob.running}Generating…{:else}Generate {mode === 'video' ? 'video' : (n > 1 ? `${n} images` : 'image')}{/if}
        </button>
        {#if mode === 'image' && activeSource}
          <button type="button" onclick={makeVideo} disabled={busy || !keyReady}
            class="rounded-lg border border-line px-4 py-2.5 text-sm font-semibold transition hover:border-[var(--accent)] disabled:opacity-40">Make video</button>
        {/if}
        {#if mode === 'video' && !videoJob.running}
          <span class="text-xs text-muted">Video renders in the background — switch workspaces while it works.</span>
        {/if}
      </div>
    </div>
  </div>
</div>

{#if confirmingDelete}
  <ConfirmDialog title="Delete this generation?"
    message="Removes this staged image/video from the workspace. If you already saved it to the gallery, that copy stays."
    confirmLabel="Delete"
    onconfirm={doDelete}
    oncancel={() => (confirmingDelete = null)} />
{/if}

{#if confirmingClear}
  <ConfirmDialog title="Clear this workspace?"
    message="Deletes every staged generation and its history for this workspace. Anything you already saved to the gallery stays. This can't be undone."
    confirmLabel="Clear"
    onconfirm={doClear}
    oncancel={() => (confirmingClear = false)} />
{/if}

<style>
  /* Sweeping accent shimmer over a muted base while a generation is in flight. */
  .imagine-shimmer {
    position: absolute;
    inset: 0;
    overflow: hidden;
    background: var(--surface-2);
  }
  .imagine-shimmer::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(110deg,
      transparent 25%,
      color-mix(in srgb, var(--accent) 24%, transparent) 45%,
      color-mix(in srgb, var(--accent-2) 24%, transparent) 55%,
      transparent 75%);
    transform: translateX(-100%);
    animation: imagine-shimmer 1.5s ease-in-out infinite;
  }
  @keyframes imagine-shimmer {
    to { transform: translateX(100%); }
  }
</style>
