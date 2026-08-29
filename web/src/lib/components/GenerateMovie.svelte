<script>
  import { onMount, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import ParticleField from './ParticleField.svelte';
  import { generateMovie, movieResultUrl, commitMovie, restyleMovie, motionCoverage, startMotionCache, movieResolutions } from '$lib/api.js';
  import { collections, loadCollections, setStashed, movieJob, movieChip, ensureMoviePolling, refreshMovieStatus, markMovieStarted, acknowledgeMovie, montageMode, montageRender } from '$lib/state.js';
  import { toast } from '$lib/toast.js';

  // videoIds: ordered ids of the currently-selected videos (selection order).
  let { videoIds = [], onclose = () => {} } = $props();

  // Aspect first, exact size second: what hurts a montage is aspect mismatch
  // (cover-cropping a landscape clip into a vertical frame loses most of it),
  // not resolution mismatch (scaling is benign). Picking an aspect reveals the
  // resolutions actually present in the current pool (with counts) to set the
  // canvas — and, in Auto-pick, also FILTERS the pool to matching clips.
  const ASPECTS = [
    { id: 'auto', label: 'Auto' },
    { id: 'landscape', label: 'Landscape' },
    { id: 'portrait', label: 'Vertical' },
    { id: 'square', label: 'Square' }
  ];
  // When the histogram is unavailable (older server / empty index) the picker
  // still works with the common Grok sizes, just without counts.
  const FALLBACK_SIZES = {
    landscape: [{ w: 1920, h: 1080 }, { w: 1280, h: 720 }],
    portrait: [{ w: 1080, h: 1920 }, { w: 720, h: 1280 }],
    square: [{ w: 1080, h: 1080 }]
  };
  const STAGE_LABEL = {
    queued: 'Queued', analyzing_audio: 'Analyzing audio', analyzing_motion: 'Analyzing motion',
    // Match Cut's own stage: scoring every ordered clip pair's best seam.
    matching: 'Matching motion',
    // Auto Montage's own stage: choosing which library clips best serve the song.
    selecting: 'Choosing clips',
    planning: 'Planning cuts', rendering: 'Rendering', done: 'Done', error: 'Error'
  };

  // Render mode. 'beat' = the beat-synced montage (song required); 'match' = Motion
  // Match Cut, which splices where motion aligns and needs no song. Kept in a store
  // so the choice survives the panel being destroyed on close.
  const MODES = [
    { id: 'beat', label: 'Beat Montage', help: 'Cuts land on the song’s beats. Pick a style below; the song sets the pace.' },
    { id: 'match', label: 'Match Cut', help: 'Finds frames where motion lines up across different clips — direction, speed, push-in, rotation — and splices there, so movement appears to continue through the cut. Shots are gently retimed so the speed matches too. No song needed; add one and it plays underneath.' }
  ];

  const PRESETS = [
    { id: 'classic', label: 'Classic', help: 'Punchy hard cuts on the beat — the original style.' },
    { id: 'cinematic', label: 'Cinematic', help: 'Smarter analysis, full-frame framing (no black bars), beat-timed transitions at section changes/drops, a gentle push-in that lets shots breathe, and a zoom punch on the drops.' },
    { id: 'moody', label: 'Moody', help: 'Long held shots with a slow push-in, punctuated by quick beat bursts on the loud parts. Calmer footage.' },
    { id: 'musicvideo', label: 'Music Video', help: 'Maximum-energy beat edit: machine-gun sub-beat cutting that flurries on the drops, hard zoom-punches, RGB-split + camera-shake hits, white-flash strobes on the loud beats, and a saturated neon grade. Scene-aware cuts, edge-to-edge.' }
  ];
  // Picture & Video mode is delivered by its own preset (the Music-Video edit, but still
  // images are allowed as beats — held on the beat with a Ken-Burns push, letterboxed).
  // It's shown as the ONLY style in picture-video mode; the four above are video-only.
  const PICVIDEO_PRESET = { id: 'picvideo', label: 'Picture & Video', help: 'Music-Video energy, but your still images ride along as beats too — each photo is held on the beat with a slow Ken-Burns push and letterboxed so its whole frame is kept. Videos and photos are cut together to the song.' };
  const ALL_PRESETS = [...PRESETS, PICVIDEO_PRESET]; // for label/help lookup by id
  // 'video' shows the four styles; 'picture-video' shows only the picvideo style.
  const picVideo = $derived($montageMode === 'picture-video');
  const shownPresets = $derived(picVideo ? [PICVIDEO_PRESET] : PRESETS);

  let song = $state(null);
  // `preset` is the chosen VIDEO style (classic/cinematic/moody/musicvideo). Picture &
  // Video mode is delivered by the picvideo preset, so the EFFECTIVE preset we send is
  // picvideo whenever the mode is picture-video — a pure derivation, never an $effect
  // writing state (so switching modes can't get out of sync with the selected style).
  let preset = $state('classic');
  const effectivePreset = $derived(picVideo ? 'picvideo' : preset);
  let tightness = $state(0.5);
  // "Let clips speak" — preset-independent. When on, hold on clips that have
  // dialogue (from their subtitles) in the song's quiet spots and dip the music.
  let letClipsSpeak = $state(false);
  let speakMoments = $state('auto'); // 'auto' | '1'..'4'
  let aspectId = $state('auto');    // 'auto' | 'landscape' | 'portrait' | 'square'
  let sizeKey = $state('');         // "WxH" of the chosen sub-resolution chip
  let fps = $state(30);
  let targetDuration = $state('');
  let name = $state('movie');
  let dragging = $state(false);
  // Style/tightness info popovers — hover on desktop, tap on mobile. Kept as an
  // absolutely-positioned overlay so the (long) description adds no height; the
  // inline version used to push the panel into scrolling once Music Video landed.
  let showStyleInfo = $state(false);
  let showTightInfo = $state(false);
  let showModeInfo = $state(false);
  // Match Cut extras.
  let matchSpeed = $state(true);
  let matchDissolve = $state(false);
  let keepClipAudio = $state(true);

  // Auto Montage (beat only): instead of the hand-picked selection, the server's
  // clip picker chooses from the whole library — or just the ticked collections —
  // whatever best serves this song, using the pre-analyzed motion cache.
  let autoPick = $state(false);
  let autoCollections = $state([]); // collection ids; empty = whole library
  // Motion-cache coverage ({ videos, cached, running }) — gates Auto-pick and
  // drives the Analyze Library readout. Polled only while the warm-up runs.
  let coverage = $state(null);
  let coverageTimer = null;
  async function refreshCoverage() {
    try {
      coverage = await motionCoverage();
    } catch { /* older server build — the Auto section degrades gracefully */ }
    clearTimeout(coverageTimer);
    if (coverage?.running) coverageTimer = setTimeout(refreshCoverage, 2000);
  }
  async function analyzeLibrary() {
    try {
      const res = await startMotionCache();
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Another job is already running.');
      }
      coverage = { ...(coverage || { videos: 0, cached: 0 }), running: true };
      toast('Analyzing library — watch progress in the sync log.', { type: 'success' });
      refreshCoverage();
    } catch (e) {
      toast(e.message || 'Could not start the analysis.', { type: 'error' });
    }
  }
  function toggleAutoCollection(id) {
    autoCollections = autoCollections.includes(id)
      ? autoCollections.filter((c) => c !== id)
      : [...autoCollections, id];
  }
  // Locked/sealed collections stay private — never offered as an auto-pick pool
  // (the server skips them too).
  const pickableCollections = $derived(($collections || []).filter((c) => !c.locked));
  // What a collection chip's number means: with an aspect chosen, the clips that
  // collection would actually CONTRIBUTE to the pool (matching-orientation videos
  // — the pool filters at orientation level; a specific sub-resolution only sets
  // the canvas, all same-orientation clips still qualify). With aspect on Auto,
  // its eligible video count when known, else the total item count.
  function collectionChipCount(c) {
    const per = resolutionStats?.collections?.[c.id];
    if (per) return aspectId !== 'auto' ? (per[aspectId] ?? 0) : per.videos;
    return c.item_count ?? c.ids?.length ?? 0;
  }
  // Chips ordered by what they'd actually contribute (most relevant clips
  // first, so the useful pools are on top); re-sorts as the aspect changes.
  // Name tie-break keeps equal-count chips stable across refetches.
  const sortedCollections = $derived.by(() =>
    [...pickableCollections].sort((a, b) =>
      collectionChipCount(b) - collectionChipCount(a)
      || (a.name || '').localeCompare(b.name || '')));
  // Rough candidate count for the footer hint (whole library uses coverage.videos).
  const autoCandidateEstimate = $derived(
    autoCollections.length
      ? pickableCollections.filter((c) => autoCollections.includes(c.id))
          .reduce((n, c) => n + (c.item_count ?? c.ids?.length ?? 0), 0)
      : (coverage?.videos ?? 0)
  );

  // Resolution histogram of the CURRENT pool (auto-pick: the collections / whole
  // library; manual: the selection) — powers the aspect picker's counts, the
  // sub-resolution chips, and the "will be cropped" warning.
  let resolutionStats = $state(null);
  let resSeq = 0; // guards against a slow older response landing after a newer one
  $effect(() => {
    const req = autoPick && !matchCut
      ? { collections: [...autoCollections], perCollection: true }
      : { ids: [...activeIds] };
    const seq = ++resSeq;
    movieResolutions(req)
      .then((r) => { if (seq === resSeq) resolutionStats = r; })
      .catch(() => { if (seq === resSeq) resolutionStats = null; }); // older server — fallback sizes carry it
  });
  const aspectCount = (o) =>
    (resolutionStats?.sizes || []).filter((s) => s.orientation === o)
      .reduce((n, s) => n + s.count, 0);
  // Sizes offered for the chosen aspect: what's really in the pool (commonest
  // first — rendering at the dominant size keeps most clips native), else the
  // static fallbacks.
  const sizesForAspect = $derived.by(() => {
    if (aspectId === 'auto') return [];
    const real = (resolutionStats?.sizes || []).filter((s) => s.orientation === aspectId);
    return real.length ? real : FALLBACK_SIZES[aspectId] || [];
  });
  const effectiveSize = $derived(
    sizesForAspect.find((s) => `${s.w}x${s.h}` === sizeKey) || sizesForAspect[0] || null);
  // Manual selection + explicit aspect: how many chosen clips DON'T match and
  // will be cropped/letterboxed (the selection is never filtered — it's yours).
  const mismatched = $derived.by(() => {
    if (autoEffective || aspectId === 'auto' || !resolutionStats) return 0;
    return (resolutionStats.total - aspectCount(aspectId)) + (resolutionStats.unknown || 0);
  });

  let starting = $state(false);
  let startError = $state(''); // error from the kickoff POST (shown in-panel)
  // Takes: per-take commit state lives on the server (t.committed); this is the
  // take whose "Add to Collection" is in flight, and whether a restyle is starting.
  let committingId = $state(null);
  let restyling = $state(false);
  // The take the user clicked in the takes strip; null = show the newest finished one.
  let selectedTakeId = $state(null);
  // Takes this panel asked for that haven't landed yet: the preview stays on the
  // selected take while they render, then jumps to each as it finishes.
  let awaiting = $state([]);
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

  // A LIVE job's own mode always wins over the local toggle — the panel is destroyed
  // on close, so on reopen the server snapshot is the only truthful signal.
  const jobMode = $derived(job?.mode ?? $montageRender);
  const matchCut = $derived(jobMode === 'match' || jobMode === 'matchcut');
  // Picture & Video is about which media may enter the basket; Match Cut is video-only
  // (a still has no motion to match), so the two never combine.
  const picVideoEffective = $derived(picVideo && !matchCut);

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
  // Takes (Aug 2026): one session (clips + song + settings) can hold several
  // renders — other styles, or the same style with a fresh seed — kept side by side.
  const takes = $derived(job?.takes ?? []);
  const doneTakes = $derived(takes.filter((t) => t.status === 'done' && t.result));
  const queuedCount = $derived(takes.filter((t) => t.status === 'queued').length);
  const activeTake = $derived(takes.find((t) => t.job_id === job?.job_id) ?? null);
  // The take on screen: the user's pick, else the most recent finished one.
  const current = $derived(doneTakes.find((t) => t.job_id === selectedTakeId) ?? doneTakes[doneTakes.length - 1] ?? null);
  // "done" = there is a finished take to show; more takes may still be rendering
  // behind it (their progress is shown inline under the takes strip).
  const done = $derived(!!current);
  const errored = $derived(!!job && job.status === 'error' && !current);
  const styles = $derived(job?.session?.styles ?? []);
  const missingStyles = $derived(styles.filter((s) => !takes.some((t) => t.preset === s && t.status !== 'error')));
  const labelFor = (id) => ALL_PRESETS.find((p) => p.id === id)?.label || id || '';
  // "Classic · take 2" when a style was rendered more than once (fresh seeds).
  function takeLabel(t) {
    const same = takes.filter((x) => x.preset === t.preset);
    const n = same.indexOf(t) + 1;
    return same.length > 1 ? `${labelFor(t.preset)} · take ${n}` : labelFor(t.preset);
  }
  // Auto-pick is a beat-mode concept (the picker scores clips against the song's
  // grid); flipping to Match Cut falls back to the hand-picked selection.
  const autoEffective = $derived(autoPick && !matchCut);
  // Auto needs at least 2 analyzed clips in the cache to have anything to pick.
  const autoReady = $derived((coverage?.cached ?? 0) >= 2);
  // The song is OPTIONAL in Match Cut — the edit is driven by motion continuity, and
  // with no song the render is silent.
  const canGenerate = $derived(
    (autoEffective ? autoReady : activeIds.length >= 2)
    && (matchCut || !!song) && !starting && !running);
  // Prefer the JOB's source count + preset (from server provenance) whenever a job
  // is owned — the live selection is only meaningful on the fresh setup form and is
  // empty when the panel is reopened from the status chip.
  const sourceCount = $derived(job?.sources ?? activeIds.length);
  const styleLabel = $derived(labelFor(current?.preset ?? job?.preset ?? job?.result?.preset));

  onMount(() => {
    (async () => {
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
      } else if (!videoIds.length) {
        // Opened selection-free (the topbar Montage button): Auto-pick is the only
        // way to generate with no clips in hand, so start there.
        autoPick = true;
      }
      // Auto-pick needs the collections list + cache coverage; both are cheap and
      // load in the background so the form never blocks on them.
      loadCollections();
      refreshCoverage();
    })();
    return () => clearTimeout(coverageTimer);
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
    // Auto-pick sends no ids — the server resolves the pool from the collections
    // (or the whole library) and the render's picker does the choosing.
    const ids = autoEffective ? [] : [...activeIds];
    sessionIds = ids;
    try {
      const data = await generateMovie({
        ids,
        song,
        options: {
          name: name.trim() || 'movie',
          mode: matchCut ? 'matchcut' : 'beat',
          ...(autoEffective ? { auto_pick: 1,
                                ...(autoCollections.length
                                    ? { auto_collections: JSON.stringify(autoCollections) } : {}) } : {}),
          ...(matchCut ? { match_speed: matchSpeed ? 1 : 0, match_dissolve: matchDissolve ? 1 : 0,
                           keep_audio: keepClipAudio ? 1 : 0 } : {}),
          preset: effectivePreset,
          tightness,
          // Auto lets the server size the canvas (dominant resolution of an
          // auto-pick pool; largest source clip for a manual selection). An
          // explicit aspect pins the picked size — and in Auto-pick also filters
          // the candidate pool to matching-orientation clips.
          ...(aspectId !== 'auto' && effectiveSize
              ? { width: effectiveSize.w, height: effectiveSize.h } : { resolution: 'auto' }),
          ...(autoEffective && aspectId !== 'auto' ? { aspect: aspectId } : {}),
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
      markMovieStarted(data.job_id, matchCut ? 'match' : 'beat');
    } catch (e) {
      startError = e.message || 'Could not start generation.';
      toast(startError, { type: 'error' });
    } finally {
      starting = false;
    }
  }

  async function addToCollection(take) {
    if (!take || committingId || take.committed) return;
    committingId = take.job_id;
    try {
      const res = await commitMovie(take.job_id);
      // Auto-archive the finished montage so it doesn't clutter Recent — it stays
      // in the "Beat Montage" collection (and Archive / All Media).
      if (res?.id) setStashed([String(res.id)], true);
      const s = await refreshMovieStatus(); // per-take committed flags + session ack
      await loadCollections(); // surface the new "Beat Montage" collection
      toast(`Added ${labelFor(take.preset)} take to “Beat Montage”`, { type: 'success' });
      // Every finished take is filed and nothing is rendering: the session is dealt
      // with — clear the chip and dismiss the preview, as a single render always did.
      if (s?.acknowledged) {
        acknowledgeMovie(s.job_id);
        close();
      }
    } catch (e) {
      toast(e.message || 'Could not add to collection.', { type: 'error' });
    } finally {
      committingId = null;
    }
  }

  // Another take of the SAME session: a different style, every missing style, or
  // the current style with a fresh seed. The server reuses the clips + song and
  // the caches, so only the plan + render run. The panel stays on the current take
  // and the new one lands in the takes strip (selected automatically when done).
  async function restyle(opts) {
    if (restyling) return;
    restyling = true;
    try {
      const data = await restyleMovie(opts);
      awaiting = [...awaiting, ...((data?.job_ids) || [])];
      owned = true;
      ensureMoviePolling();
    } catch (e) {
      toast(e.message || 'Could not start another take.', { type: 'error' });
    } finally {
      restyling = false;
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
  $effect(() => { if (doneTakes.length) untrack(() => burstCount++); });
  // A take this panel queued has finished: show it. (Re-runs until `awaiting` is
  // drained, so several landing between polls end on the newest.)
  $effect(() => {
    const hit = doneTakes.find((t) => awaiting.includes(t.job_id));
    if (hit) untrack(() => { selectedTakeId = hit.job_id; awaiting = awaiting.filter((id) => id !== hit.job_id); });
  });
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
        <h2 class="text-lg font-extrabold tracking-tight">{matchCut ? 'Create Match Cut' : 'Create Montage'}</h2>
        <p class="text-sm text-muted">
          {#if matchCut}Motion match cut from {sourceCount} selected video{sourceCount === 1 ? '' : 's'}.
          {:else if job ? job.auto_pick : autoEffective}Beat-synced montage · clips auto-picked for the song{styleLabel ? ` · ${styleLabel} style` : ''}.
          {:else}Beat-synced montage from {sourceCount} selected {picVideoEffective ? 'item' : 'video'}{sourceCount === 1 ? '' : 's'}{styleLabel ? ` · ${styleLabel} style` : ''}.{/if}
        </p>
      </div>
      <button type="button" class="grid h-9 w-9 place-items-center rounded-lg border border-line"
        aria-label="Close" onclick={close}>✕</button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      {#if done}
        <!-- Result -->
        <div class="space-y-4">
          <!-- Music-only generated preview has no speech track. -->
          <!-- svelte-ignore a11y_media_has_caption -->
          {#key current.job_id}
            <video class="mx-auto block max-h-[55dvh] max-w-full rounded-xl bg-[var(--media-bg)]" src={movieResultUrl(false, current.job_id, current.job_id)} controls playsinline autoplay></video>
          {/key}
          <p class="text-sm text-muted">
            {#if styleLabel}<span class="font-semibold text-ink/90">{styleLabel}</span>{' · '}{/if}{current.result.cuts} cuts · {current.result.width}×{current.result.height} · {current.result.fps} fps · {current.result.duration}s
            · {(current.result.size_bytes / 1048576).toFixed(1)} MB{#if current.result.beat_engine}{' · '}{current.result.beat_engine === 'madmom' ? `madmom beats (${current.result.beat_device})` : `librosa beats${current.result.beat_engine_note ? ` — ${current.result.beat_engine_note}` : ''}`}{/if}
          </p>
          {#if current.result.match}
            <!-- Report what the matcher actually found. A weak run must be legible as
                 "no strong matches available" rather than reading as a plain montage. -->
            <p class="text-xs text-muted">
              Matched {current.result.match.clips_gated} of {current.result.match.clips_in} clips{#if current.result.match.clips_in > current.result.match.clips_gated} · {current.result.match.clips_in - current.result.match.clips_gated} had too little motion to match{/if}
              · seam quality {current.result.match.seam_mean_chain} of {current.result.match.seam_max_possible}{#if current.result.match.speed_ramped} · {current.result.match.speed_ramped} shot{current.result.match.speed_ramped === 1 ? '' : 's'} speed-matched{/if}
            </p>
          {/if}
          {#if current.result.auto}
            <!-- What Auto Montage chose, and whether any candidates were invisible
                 to it (not yet analyzed) — that's the cue to run Analyze Library. -->
            <p class="text-xs text-muted">
              Auto-picked {current.result.auto.picked} clips from {current.result.auto.analyzed} analyzed candidate{current.result.auto.analyzed === 1 ? '' : 's'}{#if current.result.auto.skipped_uncached} · {current.result.auto.skipped_uncached} skipped (not yet analyzed){/if}
            </p>
          {/if}
          <!-- Takes: every render of this session. Finished ones are selectable
               (the preview swaps), rendering/queued ones show their state, and
               each style not yet rendered is a ghost chip that renders it — same
               clips, song, settings and seed, so the comparison isolates the style. -->
          {#if takes.length > 1 || missingStyles.length}
            <div>
              <div class="mb-2 flex items-center justify-between text-xs font-bold uppercase tracking-wider text-muted">
                <span>Takes</span>
                {#if missingStyles.length > 1}
                  <button type="button" class="font-semibold normal-case tracking-normal text-[var(--accent)] disabled:opacity-50"
                    disabled={restyling} onclick={() => restyle({ all: true })}>Render all styles ({missingStyles.length} more)</button>
                {/if}
              </div>
              <div class="flex flex-wrap gap-1.5">
                {#each takes as t (t.job_id)}
                  <button type="button" title={t.status === 'error' ? (t.error || 'Failed') : t.status === 'done' ? 'Show this take' : STAGE_LABEL[t.status] || t.status}
                    class="rounded-lg border px-3 py-1.5 text-left text-sm font-semibold transition disabled:cursor-default {t.job_id === current.job_id ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : t.status === 'done' ? 'border-line hover:border-[var(--accent)]' : 'border-line opacity-70'}"
                    disabled={t.status !== 'done'} onclick={() => (selectedTakeId = t.job_id)}>
                    {takeLabel(t)}
                    <span class="ml-1 text-[11px] font-normal opacity-75">
                      {#if t.status === 'done'}{t.committed ? '✓ saved' : `${t.result.cuts} cuts`}{:else if t.status === 'queued'}queued{:else if t.status === 'error'}failed{:else}{Math.round((t.progress || 0) * 100)}%{/if}
                    </span>
                  </button>
                {/each}
                {#each missingStyles as s (s)}
                  <button type="button" title={`Render this montage in the ${labelFor(s)} style`}
                    class="rounded-lg border border-dashed border-line px-3 py-1.5 text-sm font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink disabled:opacity-50"
                    disabled={restyling} onclick={() => restyle({ preset: s })}>+ {labelFor(s)}</button>
                {/each}
              </div>
            </div>
          {/if}
          {#if running && activeTake}
            <!-- A later take rendering behind the one on screen. -->
            <div class="space-y-1.5">
              <div class="flex items-center justify-between text-xs font-semibold">
                <span>{STAGE_LABEL[activeTake.status] || activeTake.status} · {labelFor(activeTake.preset)}{#if queuedCount} · {queuedCount} queued{/if}</span>
                <span class="text-muted">{pct}%</span>
              </div>
              <div class="h-1.5 w-full overflow-hidden rounded-full bg-[var(--surface-2)]">
                <div class="h-full rounded-full bg-[var(--accent)] transition-[width] duration-500" style="width: {pct}%"></div>
              </div>
              <p class="text-xs text-muted">{job.detail || 'Working…'}</p>
            </div>
          {/if}
          <div class="flex flex-wrap gap-2">
            <a class="rounded-lg bg-[var(--accent)] px-4 py-2.5 font-bold text-[var(--on-accent)]" href={movieResultUrl(true, current.job_id, current.job_id)} download={current.result.filename}>⇩ Download MP4</a>
            <button type="button" class="rounded-lg border border-line px-4 py-2.5 font-semibold disabled:opacity-60"
              disabled={committingId === current.job_id || current.committed} onclick={() => addToCollection(current)}>
              {#if current.committed}✓ In “Beat Montage”{:else if committingId === current.job_id}Adding…{:else}+ Add to Collection{/if}
            </button>
            <button type="button" class="rounded-lg border border-line px-4 py-2.5 font-semibold disabled:opacity-60"
              title="Render this style again with a fresh seed — different moments, same clips and song"
              disabled={restyling} onclick={() => restyle({ preset: current.preset, newSeed: true })}>↻ Another take</button>
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
        <div class="space-y-3">
          {#if startError}
            <p class="rounded-lg border border-[var(--danger-border)] bg-[var(--danger-bg)] px-3 py-2 text-sm text-[var(--danger-ink-soft)]">{startError}</p>
          {:else if errored}
            <p class="rounded-lg border border-[var(--danger-border)] bg-[var(--danger-bg)] px-3 py-2 text-sm text-[var(--danger-ink-soft)]">{job.error || 'Generation failed.'}</p>
          {/if}

          <!-- Mode: Beat Montage | Match Cut. Lives INSIDE the scroll body (the panel
               has one flexible scroll region; anything added to the fixed header or
               footer permanently shrinks it — see the notes above). The beat-only
               Cut-tightness block is hidden in match mode, so net height is neutral. -->
          <div>
            <div class="mb-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-muted">
              <span>Mode</span>
              <span class="group relative inline-flex">
                <button type="button" aria-label="About this mode" aria-expanded={showModeInfo}
                  onclick={() => (showModeInfo = !showModeInfo)}
                  class="grid h-4 w-4 place-items-center rounded-full border border-current text-[10px] font-bold opacity-70 transition hover:opacity-100 pointer-coarse:h-5 pointer-coarse:w-5">i</button>
                <span class="pointer-events-none absolute left-0 top-full z-20 mt-1.5 w-64 max-w-[80vw] rounded-lg border border-line bg-[var(--surface-solid)] p-2.5 text-[11px] font-normal normal-case leading-relaxed text-muted shadow-lg transition {showModeInfo ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}">{MODES.find((m) => m.id === (matchCut ? 'match' : 'beat'))?.help}</span>
              </span>
            </div>
            <div class="grid grid-cols-2 gap-1.5">
              {#each MODES as m (m.id)}
                <button type="button" title={m.help}
                  class="rounded-lg border px-3 py-1.5 text-sm font-semibold transition {(matchCut ? 'match' : 'beat') === m.id ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                  onclick={() => montageRender.set(m.id)}>{m.label}</button>
              {/each}
            </div>
          </div>

          <!-- Clips: the hand-picked selection, or Auto Montage — the server picks
               from the pre-analyzed library whatever best serves this song. Beat
               only: the picker scores clips against the song's beat grid. -->
          {#if !matchCut}
          <div>
            <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Clips</div>
            <div class="grid grid-cols-2 gap-1.5">
              <button type="button"
                class="rounded-lg border px-3 py-1.5 text-sm font-semibold transition {!autoPick ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                onclick={() => (autoPick = false)}>My selection ({activeIds.length})</button>
              <button type="button" title="Analyzes the song, then picks the clips whose motion best fits it — calm shots for quiet passages, wild ones for the drops."
                class="rounded-lg border px-3 py-1.5 text-sm font-semibold transition {autoPick ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                onclick={() => (autoPick = true)}>Auto-pick for this song</button>
            </div>
            {#if autoPick}
              <div class="mt-2 space-y-2.5 rounded-lg border border-line p-3">
                <p class="text-[11px] leading-snug text-muted">
                  The song is analyzed first, then clips whose motion best fits its energy are chosen from
                  {autoCollections.length ? 'the ticked collections' : 'your whole library'} — calm footage for the quiet passages, the wildest shots saved for the drops.
                </p>
                {#if pickableCollections.length}
                  <div class="max-h-28 overflow-y-auto">
                    <div class="flex flex-wrap gap-1.5">
                      <button type="button"
                        class="rounded-full border px-2.5 py-1 text-xs font-semibold transition {!autoCollections.length ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                        onclick={() => (autoCollections = [])}>Whole library</button>
                      {#each sortedCollections as c (c.id)}
                        <!-- Collections that would contribute nothing at the chosen
                             aspect stay tickable but read as empty. -->
                        <button type="button"
                          class="rounded-full border px-2.5 py-1 text-xs font-semibold transition {autoCollections.includes(c.id) ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'} {collectionChipCount(c) === 0 ? 'opacity-50' : ''}"
                          onclick={() => toggleAutoCollection(c.id)}>{c.name} <span class="opacity-60">{collectionChipCount(c)}</span></button>
                      {/each}
                    </div>
                  </div>
                {/if}
                {#if coverage}
                  {#if coverage.running}
                    <p class="text-xs text-muted">Analyzing library… {coverage.cached} of {coverage.videos} clips done — progress is in the sync log.</p>
                  {:else if coverage.cached < coverage.videos}
                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <span class="text-xs text-muted">{coverage.cached} of {coverage.videos} clips analyzed{!autoReady ? ' — analyze first' : ''}.</span>
                      <button type="button" class="rounded-lg border border-line px-2.5 py-1.5 text-xs font-semibold transition hover:border-[var(--accent)]"
                        onclick={analyzeLibrary}>Analyze library</button>
                    </div>
                  {:else}
                    <p class="text-xs text-muted">All {coverage.videos} clips analyzed — syncs keep this up to date.</p>
                  {/if}
                {/if}
              </div>
            {/if}
          </div>
          {/if}

          <!-- Song -->
          <div>
            <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Song {#if matchCut}<span class="normal-case opacity-70">— optional</span>{/if}</div>
            <label class="flex cursor-pointer items-center gap-3 rounded-xl border border-dashed px-4 py-3.5 text-sm transition {dragging ? 'border-[var(--accent)] bg-[var(--accent)]/5' : 'border-line'}"
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
                  <span class="text-xs text-muted">{matchCut ? 'Optional — leave empty for a silent cut' : 'mp3 · wav · flac · m4a · aac · ogg · opus'}</span>
                {/if}
              </span>
              <input type="file" accept="audio/*,.mp3,.wav,.flac,.m4a,.aac,.ogg,.opus" class="hidden" onchange={pickFile} />
            </label>
          </div>

          <!-- Style, tightness and "let clips speak" are all BEAT-only: they read the
               song's grid. Match Cut replaces them with its own two options. -->
          {#if !matchCut}
          <!-- Style preset -->
          <div>
            <div class="mb-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-muted">
              <span>Style</span>
              <!-- Info as a hover (desktop) / tap (mobile) overlay so the preset
                   description doesn't add height and scroll the panel. -->
              <span class="group relative inline-flex">
                <button type="button" aria-label="About this style" aria-expanded={showStyleInfo}
                  onclick={() => (showStyleInfo = !showStyleInfo)}
                  class="grid h-4 w-4 place-items-center rounded-full border border-current text-[10px] font-bold opacity-70 transition hover:opacity-100 pointer-coarse:h-5 pointer-coarse:w-5">i</button>
                <span class="pointer-events-none absolute left-0 top-full z-20 mt-1.5 w-64 max-w-[80vw] rounded-lg border border-line bg-[var(--surface-solid)] p-2.5 text-[11px] font-normal normal-case leading-relaxed text-muted shadow-lg transition {showStyleInfo ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}">{ALL_PRESETS.find((p) => p.id === effectivePreset)?.help}</span>
              </span>
            </div>
            <div class="grid grid-cols-2 gap-1.5">
              {#each shownPresets as p (p.id)}
                <button type="button" title={p.help}
                  class="rounded-lg border px-3 py-1.5 text-sm font-semibold transition {effectivePreset === p.id ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                  onclick={() => { if (!picVideo) preset = p.id; }}>{p.label}</button>
              {/each}
            </div>
          </div>

          <!-- Tightness -->
          <div>
            <div class="mb-2 flex items-center justify-between text-xs font-bold uppercase tracking-wider text-muted">
              <span class="inline-flex items-center gap-1">
                Cut tightness
                <!-- Hover (desktop) / tap (mobile) overlay — keeps the description out of the layout. -->
                <span class="group relative inline-flex">
                  <button type="button" aria-label="About cut tightness" aria-expanded={showTightInfo}
                    onclick={() => (showTightInfo = !showTightInfo)}
                    class="grid h-4 w-4 place-items-center rounded-full border border-current text-[10px] font-bold opacity-70 transition hover:opacity-100 pointer-coarse:h-5 pointer-coarse:w-5">?</button>
                  <span class="pointer-events-none absolute left-0 top-full z-20 mt-1.5 w-64 max-w-[80vw] rounded-lg border border-line bg-[var(--surface-solid)] p-2.5 text-[11px] font-normal normal-case leading-relaxed text-muted shadow-lg transition {showTightInfo ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}">{tightnessHelp}</span>
                </span>
              </span>
              <span class="normal-case text-[var(--accent)]">{tightnessLabel}</span>
            </div>
            <input type="range" min="0" max="1" step="0.05" bind:value={tightness} class="w-full accent-[var(--accent)]" title={TIGHTNESS_TIP} />
            <div class="flex justify-between text-[11px] text-muted"><span>Relaxed</span><span>Tight</span></div>
          </div>

          <!-- Let clips speak (audio ducking) -->
          <div class="rounded-lg border border-line p-3">
            <label class="flex cursor-pointer items-start gap-3">
              <input type="checkbox" bind:checked={letClipsSpeak}
                class="mt-0.5 h-4 w-4 shrink-0 accent-[var(--accent)]" />
              <span>
                <span class="block text-sm font-semibold">Let clips speak</span>
                <span class="mt-0.5 block text-[11px] leading-snug text-muted">
                  In quiet stretches, hold on a clip with dialogue and dip the music so the line comes through. Only clips with subtitles (.srt/.vtt).
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
          {:else}
          <!-- Match Cut options -->
          <div class="space-y-2 rounded-lg border border-line p-3">
            <label class="flex cursor-pointer items-start gap-3">
              <input type="checkbox" bind:checked={matchSpeed} class="mt-0.5 h-4 w-4 shrink-0 accent-[var(--accent)]" />
              <span>
                <span class="block text-sm font-semibold">Match speed across cuts</span>
                <span class="mt-0.5 block text-[11px] leading-snug text-muted">
                  Gently retime each shot so the movement carries the same speed through the cut, not just the same direction. Kept within a range that stays smooth.
                </span>
              </span>
            </label>
            <label class="flex cursor-pointer items-start gap-3">
              <input type="checkbox" bind:checked={matchDissolve} class="mt-0.5 h-4 w-4 shrink-0 accent-[var(--accent)]" />
              <span>
                <span class="block text-sm font-semibold">Blend the seam</span>
                <span class="mt-0.5 block text-[11px] leading-snug text-muted">
                  Cross-fade three frames at each cut. On motion that already lines up this reads as a morph rather than a fade.
                </span>
              </span>
            </label>
            <!-- Only meaningful with no song — with one, the music is the track. -->
            {#if !song}
              <label class="flex cursor-pointer items-start gap-3">
                <input type="checkbox" bind:checked={keepClipAudio} class="mt-0.5 h-4 w-4 shrink-0 accent-[var(--accent)]" />
                <span>
                  <span class="block text-sm font-semibold">Keep the clips’ audio</span>
                  <span class="mt-0.5 block text-[11px] leading-snug text-muted">
                    With no song, each shot plays its own sound, retimed to match the picture. Clips without audio stay silent for their turn. Untick for a silent cut.
                  </span>
                </span>
              </label>
            {/if}
          </div>
          {/if}

          <!-- Resolution + fps -->
          <div class="grid gap-4 sm:grid-cols-2">
            <div>
              <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Aspect / resolution</div>
              <div class="grid grid-cols-2 gap-1.5">
                {#each ASPECTS as a (a.id)}
                  <button type="button" class="rounded-lg border px-2.5 py-1.5 text-xs font-semibold transition {aspectId === a.id ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                    onclick={() => { aspectId = a.id; sizeKey = ''; }}>
                    {a.label}{#if a.id !== 'auto' && resolutionStats}<span class="ml-1 opacity-60">{aspectCount(a.id)}</span>{/if}
                  </button>
                {/each}
              </div>
              {#if aspectId !== 'auto' && sizesForAspect.length}
                <!-- The resolutions actually present in this pool, commonest first —
                     rendering at the dominant size keeps most clips native. -->
                <div class="mt-1.5 flex flex-wrap gap-1.5">
                  {#each sizesForAspect as s (`${s.w}x${s.h}`)}
                    <button type="button"
                      class="rounded-full border px-2.5 py-1 text-xs font-semibold transition {effectiveSize && effectiveSize.w === s.w && effectiveSize.h === s.h ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                      onclick={() => (sizeKey = `${s.w}x${s.h}`)}>{s.w}×{s.h}{#if s.count}<span class="ml-1 opacity-60">{s.count}</span>{/if}</button>
                  {/each}
                </div>
              {/if}
              <p class="mt-1.5 text-[11px] leading-snug text-muted">
                {#if aspectId === 'auto'}
                  {autoEffective ? 'Sized to the pool’s most common clip shape — no cropping for most clips.' : 'Matches the largest clip — no cropping.'}
                {:else if autoEffective}
                  Only {ASPECTS.find((a) => a.id === aspectId)?.label.toLowerCase()} clips enter the pool — nothing gets cropped to shape.
                {:else if mismatched > 0}
                  <span class="text-[var(--danger-ink-soft,inherit)]">{mismatched} of {resolutionStats.total + (resolutionStats.unknown || 0)} selected clips aren’t {ASPECTS.find((a) => a.id === aspectId)?.label.toLowerCase()} and will be cropped or letterboxed.</span>
                {:else}
                  Fixed frame — off-shape clips are cropped or letterboxed.
                {/if}
              </p>
            </div>
            <div class="space-y-4">
              <div>
                <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Frame rate</div>
                <div class="grid grid-cols-3 gap-1.5">
                  {#each [24, 30, 60] as f (f)}
                    <button type="button" class="rounded-lg border px-2.5 py-1.5 text-xs font-semibold transition {fps === f ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                      onclick={() => (fps = f)}>{f}</button>
                  {/each}
                </div>
              </div>
              <div>
                <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Length (s) <span class="normal-case opacity-70">— blank = {matchCut ? 'as long as the matches run' : 'song length'}</span></div>
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
          {#if autoEffective}
            {#if !autoReady}Analyze the library first — Auto-pick chooses from pre-analyzed clips.{:else if !song}Choose a song to continue.{:else if aspectId !== 'auto' && resolutionStats}Ready — picking from ~{aspectCount(aspectId)} {ASPECTS.find((a) => a.id === aspectId)?.label.toLowerCase()} clips.{:else}Ready — picking from ~{autoCandidateEstimate} clips for this song.{/if}
          {:else if activeIds.length < 2}Select at least 2 {picVideoEffective ? 'photos or videos' : 'videos'}.{:else if !song && !matchCut}Choose a song to continue.{:else if matchCut && !song}Ready — no song, so the cut will be silent.{:else}Ready to generate.{/if}
        </p>
        <button type="button" class="rounded-lg bg-[var(--accent)] px-5 py-2.5 font-bold text-[var(--on-accent)] disabled:opacity-45"
          disabled={!canGenerate} onclick={generate}>
          {starting ? 'Starting…' : matchCut ? 'Generate Match Cut' : 'Generate Montage'}
        </button>
      </footer>
    {/if}
  </div>
</div>
