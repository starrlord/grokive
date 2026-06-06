<script>
  import { onMount, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import ParticleField from './ParticleField.svelte';
  import { generateMovie, movieResultUrl, commitMovie } from '$lib/api.js';
  import { loadCollections, setStashed, movieJob, movieChip, ensureMoviePolling, refreshMovieStatus, markMovieStarted, acknowledgeMovie } from '$lib/state.js';
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

  const PRESETS = [
    { id: 'classic', label: 'Classic', help: 'Punchy hard cuts on the beat — the original style.' },
    { id: 'cinematic', label: 'Cinematic', help: 'Smarter analysis, full-frame framing (no black bars), beat-timed transitions at section changes/drops, a gentle push-in that lets shots breathe, and a zoom punch on the drops.' },
    { id: 'moody', label: 'Moody', help: 'Long held shots with a slow push-in, punctuated by quick beat bursts on the loud parts. Calmer footage.' }
  ];

  let song = $state(null);
  let preset = $state('classic');
  let tightness = $state(0.5);
  // "Let clips speak" — preset-independent. When on, hold on clips that have
  // dialogue (from their subtitles) in the song's quiet spots and dip the music.
  let letClipsSpeak = $state(false);
  let speakMoments = $state('auto'); // 'auto' | '1'..'4'
  let resId = $state('land1080');
  let fps = $state(30);
  let targetDuration = $state('');
  let name = $state('movie');
  let dragging = $state(false);

  let starting = $state(false);
  let startError = $state(''); // error from the kickoff POST (shown in-panel)
  let committing = $state(false);
  let committed = $state(false);
  // The render's status lives in the global movieJob store (shared with the Montage
  // button + the floating status chip). `owned` is whether THIS panel is surfacing
  // that job — true while driving a live render or showing a not-yet-acknowledged
  // result, false on a fresh open so an acknowledged/absent job shows the setup form.
  let owned = $state(false);
  const job = $derived(owned ? $movieJob : null);
  // The clips this panel renders from. Seeded from the live selection (videoIds) on
  // a fresh open, but captured once a render starts (and restored from the job's
  // provenance on reconnect) so the result's source count stays correct even after
  // the live selection is cleared or the panel was reopened from the chip.
  let sessionIds = $state([]);
  const activeIds = $derived(sessionIds.length ? sessionIds : videoIds);

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
  const canGenerate = $derived(activeIds.length >= 2 && !!song && !starting && !running);
  // Prefer the JOB's source count + preset (from server provenance) whenever a job
  // is owned — the live selection is only meaningful on the fresh setup form and is
  // empty when the panel is reopened from the status chip.
  const sourceCount = $derived(job?.sources ?? activeIds.length);
  const styleLabel = $derived(PRESETS.find((p) => p.id === (job?.preset ?? job?.result?.preset))?.label || '');

  onMount(async () => {
    // Reconnect to whatever the status chip considers pending — a live render OR a
    // finished/failed result not yet acknowledged — so reopening always lands you
    // back on it. An acknowledged or absent job leaves the fresh setup form. Live
    // progress is fed by the global poller.
    await refreshMovieStatus();
    if (get(movieChip)) {
      owned = true;
      // Remember the job's source clips so the result's source count stays correct
      // even though the live selection that started it is long gone.
      const ids = get(movieJob).source_ids;
      if (ids?.length) sessionIds = ids;
      if (get(movieJob).running) ensureMoviePolling();
    }
  });

  // Closing never discards the job — the floating chip keeps it reachable until the
  // result is acknowledged (Add to Collection / dismiss).
  function close() {
    onclose();
  }

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
    // Lock in the clips we're rendering so the result's source count stays correct.
    const ids = [...activeIds];
    sessionIds = ids;
    try {
      const data = await generateMovie({
        ids,
        song,
        options: {
          name: name.trim() || 'movie',
          preset,
          tightness,
          width: res.w,
          height: res.h,
          fps,
          target_duration: targetDuration,
          let_clips_speak: letClipsSpeak,
          speak_moments: speakMoments,
          // Fresh seed every run so successive renders pick different moments.
          seed: Math.floor(Math.random() * 1_000_000_000)
        }
      });
      // Surface this render in the panel and light up the button + chip instantly.
      owned = true;
      markMovieStarted(data.job_id);
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
      const res = await commitMovie();
      committed = true;
      // Auto-archive the finished montage so it doesn't clutter Recent — it stays
      // in the "Beat Montage" collection (and Archive / All Media).
      if (res?.id) setStashed([String(res.id)], true);
      acknowledgeMovie(job?.job_id); // dealt with — clear the floating chip
      await loadCollections(); // surface the new "Beat Montage" collection
      toast('Added to “Beat Montage” collection', { type: 'success' });
      close(); // the montage is filed away — dismiss the preview
    } catch (e) {
      toast(e.message || 'Could not add to collection.', { type: 'error' });
    } finally {
      committing = false;
    }
  }

  function onkey(e) {
    // Closing mid-render is fine — generation continues server-side and reopening
    // the panel reconnects to the live job (see onMount), so don't trap the user.
    if (e.key === 'Escape') close();
  }

  const pct = $derived(Math.round(((job?.progress) || 0) * 100));

  // Particle field: the progress bar's track (sparks emit at its leading edge)
  // and a burst counter bumped once when a render lands on `done`.
  let barTrack = $state(null);
  let burstCount = $state(0);
  // Fire one celebratory burst when a render lands on `done`. Increment inside
  // untrack so the effect depends only on `done`, not on burstCount itself —
  // otherwise burstCount++ would re-trigger the effect and loop infinitely.
  $effect(() => { if (done) untrack(() => burstCount++); });
