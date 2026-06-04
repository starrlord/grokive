<script>
  import { fly } from 'svelte/transition';
  import { favorites, toggleFavorite, removeMedia, deleted } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import ConfirmDialog from './ConfirmDialog.svelte';

  async function copy(e, text) {
    const b = e.currentTarget;
    const ok = await copyText(text);
    const prev = b.textContent;
    b.textContent = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => (b.textContent = prev), 1200);
  }

  // Human file size: "812 KB", "3.8 MB", "2 GB" (drops a trailing .0).
  function fmtSize(b) {
    if (b == null || b === '') return '';
    if (b < 1024) return `${b} B`;
    if (b < 1024 ** 2) return `${Math.round(b / 1024)} KB`;
    if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
    const gb = b / 1024 ** 3;
    return `${gb.toFixed(gb < 10 ? 1 : 0).replace(/\.0$/, '')} GB`;
  }

  let { list = [], index = 0, autoAdvance = false, title = '', onclose = () => {} } = $props();
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
  let confirmingDelete = $state(false);
  function doDelete() {
    confirmingDelete = false;
    if (item) removeMedia([item.id]);
  }
  let videoEl = $state(null);
  let stageEl = $state(null);
  let showInfo = $state(false);
  // iOS overlays its native control bar for a few seconds whenever a clip starts,
  // covering the autoplaying video. On touch devices we start with controls off so
  // the clean clip shows, then reveal native controls on the first tap (same tap
  // that unmutes). Desktop keeps controls visible from the start.
  const coarsePointer = typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches;
  let showControls = $state(!coarsePointer);
  // iOS blocks autoplay of clips with sound unless the play is tied to a tap on
  // the player. So we start muted (which iOS *will* autoplay, controls auto-hide),
  // and the first tap anywhere unmutes — and the preference sticks across clips.
  let wantSound = $state(false);
  function enableSound() {
    showControls = true;
    if (wantSound) return;
    wantSound = true;
    if (videoEl) {
      // Clear the `muted` *attribute* too (via defaultMuted) so the next clip
      // doesn't re-mute itself on load.
      videoEl.defaultMuted = false;
      videoEl.muted = false;
      videoEl.play?.().catch(() => {});
    }
  }

  // Largest box that fits within 94vw x 92dvh while preserving the media's aspect
  // (upscaling allowed, so portrait 9:16 clips fill the height instead of showing
  // at their native pixel size). Falls back to max-only when dimensions are unknown.
  function fitStyle(it) {
    if (it && it.thumb_w && it.thumb_h) {
      return `width:min(94vw, calc(92dvh * ${it.thumb_w} / ${it.thumb_h})); height:auto; max-height:92dvh; max-width:94vw;`;
    }
    return 'max-width:94vw; max-height:92dvh;';
  }

  function step(d) {
    const n = i + d;
    if (n >= 0 && n < liveList.length) i = n;
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
    if (autoAdvance && i < liveList.length - 1) step(1);
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
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    onclose();
  }
  function onkey(e) {
    if (e.key === 'Escape') { if (document.fullscreenElement) return; if (showInfo) { showInfo = false; return; } close(); }
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'f' || e.key === 'F') toggleFs();
    else if (e.key === 'i' || e.key === 'I') showInfo = !showInfo;
    else if (e.key === 'Delete') confirmingDelete = true;
  }
</script>

<svelte:window on:keydown={onkey} />

