<script>
  import { onMount, onDestroy } from 'svelte';
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { generateMovie, movieStatus, movieResultUrl, commitMovie } from '$lib/api.js';
  import { loadCollections } from '$lib/state.js';
  import { toast } from '$lib/toast.js';

  // videoIds: ordered ids of the currently-selected videos (selection order).
  let { videoIds = [], onclose = () => {} } = $props();

  const RES = [
    { id: 'land1080', label: '1080p · 16:9', w: 1920, h: 1080 },
    { id: 'land720', label: '720p · 16:9', w: 1280, h: 720 },
    { id: 'vert', label: 'Vertical · 9:16', w: 1080, h: 1920 },
    { id: 'square', label: 'Square · 1:1', w: 1080, h: 1080 }
  ];
  const STAGE_LABEL = {
    queued: 'Queued', analyzing_audio: 'Analyzing audio', analyzing_motion: 'Analyzing motion',
    planning: 'Planning cuts', rendering: 'Rendering', done: 'Done', error: 'Error'
  };

  let song = $state(null);
  let tightness = $state(0.5);
  let resId = $state('land1080');
  let fps = $state(30);
  let targetDuration = $state('');
  let name = $state('movie');
  let dragging = $state(false);

  let starting = $state(false);
  let startError = $state(''); // error from the kickoff POST (shown in-panel)
  let job = $state(null); // last /status payload
  let committing = $state(false);
  let committed = $state(false);
  let timer = null;

  const res = $derived(RES.find((r) => r.id === resId) || RES[0]);
  const tightnessLabel = $derived(tightness < 0.34 ? 'Relaxed' : tightness > 0.66 ? 'Tight' : 'Balanced');
  const tightnessHelp = $derived(
    tightness < 0.34
      ? 'Longer takes — a new clip roughly every 4 beats. Calmer and more cinematic; lets each shot breathe.'
      : tightness > 0.66
      ? 'Rapid cuts — close to one clip per beat. High-energy and frantic; great for drops and choruses.'
      : 'Moderate pace — a new clip every couple of beats. A balanced mix of breathing room and momentum.'
  );
  const TIGHTNESS_TIP = 'Controls how often the montage cuts to a new clip, relative to the song’s beats. Drag left for fewer, longer shots; right for more, shorter shots. Cut density also rises automatically in louder sections.';
  const running = $derived(!!job && job.running);
  const done = $derived(!!job && job.status === 'done' && !!job.result);
  const errored = $derived(!!job && job.status === 'error');
  const canGenerate = $derived(videoIds.length >= 2 && !!song && !starting && !running);

  function poll() {
    movieStatus().then((s) => {
      job = s;
      if (!s.running) stop();
    }).catch(() => {});
  }
  function start() {
    stop();
    timer = setInterval(poll, 1200);
  }
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  onMount(async () => {
    // Reconnect: if a render is already in flight (e.g. user reopened the panel),
    // resume showing its live progress instead of starting fresh.
    try {
      const s = await movieStatus();
      if (s.running || s.status === 'done' || s.status === 'error') job = s;
      if (s.running) start();
    } catch {}
  });
  onDestroy(stop);

  function pickFile(e) {
    const f = e.target.files?.[0];
    if (f) song = f;
  }
  function onDrop(e) {
    e.preventDefault();
    dragging = false;
    const f = e.dataTransfer?.files?.[0];
    if (f && /^audio\/|\.(mp3|wav|flac|m4a|aac|ogg|opus)$/i.test(f.type || f.name)) song = f;
    else if (f) toast('That file is not a supported audio format.', { type: 'error' });
  }

  async function generate() {
    if (!canGenerate) return;
    starting = true;
    startError = '';
    try {
      await generateMovie({
        ids: videoIds,
        song,
        options: {
          name: name.trim() || 'movie',
          tightness,
          width: res.w,
          height: res.h,
          fps,
          target_duration: targetDuration,
          // Fresh seed every run so successive renders pick different moments.
          seed: Math.floor(Math.random() * 1_000_000_000)
        }
      });
      job = { running: true, status: 'queued', progress: 0, detail: 'Queued…' };
      start();
    } catch (e) {
      startError = e.message || 'Could not start generation.';
      toast(startError, { type: 'error' });
    } finally {
      starting = false;
    }
  }

  async function addToCollection() {
    if (committing || committed) return;
    committing = true;
    try {
      await commitMovie();
      committed = true;
      await loadCollections(); // surface the new "Beat Montage" collection
      toast('Added to “Beat Montage” collection', { type: 'success' });
    } catch (e) {
      toast(e.message || 'Could not add to collection.', { type: 'error' });
    } finally {
      committing = false;
    }
  }

  function reset() {
    stop();
    job = null;
    committed = false;
    committing = false;
  }
  function onkey(e) {
    if (e.key === 'Escape' && !running) onclose();
  }

  const pct = $derived(Math.round(((job?.progress) || 0) * 100));