</script>

<svelte:window onkeydown={onkey} />

<div use:portal class="fixed inset-0 z-[70] grid place-items-center bg-[var(--overlay-strong)] p-4 backdrop-blur-sm" role="presentation"
     transition:fade={{ duration: 120 }} onclick={(e) => { if (e.target === e.currentTarget) close(); }}>
  {#if running || done}
    <!-- Full-screen colourful parallax bokeh + aurora behind the panel; fires a
         multi-colour burst from centre when the render finishes. -->
    <ParticleField active={running} animate={running} layers={3} intensity={0.85} auroraAlpha={0.3} aurora
      burst={burstCount} class="pointer-events-none absolute inset-0 z-0 h-full w-full" />
  {/if}
  <div class="panel relative z-10 flex max-h-[90dvh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl" role="dialog" aria-modal="true" aria-label="Create Montage" tabindex="-1"
       use:trapFocus transition:fly={{ y: 18, duration: 180 }}>
    <header class="flex items-center justify-between border-b border-line px-5 py-4">
      <div>
        <h2 class="text-lg font-extrabold tracking-tight">Create Montage</h2>
        <p class="text-sm text-muted">Beat-synced montage from {sourceCount} selected video{sourceCount === 1 ? '' : 's'}{styleLabel ? ` · ${styleLabel} style` : ''}.</p>
      </div>
      <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line"
        aria-label="Close" onclick={close}>✕</button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-5">
      {#if done}
        <!-- Result -->
        <div class="space-y-4">
          <!-- Music-only generated preview has no speech track. -->
          <!-- svelte-ignore a11y_media_has_caption -->
          <video class="mx-auto block max-h-[55dvh] max-w-full rounded-xl bg-[var(--media-bg)]" src={movieResultUrl(false, job.job_id)} controls playsinline autoplay></video>
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
          </div>
        </div>
      {:else if running}
        <!-- Progress -->
        <div class="relative overflow-hidden px-5 py-4">
          <!-- Subtle small bokeh + a faint cool wash inside the panel (kept low so
               text stays crisp); sparks stream off the progress bar's leading edge. -->
          <ParticleField active={running} layers={2} intensity={0.5} scale={0.5}
            aurora auroraColors={['#22d3ee', '#60a5fa', '#a78bfa']} auroraAlpha={0.1}
            emitEl={barTrack} emitAt={pct / 100}
            class="pointer-events-none absolute inset-0 z-0 h-full w-full" />
          <div class="relative z-10 space-y-4">
            <div class="flex items-center justify-between text-sm font-semibold">
              <span>{STAGE_LABEL[job.status] || job.status}</span>
              <span class="text-muted">{pct}%</span>
            </div>
            <div bind:this={barTrack} class="h-2.5 w-full overflow-hidden rounded-full bg-[var(--surface-2)]">
              <div class="h-full rounded-full bg-[var(--accent)] transition-[width] duration-500" style="width: {pct}%"></div>
            </div>
            <p class="text-sm font-medium text-ink/90">{job.detail || 'Working…'}</p>
            <p class="text-xs text-muted">Generation runs on the server and continues even if you close this panel.</p>
          </div>
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

          <!-- Style preset -->
          <div>
            <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Style</div>
            <div class="grid grid-cols-3 gap-1.5">
              {#each PRESETS as p (p.id)}
                <button type="button" title={p.help}
                  class="rounded-lg border px-3 py-2 text-sm font-semibold transition {preset === p.id ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                  onclick={() => (preset = p.id)}>{p.label}</button>
              {/each}
            </div>
            <p class="mt-2 text-xs leading-relaxed text-muted">{PRESETS.find((p) => p.id === preset)?.help}</p>
          </div>

          <!-- Tightness -->
          <div>
            <div class="mb-2 flex items-center justify-between text-xs font-bold uppercase tracking-wider text-muted">
              <span class="inline-flex items-center gap-1">
                Cut tightness
                <span class="grid h-4 w-4 cursor-help place-items-center rounded-full border border-current text-[10px] font-bold opacity-70" title={TIGHTNESS_TIP}>?</span>
              </span>
              <span class="normal-case text-[var(--accent)]" title={tightnessHelp}>{tightnessLabel}</span>
            </div>
            <input type="range" min="0" max="1" step="0.05" bind:value={tightness} class="w-full accent-[var(--accent)]" title={TIGHTNESS_TIP} />
            <div class="mb-1 flex justify-between text-[11px] text-muted"><span>Relaxed</span><span>Tight</span></div>
            <p class="text-xs leading-relaxed text-muted">{tightnessHelp}</p>
          </div>

          <!-- Let clips speak (audio ducking) -->
          <div class="rounded-lg border border-line p-3">
            <label class="flex cursor-pointer items-start gap-3">
              <input type="checkbox" bind:checked={letClipsSpeak}
                class="mt-0.5 h-4 w-4 shrink-0 accent-[var(--accent)]" />
              <span>
                <span class="block text-sm font-semibold">Let clips speak</span>
                <span class="mt-1 block text-xs leading-relaxed text-muted">
                  In the song's quiet stretches, hold on a clip that has dialogue and dip the
                  music so a full line comes through, then pick the beat back up. Only uses clips
                  that already have subtitles (.srt/.vtt); clips without speech are skipped.
                </span>
              </span>
            </label>
            {#if letClipsSpeak}
              <div class="mt-3 flex items-center gap-2 pl-7">
                <span class="text-xs font-bold uppercase tracking-wider text-muted">Moments</span>
                {#each ['auto', '1', '2', '3', '4'] as m (m)}
                  <button type="button"
                    class="rounded-lg border px-2.5 py-1 text-xs font-semibold capitalize transition {speakMoments === m ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                    onclick={() => (speakMoments = m)}>{m}</button>
                {/each}
              </div>
            {/if}
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
          {#if activeIds.length < 2}Select at least 2 videos.{:else if !song}Choose a song to continue.{:else}Ready to generate.{/if}
        </p>
        <button type="button" class="rounded-lg bg-[var(--accent)] px-5 py-2.5 font-bold text-[var(--on-accent)] disabled:opacity-45"
          disabled={!canGenerate} onclick={generate}>
          {starting ? 'Starting…' : 'Generate Montage'}
        </button>
      </footer>
    {/if}
  </div>
</div>