{#if item}
  <div class="fixed inset-0 z-50 bg-[var(--lightbox-bg)] backdrop-blur-sm" role="dialog" aria-modal="true">
    <!-- Media fills the whole viewport; nothing overlaps it unless Info is opened. -->
    <div bind:this={stageEl} class="absolute inset-0 grid place-items-center p-2 sm:p-4" role="presentation"
         onpointerdown={enableSound}
         onclick={(e) => { if (e.target === e.currentTarget) close(); }}>
      {#if item.media_type === 'video'}
        <!-- caption track is (re)built imperatively in the $effect so playlist clips keep subtitles -->
        <video bind:this={videoEl} controls={showControls} autoplay playsinline muted={!wantSound} onended={onended}
               style={fitStyle(item)} class="rounded-lg bg-[var(--media-bg)]"></video>
      {:else}
        <img src={item.href} alt="" style={fitStyle(item)} class="rounded-lg" />
      {/if}
    </div>

    <!-- Top scrim: grounds the floating chrome so it stays legible over bright
         frames, and visually separates controls from the media on full-bleed clips. -->
    <div class="pointer-events-none absolute inset-x-0 top-0 z-[5] h-28 bg-gradient-to-b from-[var(--lightbox-scrim-start)] via-[var(--lightbox-scrim-mid)] to-transparent"></div>

    <!-- Top chrome (safe-area inset so it clears notches / Dynamic Island) -->
    <div class="absolute z-10 flex gap-2" style="top: max(0.75rem, env(safe-area-inset-top)); right: max(0.75rem, env(safe-area-inset-right));">
      <button class="glass grid h-10 w-10 place-items-center rounded-lg text-lg {$favorites.has(item.id) ? 'text-[var(--favorite)]' : ''}"
        title="Favorite" onclick={() => toggleFavorite(item.id)}>{$favorites.has(item.id) ? '♥' : '♡'}</button>
      <button class="glass grid h-10 w-10 place-items-center rounded-lg text-lg {showInfo ? 'text-[var(--accent)]' : ''}"
        title="Info (i)" onclick={() => (showInfo = !showInfo)}>ⓘ</button>
      <button class="glass grid h-10 w-10 place-items-center rounded-lg transition hover:text-[var(--danger-ink)]"
        title="Delete (Del)" aria-label="Delete" onclick={() => (confirmingDelete = true)}>
        <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/><path d="M10 11v6M14 11v6"/></svg>
      </button>
      <button class="glass rounded-lg px-3 py-2 font-bold" onclick={close}>Close</button>
    </div>
    {#if elementFsSupported || item.media_type === 'video'}
      <button class="glass absolute z-10 grid h-10 w-10 place-items-center rounded-lg" style="top: max(0.75rem, env(safe-area-inset-top)); left: max(0.75rem, env(safe-area-inset-left));" title="Fullscreen (f)" onclick={toggleFs}>⛶</button>
    {/if}

    <!-- Side nav -->
    {#if i > 0}
      <button class="glass absolute left-3 top-1/2 z-10 grid h-12 w-12 -translate-y-1/2 place-items-center rounded-full text-2xl" onclick={() => step(-1)}>‹</button>
    {/if}
    {#if i < liveList.length - 1}
      <button class="glass absolute right-3 top-1/2 z-10 grid h-12 w-12 -translate-y-1/2 place-items-center rounded-full text-2xl" onclick={() => step(1)}>›</button>
    {/if}

    <!-- Counter (small, unobtrusive; safe-area inset so it clears the home indicator) -->
    <div class="glass absolute left-1/2 z-10 -translate-x-1/2 rounded-full px-3 py-1 text-xs text-muted" style="bottom: max(0.75rem, env(safe-area-inset-bottom));">
      {[title, `${i + 1} / ${liveList.length}`].filter(Boolean).join('  ·  ')}
    </div>

    <!-- Info panel: hidden by default, slides up over the bottom when opened -->
    {#if showInfo}
      <div class="panel absolute inset-x-0 bottom-0 z-20 max-h-[50vh] overflow-auto px-6 py-4"
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
            (item.created_at || '').slice(0, 10),
            (item.href || '').split('/').pop()
          ].filter(Boolean).join('  ·  ')}
        </p>
        <div class="flex flex-wrap gap-2">
          <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold {$favorites.has(item.id) ? 'text-[var(--favorite)]' : ''}"
            onclick={() => toggleFavorite(item.id)}>{$favorites.has(item.id) ? '♥ Favorited' : '♡ Favorite'}</button>
          <button type="button" class="rounded-lg border border-line px-3 py-2 text-sm font-semibold"
            onclick={(e) => copy(e, item.prompt)}>Copy prompt</button>
          <a class="rounded-lg border border-line px-3 py-2 text-sm font-semibold" href={item.href} target="_blank" rel="noreferrer">Open original</a>
        </div>
      </div>
    {/if}

    {#if confirmingDelete}
      <ConfirmDialog title="Delete this item?"
        message="The file is permanently removed from disk and won't be re-downloaded on future syncs."
        confirmLabel="Delete" onconfirm={doDelete} oncancel={() => (confirmingDelete = false)} />
    {/if}
  </div>
{/if}