</script>

<svelte:window onkeydown={onkey} />

<div use:portal class="fixed inset-0 z-[70] grid place-items-center bg-[var(--overlay-strong)] p-4 backdrop-blur-sm" role="presentation"
     transition:fade={{ duration: 120 }} onclick={(e) => { if (e.target === e.currentTarget && !running) onclose(); }}>
  <div class="panel flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl" role="dialog" aria-modal="true" aria-label="Generate Movie" tabindex="-1"
       transition:fly={{ y: 18, duration: 180 }}>
    <header class="flex items-center justify-between border-b border-line px-5 py-4">
      <div>
        <h2 class="text-lg font-extrabold tracking-tight">Generate Movie</h2>
        <p class="text-sm text-muted">Beat-synced montage from {videoIds.length} selected video{videoIds.length === 1 ? '' : 's'}.</p>
      </div>
      <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line disabled:opacity-40"
        aria-label="Close" disabled={running} onclick={onclose}>✕</button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-5">
      {#if done}
        <!-- Result -->
        <div class="space-y-4">
          <!-- Music-only generated preview has no speech track. -->
          <!-- svelte-ignore a11y_media_has_caption -->
          <video class="mx-auto block max-h-[55vh] max-w-full rounded-xl bg-[var(--media-bg)]" src={movieResultUrl(false, job.job_id)} controls playsinline autoplay></video>
          <p class="text-sm text-muted">
            {job.result.cuts} cuts · {job.result.width}×{job.result.height} · {job.result.fps} fps · {job.result.duration}s
            · {(job.result.size_bytes / 1048576).toFixed(1)} MB
          </p>
          <div class="flex flex-wrap gap-2">
            <a class="rounded-lg bg-[var(--accent)] px-4 py-2.5 font-bold text-[var(--on-accent)]" href={movieResultUrl(true, job.job_id)} download={job.result.filename}>⇩ Download MP4</a>
            <button type="button" class="rounded-lg border border-line px-4 py-2.5 font-semibold disabled:opacity-60"
              disabled={committing || committed} onclick={addToCollection}>
              {#if committed}✓ In “Beat Montage”{:else if committing}Adding…{:else}+ Add to Collection{/if}
            </button>
            <button type="button" class="rounded-lg border border-line px-4 py-2.5 font-semibold" onclick={reset}>Make another</button>
          </div>
        </div>
      {:else if running}
        <!-- Progress -->
        <div class="space-y-4 py-4">
          <div class="flex items-center justify-between text-sm font-semibold">
            <span>{STAGE_LABEL[job.status] || job.status}</span>
            <span class="text-muted">{pct}%</span>
          </div>
          <div class="h-2.5 w-full overflow-hidden rounded-full bg-[var(--surface-2)]">
            <div class="h-full rounded-full bg-[var(--accent)] transition-[width] duration-500" style="width: {pct}%"></div>
          </div>
          <p class="text-sm text-muted">{job.detail || 'Working…'}</p>
          <p class="text-xs text-muted">Generation runs on the server and continues even if you close this panel.</p>
        </div>
      {:else}
        <!-- Setup -->
        <div class="space-y-5">
          {#if startError}
            <p class="rounded-lg border border-[var(--danger-border)] bg-[var(--danger-bg)] px-3 py-2 text-sm text-[var(--danger-ink-soft)]">{startError}</p>
          {:else if errored}
            <p class="rounded-lg border border-[var(--danger-border)] bg-[var(--danger-bg)] px-3 py-2 text-sm text-[var(--danger-ink-soft)]">{job.error || 'Generation failed.'}</p>
          {/if}

          <!-- Song -->
          <div>
            <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Song</div>
            <label class="flex cursor-pointer items-center gap-3 rounded-xl border border-dashed px-4 py-5 text-sm transition {dragging ? 'border-[var(--accent)] bg-[var(--accent)]/5' : 'border-line'}"
              ondragover={(e) => { e.preventDefault(); dragging = true; }}
              ondragleave={() => (dragging = false)}
              ondrop={onDrop}>
              <span class="text-2xl">🎵</span>
              <span class="min-w-0 flex-1">
                {#if song}
                  <span class="block truncate font-semibold text-ink">{song.name}</span>
                  <span class="text-xs text-muted">{(song.size / 1048576).toFixed(1)} MB · click or drop to replace</span>
                {:else}
                  <span class="block font-semibold text-ink">Drop an audio file or click to choose</span>
                  <span class="text-xs text-muted">mp3 · wav · flac · m4a · aac · ogg · opus</span>
                {/if}
              </span>
              <input type="file" accept="audio/*,.mp3,.wav,.flac,.m4a,.aac,.ogg,.opus" class="hidden" onchange={pickFile} />
            </label>
          </div>

          <!-- Tightness -->
          <div>
            <div class="mb-2 flex items-center justify-between text-xs font-bold uppercase tracking-wider text-muted">
              <span class="inline-flex items-center gap-1">
                Cut tightness
                <span class="grid h-3.5 w-3.5 cursor-help place-items-center rounded-full border border-current text-[9px] font-bold opacity-70" title={TIGHTNESS_TIP}>?</span>
              </span>
              <span class="normal-case text-[var(--accent)]" title={tightnessHelp}>{tightnessLabel}</span>
            </div>
            <input type="range" min="0" max="1" step="0.05" bind:value={tightness} class="w-full accent-[var(--accent)]" title={TIGHTNESS_TIP} />
            <div class="mb-1 flex justify-between text-[11px] text-muted"><span>Relaxed</span><span>Tight</span></div>
            <p class="text-xs leading-relaxed text-muted">{tightnessHelp}</p>
          </div>

          <!-- Resolution + fps -->
          <div class="grid gap-4 sm:grid-cols-2">
            <div>
              <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Aspect / resolution</div>
              <div class="grid grid-cols-2 gap-1.5">
                {#each RES as r (r.id)}
                  <button type="button" class="rounded-lg border px-2.5 py-2 text-xs font-semibold transition {resId === r.id ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                    onclick={() => (resId = r.id)}>{r.label}</button>
                {/each}
              </div>
            </div>
            <div class="space-y-4">
              <div>
                <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Frame rate</div>
                <div class="grid grid-cols-3 gap-1.5">
                  {#each [24, 30, 60] as f (f)}
                    <button type="button" class="rounded-lg border px-2.5 py-2 text-xs font-semibold transition {fps === f ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                      onclick={() => (fps = f)}>{f}</button>
                  {/each}
                </div>
              </div>
              <div>
                <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Length (s) <span class="normal-case opacity-70">— blank = song length</span></div>
                <input type="number" min="1" placeholder="auto" bind:value={targetDuration}
                  class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none" />
              </div>
            </div>
          </div>

          <!-- Name -->
          <div>
            <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">File name</div>
            <input bind:value={name} maxlength="80" class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none" />
          </div>
        </div>
      {/if}
    </div>

    {#if !done && !running}
      <footer class="flex items-center justify-between gap-3 border-t border-line px-5 py-4">
        <p class="text-xs text-muted">
          {#if videoIds.length < 2}Select at least 2 videos.{:else if !song}Choose a song to continue.{:else}Ready to generate.{/if}
        </p>
        <button type="button" class="rounded-lg bg-[var(--accent)] px-5 py-2.5 font-bold text-[var(--on-accent)] disabled:opacity-45"
          disabled={!canGenerate} onclick={generate}>
          {starting ? 'Starting…' : 'Generate Clip'}
        </button>
      </footer>
    {/if}
  </div>
</div>
