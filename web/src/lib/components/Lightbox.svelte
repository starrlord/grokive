<script module>
  import { loadVolume, saveVolume } from '$lib/state.js';
  // Beat-montage preset id -> human label, shown in the info panel for montages.
  const STYLE_LABELS = { classic: 'Classic', cinematic: 'Cinematic', moody: 'Moody', musicvideo: 'Music Video' };
  // One AudioContext shared across all lightbox opens — browsers cap how many you
  // can create. Routing the <video> through a context that's resumed inside a user
  // gesture makes the *context* the authorized audio output, so every subsequent
  // clip plays with sound without its own gesture — bypassing the per-clip
  // autoplay-audio gate (and Svelte's reactive `muted` churn). The per-element
  // source node is created per instance (see routeAudio).
  let sharedAudioCtx = null;
  function audioContext() {
    if (sharedAudioCtx) return sharedAudioCtx;
    const AC = typeof window !== 'undefined' && (window.AudioContext || window.webkitAudioContext);
    sharedAudioCtx = AC ? new AC() : null;
    return sharedAudioCtx;
  }
  let desktopSoundEnabled = false;
  // Last-set desktop volume (0..1). Seeded from localStorage so the chosen level sticks
  // across Lightbox opens AND page reloads (a per-device preference). Kept in sync below
  // via onVolumeChange. Volume LEVEL only — the muted-first autoplay path is untouched.
  let desktopVolume = loadVolume();
</script>

<script>
  import { onDestroy } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { favorites, toggleFavorite, removeMedia, deleted, sendToImagine, toggleBasket, basketMembers, queueImageForMontage, captionVideoHeight, slideSeconds, setSlideSeconds, lightboxChrome, collections, filters, activeCollectionId } from '$lib/state.js';
  import { mediaRelated } from '$lib/api.js';
  import { copyText } from '$lib/clipboard.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import { fmtSize } from '$lib/format.js';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import VisionPrompt from './VisionPrompt.svelte';
  import SubtitleStyleModal from './SubtitleStyleModal.svelte';

  async function copy(e, text) {
    const b = e.currentTarget;
    const ok = await copyText(text);
    const prev = b.textContent;
    b.textContent = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => (b.textContent = prev), 1200);
  }

  // onitemchange(id|null) — reports which item is showing as the viewer navigates
  // (arrows, deletes, prop updates). Lets a launcher that stays visible above the
  // Lightbox (the Montage-queue triage panel) highlight the row being viewed.
  let { list = [], index = 0, autoAdvance = false, autoSlideshow = false, title = '', onclose = () => {}, onopenrelated = () => {}, onopencollection = () => {}, onitemchange = () => {} } = $props();
  // Honour reduced-motion for the slide crossfade (and skip it entirely there).
  const reduceMotion = typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const FADE_MS = 360;
  let i = $state(index);
  $effect(() => { i = index; });

  // Deleted items drop out of the viewer live; navigate the filtered list. After a
  // delete the current index naturally lands on the next clip (or the previous one
  // at the end); close when nothing's left.
  const liveList = $derived(list.filter((x) => !$deleted.has(x.id)));
  $effect(() => {
    if (liveList.length === 0) onclose();
    else if (i > liveList.length - 1) i = liveList.length - 1;
  });
  const item = $derived(liveList[i] || null);
  $effect(() => { onitemchange(item?.id ?? null); });
  let confirmingDelete = $state(false);
  let autoplayVideos = $state(false);
  // Photo slideshow mode (image-only). The ▶ control drives this when the open item is
  // an image; videos keep using autoplayVideos. The two are mutually exclusive.
  let slideshow = $state(false);
  $effect(() => {
    list;
    index;
    autoplayVideos = autoAdvance;
    slideshow = autoSlideshow;
  });
  const nextPlayableVideo = $derived(liveList.findIndex((it, idx) => idx >= i && it.media_type === 'video'));
  const hasPlayableVideo = $derived(nextPlayableVideo !== -1);
  const isImage = $derived(item?.media_type === 'image');
  // Next IMAGE strictly after the current position (slideshow skips videos, stops at the end).
  const nextImageIdx = $derived(liveList.findIndex((it, idx) => idx > i && it.media_type === 'image'));
  const hasMoreImages = $derived(nextImageIdx !== -1);
  const playing = $derived(autoplayVideos || slideshow);
  function toggleSlideshow() {
    slideshow = !slideshow;
    if (slideshow) autoplayVideos = false;
  }
  // Advance the slideshow one photo per interval, skipping videos. Pauses while any
  // panel/dialog is open and self-clears on every change (step, mode off, unmount).
  $effect(() => {
    if (!slideshow) return;
    // A panel/dialog open is a PAUSE — keep the slideshow armed and resume on close.
    if (showInfo || showVision || showSubStyle || confirmingDelete) return;
    const it = item;
    // The auto-advance never lands on a video; the only way here is manual arrow nav.
    // Treat that as ending the photo slideshow so the ▶/pace controls don't desync.
    if (!it || it.media_type !== 'image') { slideshow = false; return; }
    const target = nextImageIdx;                       // next photo, or -1 at the end
    // Warm the next photo so the crossfade dissolves into a decoded image, not a blank frame.
    if (target !== -1 && typeof Image !== 'undefined') { const pre = new Image(); pre.src = liveList[target].href; }
    const ms = Math.max(1, Number($slideSeconds) || 5) * 1000;
    const t = setTimeout(() => {
      if (target === -1) slideshow = false;            // reached the last photo → stop
      else i = target;
    }, ms);
    return () => clearTimeout(t);
  });
  function doDelete() {
    confirmingDelete = false;
    if (item) removeMedia([item.id]);
  }
  let videoEl = $state(null);
  // Keep the global ::cue rule sized to the video's actual rendered height, so captions
  // look the same on desktop and phone (see subtitleCueRule). Re-measures on open, window
  // resize, and orientation/fullscreen changes; resets to 0 (→ %-based fallback) on close.
  $effect(() => {
    const el = videoEl;
    if (!el || typeof ResizeObserver === 'undefined') return;
    // Only publish a real height — skipping 0 avoids a 1–2 frame flash of %-based sizing
    // before layout settles (ResizeObserver fires again with the measured height).
    const measure = () => { const h = Math.round(el.clientHeight || 0); if (h > 0) captionVideoHeight.set(h); };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => { ro.disconnect(); captionVideoHeight.set(0); };
  });
  let stageEl = $state(null);
  let showInfo = $state(false);
  let showVision = $state(false);
  let showSubStyle = $state(false);
  let relatedFor = $state('');
  let related = $state({ base: null, generated: [] });
  let relatedLoading = $state(false);
  // The bottom counter (title · "3 / 47") sits exactly where iOS draws captions
  // and its native control bar, so leaving it up permanently covers both. Reveal
  // it on each clip change and on pointer activity, then fade it out while the
  // clip plays so it stops obscuring the media and subtitles.
  let counterVisible = $state(true);
  let counterTimer;
  function pokeCounter() {
    counterVisible = true;
    clearTimeout(counterTimer);
    counterTimer = setTimeout(() => { counterVisible = false; }, 2500);
  }
  // Reading `item` makes this re-run whenever the clip changes (open, step,
  // auto-advance, or a delete shifting the index).
  $effect(() => { if (item) pokeCounter(); });
  // iOS overlays its native control bar for a few seconds whenever a clip starts,
  // covering the autoplaying video. On touch devices we start with controls off so
  // the clean clip shows, then reveal native controls on the first tap (same tap
  // that unmutes). Desktop keeps controls visible from the start.
  const coarsePointer = typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches;
  let showControls = $state(!coarsePointer);
  // iOS blocks autoplay of clips with sound unless the play is tied to a tap on
  // the player. So we start muted (which iOS *will* autoplay, controls auto-hide),
  // and the first tap anywhere unmutes — and the preference sticks across clips.
  let wantSound = $state(!coarsePointer && desktopSoundEnabled);
  // This element's one-time tap into the shared audio graph. createMediaElementSource
  // is once-per-element, so we guard and create it on the first unmute.
  let mediaSourceNode = null;
  function routeAudio() {
    const ctx = audioContext();
    if (!ctx || !videoEl || mediaSourceNode) return;
    try {
      mediaSourceNode = ctx.createMediaElementSource(videoEl);
      mediaSourceNode.connect(ctx.destination);
    } catch {
      mediaSourceNode = null;  // fall back to the element's own output
    }
  }
  function enableSound() {
    showControls = true;
    pokeCounter();
    if (wantSound) return;
    wantSound = true;
    if (!coarsePointer) desktopSoundEnabled = true;
    if (videoEl) {
      // Unlock + route audio through the shared AudioContext while we're inside
      // this tap (the gesture), then unmute. From here every clip is audible via
      // the graph — no per-clip gesture, immune to the autoplay re-gating.
      routeAudio();
      audioContext()?.resume?.().catch(() => {});
      videoEl.defaultMuted = false;
      videoEl.muted = false;
      videoEl.play?.().catch(() => {});
    }
  }
  // Desktop shows the native control bar from the start, so users unmute with its
  // volume/mute control — which sets `videoEl.muted = false` directly (it lives in
  // the video's UA shadow DOM) and never runs enableSound. `wantSound` therefore
  // stayed false and the advance $effect re-muted every subsequent clip: audio
  // read as unmuted at full volume but was silent until the slider was nudged
  // again (most visible on Firefox). Mirror any user-driven unmute back into
  // wantSound so the sound preference sticks across clips. Preserve volume on
  // desktop too; iOS/touch stays on the existing muted-first unlock path.
  function onVolumeChange() {
    if (!videoEl) return;
    // Persist the chosen LEVEL per device (desktop only — touch stays on muted-first
    // autoplay and never exposes a level slider pre-tap). Saving the level can't trigger
    // an unmuted autoplay, so iOS is unaffected.
    if (!coarsePointer) { desktopVolume = videoEl.volume; saveVolume(videoEl.volume); }
    if (!videoEl.muted) {
      wantSound = true;
      if (!coarsePointer) desktopSoundEnabled = true;
    }
  }
  onDestroy(() => {
    clearTimeout(counterTimer);
    try { mediaSourceNode?.disconnect(); } catch {}
    mediaSourceNode = null;  // the shared AudioContext stays alive for reuse
  });

  // Largest box that fits within 94vw x the lightbox media height while preserving the media's aspect
  // (upscaling allowed, so portrait 9:16 clips fill the height instead of showing
  // at their native pixel size). Falls back to max-only when dimensions are unknown.
  function fitStyle(it) {
    const maxH = 'var(--lightbox-media-max-h)';
    if (it && it.thumb_w && it.thumb_h) {
      return `width:min(94vw, calc(${maxH} * ${it.thumb_w} / ${it.thumb_h})); height:auto; max-height:${maxH}; max-width:94vw;`;
    }
    return `max-width:94vw; max-height:${maxH};`;
  }

  function step(d) {
    const n = i + d;
    if (n >= 0 && n < liveList.length) i = n;
  }
  function nextVideoIndex(fromIndex) {
    return liveList.findIndex((it, idx) => idx >= fromIndex && it.media_type === 'video');
  }
  function toggleAutoplayVideos() {
    if (autoplayVideos) {
      autoplayVideos = false;
      return;
    }
    const next = nextVideoIndex(i);
    if (next === -1) return;
    autoplayVideos = true;
    if (next !== i) i = next;
  }

  $effect(() => {
    const it = item;
    if (!showInfo || !it) return;
    const id = it.id;
    relatedFor = id;
    related = { base: null, generated: [] };
    relatedLoading = true;
    mediaRelated(id)
      .then((data) => {
        if (relatedFor !== id) return;
        related = { base: data?.base || null, generated: data?.generated || [] };
      })
      .catch(() => {
        if (relatedFor === id) related = { base: null, generated: [] };
      })
      .finally(() => {
        if (relatedFor === id) relatedLoading = false;
      });
  });

  function openRelated(relatedList, nextTitle) {
    const clean = (relatedList || []).filter(Boolean);
    if (!clean.length) return;
    showInfo = false;
    onopenrelated(clean, 0, nextTitle);
  }

  // Collections this item belongs to, for the info panel's "In" chips. Pure
  // client-side lookup against the store. Sealed (locked, not unlocked) collections
  // are served with ids: [] precisely so membership can't leak — no filtering needed.
  const memberOf = $derived(item ? ($collections || []).filter((c) => (c.ids || []).includes(item.id)) : []);
  // Tag chip: jump out of the viewer to All Media filtered to just that tag — one
  // predictable "show me more like this" destination no matter where the lightbox
  // was opened from (a play-queue over the collections landing, a canvas, etc.).
  function browseTag(tag) {
    // Clear the drilled collection BEFORE the filters change (same order as
    // setView): the page's filter effect skips loading while a collection is
    // active, and it won't refire when the id clears later — leaving stale,
    // unfiltered results if the id were still set during the update.
    activeCollectionId.set(null);
    filters.update((f) => ({ ...f, view: 'all', canvas: null, query: '', tags: [tag], models: [], resolutions: [], mediaType: 'all', period: 'all' }));
    close();
  }
  function gotoCollection(c) {
    onopencollection(c.id);
    close();
  }

  // Persistent <video>: update src in place so native/container fullscreen
  // survives advancing to the next clip.
  $effect(() => {
    const it = item;
    if (it && it.media_type === 'video' && videoEl && videoEl.getAttribute('src') !== it.href) {
      // On touch, start every clip with native controls OFF. iOS flashes its
      // control bar over the first ~3s of any `controls` video that starts playing,
      // which is disruptive on each seamless playlist advance. A tap re-reveals the
      // controls (and unmutes the first time); the sound preference itself persists.
      // Set the property imperatively too so the attribute is gone *before* play().
      if (coarsePointer) { showControls = false; videoEl.controls = false; }
      // iOS only grants muted-autoplay when the `muted` *attribute* is present —
      // the JS property alone isn't honored by Safari's autoplay gate. defaultMuted
      // reflects that content attribute, so set it (and muted) before the src loads.
      if (!coarsePointer) videoEl.volume = desktopVolume;
      videoEl.defaultMuted = !wantSound;
      videoEl.muted = !wantSound;
      videoEl.src = it.href;
      // Rebuild the caption track for this clip. The `default` attribute only
      // selects a track on the element's *first* load, so on the reused element
      // we re-create the track and force 'showing' once loaded — otherwise only
      // the first clip of a playlist shows subtitles.
      videoEl.querySelectorAll('track').forEach((t) => t.remove());
      if (it.subtitles) {
        const tr = document.createElement('track');
        tr.kind = 'subtitles';
        tr.label = 'English';
        tr.srclang = 'en';
        tr.src = it.subtitles;
        tr.default = true;
        videoEl.appendChild(tr);
        const show = () => {
          try { for (let k = 0; k < videoEl.textTracks.length; k++) videoEl.textTracks[k].mode = 'showing'; } catch {}
        };
        tr.addEventListener('load', show, { once: true });
        videoEl.addEventListener('loadeddata', show, { once: true });
      }
      videoEl.play?.().catch(() => {});
    }
  });

  function onended() {
    if (!autoplayVideos) return;
    const next = nextVideoIndex(i + 1);
    if (next !== -1) i = next;
  }
  // iPhone Safari doesn't implement the Fullscreen API on elements (only iPad /
  // desktop / Android do), so element fullscreen silently fails there. Detect it
  // up front to hide the dead button for images, and fall back to the native video
  // player's webkitEnterFullscreen() — the one thing iPhone *can* fullscreen.
  const elementFsSupported = typeof document !== 'undefined' && !!document.documentElement.requestFullscreen;
  function toggleFs() {
    if (document.fullscreenElement) { document.exitFullscreen?.(); return; }
    if (stageEl?.requestFullscreen) { stageEl.requestFullscreen().catch(() => {}); return; }
    if (videoEl?.webkitEnterFullscreen) videoEl.webkitEnterFullscreen();
  }
  function close() {
    // Pause before unmount so we never tear down a mid-play element — a clean stop,
    // the same end-state as letting the clip finish (the path that doesn't wedge the
    // shared audio graph). Both the Close button and a void-click route through here.
    try { videoEl?.pause?.(); } catch {}
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    onclose();
  }
  function onkey(e) {
    // The subtitle-style dialog owns the keyboard while open (it handles its own
    // Escape); don't let arrows/Escape also drive the player underneath.
    if (showSubStyle) return;
    if (e.key === 'Escape') { if (document.fullscreenElement) return; if (showVision) { showVision = false; return; } if (showInfo) { showInfo = false; return; } close(); }
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === ' ' || e.code === 'Space') {
      // Space = play/pause. Skip when a control/field is focused (let it activate
      // natively) or the vision composer is open. On an image it toggles the photo
      // slideshow; on a video it pauses/resumes the clip.
      const t = e.target;
      const interactive = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'BUTTON' || t.tagName === 'A' || t.tagName === 'SELECT' || t.tagName === 'VIDEO' || t.isContentEditable);
      if (interactive || showVision) return;
      e.preventDefault();
      if (isImage) { if (slideshow || hasMoreImages) toggleSlideshow(); }
      else if (videoEl) { videoEl.paused ? videoEl.play?.().catch(() => {}) : videoEl.pause?.(); }
    }
    else if (e.key === 'f' || e.key === 'F') toggleFs();
    else if (e.key === 'i' || e.key === 'I') showInfo = !showInfo;
    // h = hide/show the top action cluster, the keyboard twin of the collapse orb.
    else if (e.key === 'h' || e.key === 'H') { lightboxChrome.update((v) => !v); pokeCounter(); }
    else if (e.key === 'Delete') confirmingDelete = true;
  }
</script>

<svelte:window onkeydown={onkey} />

{#if item}
  <div class="lightbox fixed inset-0 z-50 bg-[var(--lightbox-bg)] backdrop-blur-sm" role="dialog" aria-modal="true" tabindex="-1" use:trapFocus>
    <!-- Media fills the whole viewport; nothing overlaps it unless Info is opened. -->
    <div bind:this={stageEl} class="lightbox-stage absolute inset-0 grid place-items-center p-2 sm:p-4" role="presentation"
         onpointerdown={(e) => { if (e.target !== e.currentTarget) enableSound(); }}
         onpointermove={pokeCounter}
         onclick={(e) => { if (e.target === e.currentTarget) close(); }}>
      {#if item.media_type === 'video'}
        <!-- caption track is (re)built imperatively in the $effect so playlist clips keep subtitles -->
        <!-- No `muted={...}` attribute on purpose: Svelte now compiles it to a
             reactive effect that re-asserts `video.muted` on its own schedule,
             fighting the imperative muted/autoplay handshake below. We own `muted`
             entirely via the $effect (muted autoplay pre-unlock) + enableSound. -->
        <video bind:this={videoEl} controls={showControls} autoplay playsinline onended={onended} onvolumechange={onVolumeChange}
               style={fitStyle(item)} class="lightbox-media rounded-lg bg-[var(--media-bg)]"></video>
      {:else}
        <!-- Keyed by href so each photo is its own element: the outgoing + incoming
             images share grid cell 1/1 (see CSS) and crossfade. Honours reduced-motion. -->
        {#key item.href}
          <img src={item.href} alt="" decoding="async" style={fitStyle(item)} class="lightbox-media rounded-lg"
               transition:fade={{ duration: reduceMotion ? 0 : FADE_MS }} />
        {/key}
      {/if}
    </div>

    <!-- Top scrim: grounds the floating chrome so it stays legible over bright
         frames, and visually separates controls from the media on full-bleed clips.
         It exists FOR that chrome, so it collapses with it — a 7rem veil over the top
         of the frame is most of what you're reclaiming in landscape. The orb and Close
         that stay behind carry their own .glass background, so they're legible without it. -->
    {#if $lightboxChrome}
      <div class="pointer-events-none absolute inset-x-0 top-0 z-[5] h-28 bg-gradient-to-b from-[var(--lightbox-scrim-start)] via-[var(--lightbox-scrim-mid)] to-transparent"
           transition:fade={{ duration: reduceMotion ? 0 : 140 }}></div>
    {/if}

    <!-- Top chrome (safe-area inset so it clears notches / Dynamic Island).
         Collapsible: the action cluster hides behind the orb, leaving orb + Close.
         Close deliberately stays out of the collapse — iOS has no Escape key, and a
         full-bleed landscape clip leaves no backdrop to tap, so hiding it would strand
         you with no visible exit. The orb and Close are the right-anchored pair, so the
         cluster grows/shrinks leftward from a fixed point instead of shifting them. -->
    <div class="absolute z-10 flex gap-2" style="top: max(0.75rem, env(safe-area-inset-top)); right: max(0.75rem, env(safe-area-inset-right));">
      {#if $lightboxChrome}
      <div id="lightbox-actions" class="flex gap-2" transition:fade={{ duration: reduceMotion ? 0 : 140 }}>
        <button class="glass grid h-10 w-10 place-items-center rounded-lg text-lg {$favorites.has(item.id) ? 'text-[var(--favorite)]' : ''}"
          title="Favorite" aria-label={$favorites.has(item.id) ? 'Unfavorite' : 'Favorite'} aria-pressed={$favorites.has(item.id)} onclick={() => toggleFavorite(item.id)}>{$favorites.has(item.id) ? '♥' : '♡'}</button>
        {#if item.media_type !== 'video'}
          <button class="glass grid h-10 w-10 place-items-center rounded-lg {showVision ? 'text-[var(--accent)]' : ''}"
            title="Describe for Grok (AI)" aria-label="Describe image for Grok" aria-pressed={showVision}
            onclick={() => { showVision = !showVision; if (showVision) showInfo = false; }}>
            <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg>
          </button>
          <button class="glass grid h-10 w-10 place-items-center rounded-lg"
            title="Use as source for Grok Imagine" aria-label="Use as Imagine source"
            onclick={() => { sendToImagine(item); close(); }}>
            <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4V2"/><path d="M15 16v-2"/><path d="M8 9h2"/><path d="M20 9h2"/><path d="M17.8 11.8 19 13"/><path d="M15 9h.01"/><path d="M17.8 6.2 19 5"/><path d="m3 21 9-9"/><path d="M12.2 6.2 11 5"/></svg>
          </button>
          <!-- Add this IMAGE to the Montage queue as a Picture & Video beat (stays open so you
               can keep browsing); adding it switches the montage into picture-video mode. -->
          <button class="glass grid h-10 w-10 place-items-center rounded-lg {basketMembers.has(item.id) ? 'text-[var(--accent)]' : ''}"
            title={basketMembers.has(item.id) ? 'In montage queue — click to remove' : 'Add photo to montage queue (Picture & Video)'}
            aria-label={basketMembers.has(item.id) ? 'Remove from montage queue' : 'Add photo to montage queue'} aria-pressed={basketMembers.has(item.id)}
            onclick={() => queueImageForMontage(item.id)}>
            <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          </button>
        {:else if item.model !== 'Beat Montage'}
          <!-- Add this video to the cross-library Montage queue. Stays open (no close())
               so you can keep browsing and queueing across collections. -->
          <button class="glass grid h-10 w-10 place-items-center rounded-lg {basketMembers.has(item.id) ? 'text-[var(--accent)]' : ''}"
            title={basketMembers.has(item.id) ? 'In montage queue — click to remove' : 'Add to montage queue'}
            aria-label={basketMembers.has(item.id) ? 'Remove from montage queue' : 'Add to montage queue'} aria-pressed={basketMembers.has(item.id)}
            onclick={() => toggleBasket(item.id)}>
            <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          </button>
        {/if}
        {#if item.media_type === 'video' && item.subtitles}
          <button class="glass grid h-10 w-10 place-items-center rounded-lg text-xs font-black tracking-tight {showSubStyle ? 'text-[var(--accent)]' : ''}"
            title="Subtitle display" aria-label="Subtitle display" aria-pressed={showSubStyle} onclick={() => (showSubStyle = true)}>CC</button>
        {/if}
        <button class="glass grid h-10 w-10 place-items-center rounded-lg text-lg {showInfo ? 'text-[var(--accent)]' : ''}"
          title="Info (i)" aria-label="Info" aria-pressed={showInfo} onclick={() => { showInfo = !showInfo; if (showInfo) showVision = false; }}>ⓘ</button>
        <button class="glass grid h-10 w-10 place-items-center rounded-lg text-sm font-bold {playing ? 'text-[var(--accent)]' : ''}"
          title={isImage ? (slideshow ? 'Stop slideshow' : 'Start photo slideshow') : (autoplayVideos ? 'Stop autoplay videos' : 'Autoplay videos from here')}
          aria-label={isImage ? (slideshow ? 'Stop slideshow' : 'Start photo slideshow') : (autoplayVideos ? 'Stop autoplay videos' : 'Autoplay videos from here')}
          aria-pressed={playing}
          disabled={isImage ? (!slideshow && !hasMoreImages) : (!autoplayVideos && !hasPlayableVideo)}
          onclick={isImage ? toggleSlideshow : toggleAutoplayVideos}>▶</button>
        <button class="glass grid h-10 w-10 place-items-center rounded-lg transition hover:text-[var(--danger-ink)]"
          title="Delete (Del)" aria-label="Delete" onclick={() => (confirmingDelete = true)}>
          <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
        </button>
      </div>
      {/if}
      <!-- Collapse orb. While collapsed it rides the same idle signal as the bottom
           counter (pokeCounter — clip change, pointer activity, first tap) and dims to
           40% rather than vanishing, so the frame is near-clean but the way back is
           never hidden. Hover/focus always restores it. -->
      <button class="glass grid h-10 w-10 place-items-center rounded-lg transition-opacity duration-300 hover:opacity-100 focus-visible:opacity-100 {$lightboxChrome || counterVisible ? 'opacity-100' : 'opacity-40'}"
        title={$lightboxChrome ? 'Hide controls (h)' : 'Show controls (h)'}
        aria-label={$lightboxChrome ? 'Hide controls' : 'Show controls'}
        aria-expanded={$lightboxChrome} aria-controls="lightbox-actions"
        onclick={() => { lightboxChrome.update((v) => !v); pokeCounter(); }}>
        {#if $lightboxChrome}
          <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 17 5-5-5-5"/><path d="m13 17 5-5-5-5"/></svg>
        {:else}
          <svg viewBox="0 0 24 24" class="h-5 w-5" fill="currentColor" stroke="none" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>
        {/if}
      </button>
      <button class="glass rounded-lg px-3 py-2 font-bold transition-opacity duration-300 hover:opacity-100 focus-visible:opacity-100 {$lightboxChrome || counterVisible ? 'opacity-100' : 'opacity-40'}" onclick={close}>Close</button>
    </div>
    {#if elementFsSupported || item.media_type === 'video'}
      <button class="glass absolute z-10 grid h-10 w-10 place-items-center rounded-lg" style="top: max(0.75rem, env(safe-area-inset-top)); left: max(0.75rem, env(safe-area-inset-left));" title="Fullscreen (f)" aria-label="Fullscreen" onclick={toggleFs}>⛶</button>
    {/if}

    <!-- Side nav -->
    {#if i > 0}
      <button class="glass absolute left-3 top-1/2 z-10 grid h-12 w-12 -translate-y-1/2 place-items-center rounded-full text-2xl" aria-label="Previous (←)" title="Previous (←)" onclick={() => step(-1)}>‹</button>
    {/if}
    {#if i < liveList.length - 1}
      <button class="glass absolute right-3 top-1/2 z-10 grid h-12 w-12 -translate-y-1/2 place-items-center rounded-full text-2xl" aria-label="Next (→)" title="Next (→)" onclick={() => step(1)}>›</button>
    {/if}

    <!-- Counter (small, unobtrusive; safe-area inset so it clears the home indicator).
         pointer-events-none so it never shadows a tap on the stage below; fades out
         a couple seconds after each clip starts so it clears captions and the frame. -->
    <div class="lightbox-counter glass pointer-events-none absolute left-1/2 z-10 -translate-x-1/2 rounded-full px-3 py-1 text-xs text-muted transition-opacity duration-300 {counterVisible ? 'opacity-100' : 'opacity-0'}" style="bottom: max(0.75rem, env(safe-area-inset-bottom));">
      {[title, `${i + 1} / ${liveList.length}`].filter(Boolean).join('  ·  ')}
    </div>

    <!-- Slideshow pace control (shown only while a slideshow runs). A −/＋ stepper, not a
         number input, so iOS never pops the on-screen keyboard inside the fullscreen viewer.
         Persisted via slideSeconds so the chosen pace sticks across sessions. -->
    {#if slideshow && isImage}
      <div class="slide-speed glass absolute left-1/2 z-10 flex -translate-x-1/2 items-center gap-1 rounded-full p-1"
           style="bottom: calc(max(0.75rem, env(safe-area-inset-bottom)) + 2.75rem);">
        <button class="grid h-8 w-8 place-items-center rounded-full text-lg font-bold leading-none disabled:opacity-40"
          aria-label="Less time per photo" title="Less time per photo"
          disabled={$slideSeconds <= 1} onclick={() => setSlideSeconds($slideSeconds - 1)}>−</button>
        <span class="min-w-[4.5rem] text-center text-xs font-bold tabular-nums">{$slideSeconds}s / photo</span>
        <button class="grid h-8 w-8 place-items-center rounded-full text-lg font-bold leading-none disabled:opacity-40"
          aria-label="More time per photo" title="More time per photo"
          disabled={$slideSeconds >= 30} onclick={() => setSlideSeconds($slideSeconds + 1)}>＋</button>
      </div>
    {/if}

    <!-- Info panel: hidden by default, slides up over the bottom when opened -->
    {#if showInfo}
      <div class="panel absolute inset-x-0 bottom-0 z-20 max-h-[50dvh] overflow-auto px-6 py-4"
           transition:fly={{ y: 240, duration: 180 }}>
        <div class="mb-2 flex items-start gap-3">
          <p class="flex-1 leading-relaxed">{item.prompt || 'Untitled prompt'}</p>
          <button class="shrink-0 rounded-sm border border-line px-2 py-0.5 text-xs" onclick={() => (showInfo = false)}>Hide</button>
        </div>
        <p class="mb-3 text-sm text-muted">
          {[
            item.media_type,
            item.media_w && item.media_h ? `${item.media_w}×${item.media_h}` : null,
            fmtSize(item.size_bytes),
            item.model,
            item.preset ? (STYLE_LABELS[item.preset] || item.preset) : null,
            (item.created_at || '').slice(0, 10),
            (item.href || '').split('/').pop()
          ].filter(Boolean).join('  ·  ')}
        </p>
        {#if item.api_generated}
          <div class="mb-3 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-bold"
               style="background: color-mix(in srgb, var(--accent-2) 18%, transparent); color: var(--accent-2);">
            <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="currentColor" aria-hidden="true"><path d="M12 3l1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6z"/></svg>
            <span>Generated with Grok Imagine</span>
          </div>
        {/if}
        {#if item.tags?.length}
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <span class="text-xs font-bold uppercase tracking-wide text-muted">Tags</span>
            {#each item.tags as tag (tag)}
              <button type="button" class="rounded-full border border-line px-3 py-1 text-xs font-semibold transition hover:border-[var(--accent)]"
                title={`Show all media tagged “${tag}”`} onclick={() => browseTag(tag)}>{tag}</button>
            {/each}
          </div>
        {/if}
        {#if memberOf.length}
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <span class="text-xs font-bold uppercase tracking-wide text-muted">In</span>
            {#each memberOf as c (c.id)}
              <button type="button" class="rounded-full border border-line px-3 py-1 text-xs font-semibold transition hover:border-[var(--accent)]"
                title={`Open collection “${c.name}”`} onclick={() => gotoCollection(c)}>{c.name}</button>
            {/each}
          </div>
        {/if}
        {#if related.base || related.generated.length}
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <span class="text-xs font-bold uppercase tracking-wide text-muted">Related</span>
            {#if related.base}
              <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold hover:border-[var(--accent)]"
                title="Open the base image this video was generated from"
                onclick={() => openRelated([related.base], 'Base image')}>Base image</button>
            {/if}
            {#if related.generated.length}
              <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold hover:border-[var(--accent)]"
                title="Open videos generated from this image"
                onclick={() => openRelated(related.generated, `${related.generated.length} generated video${related.generated.length === 1 ? '' : 's'}`)}>
                {related.generated.length} generated video{related.generated.length === 1 ? '' : 's'}
              </button>
            {/if}
          </div>
        {:else if relatedLoading}
          <p class="mb-3 text-xs text-muted">Checking related media…</p>
        {/if}
        <div class="flex flex-wrap gap-2">
          <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold {$favorites.has(item.id) ? 'text-[var(--favorite)]' : ''}"
            onclick={() => toggleFavorite(item.id)}>{$favorites.has(item.id) ? '♥ Favorited' : '♡ Favorite'}</button>
          <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold"
            onclick={(e) => copy(e, item.prompt)}>Copy prompt</button>
          {#if item.media_type !== 'video'}
            <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold hover:border-[var(--accent)]"
              onclick={() => { sendToImagine(item); close(); }}>Use as Imagine source</button>
          {/if}
          <a class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" href={item.href} target="_blank" rel="noreferrer">Open original</a>
        </div>
      </div>
    {/if}

    <!-- Describe-for-Grok overlay: image-only, generates a Grok Imagine prompt from the
         image + its stored prompt via a vision model. Sits above the Info panel. -->
    {#if showVision && item.media_type !== 'video'}
      <VisionPrompt {item} onclose={() => (showVision = false)} />
    {/if}

    {#if confirmingDelete}
      <ConfirmDialog title="Delete this item?"
        message="The file is permanently removed from disk and won't be re-downloaded on future syncs."
        confirmLabel="Delete" onconfirm={doDelete} oncancel={() => (confirmingDelete = false)} />
    {/if}

    {#if showSubStyle}
      <SubtitleStyleModal onclose={() => (showSubStyle = false)} />
    {/if}
  </div>
{/if}

<style>
  .lightbox {
    --lightbox-media-max-h: 92dvh;
  }

  .lightbox-media {
    object-fit: contain;
    /* Pin the media to a single grid cell so that, during a slideshow crossfade, the
       outgoing and incoming <img> stack and overlap (centred by the stage's
       place-items-center) instead of auto-flowing into two stacked rows. */
    grid-area: 1 / 1;
  }

  @media (orientation: landscape) and (max-height: 520px) {
    .lightbox {
      --lightbox-counter-space: calc(3.6rem + env(safe-area-inset-bottom));
      --lightbox-media-max-h: calc(100dvh - var(--lightbox-counter-space));
    }

    .lightbox-stage {
      padding-bottom: var(--lightbox-counter-space);
    }

    .lightbox-counter {
      bottom: max(0.35rem, env(safe-area-inset-bottom)) !important;
    }
  }
</style>
