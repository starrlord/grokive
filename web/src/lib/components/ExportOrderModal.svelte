<script>
  // Explicit "arrange before you merge" step. Videos arrive here in whatever order the
  // grid/selection produced (newest-first by default, and in the grouped view not even
  // in the on-screen family order) — so the merge used to come out back-to-front. Here
  // the user drags / ▲▼ them into the exact sequence the combined MP4 should play, then
  // exports. This is the ONLY place the merge order is chosen deliberately; the backend
  // concatenates ids in the order it's handed, so top-to-bottom here == playback order.
  //
  // It also owns the "Cinematic intro" option: a server-rendered trailer-style opener
  // (a title card in one of four styles over clips sampled from the set) prepended to
  // the merge. A toggle here — rather than a separate "Create intro?" dialog — because
  // every multi-video merge already passes through this modal; off = the export of old.
  import { exportSelection } from '$lib/api.js';
  import { loadIntroPrefs, saveIntroPrefs, shuffled } from '$lib/state.js';
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

  // --- Cinematic intro -------------------------------------------------------
  // Style presets: title fill + stroke + subtitle; the border reuses the title color.
  const PRESETS = [
    { id: 'gold',    label: 'Gold',    title: '#D4AF37', stroke: '#8B6914', subtitle: '#E8D5A3' },
    { id: 'silver',  label: 'Silver',  title: '#C9CCD1', stroke: '#585E66', subtitle: '#E8EAED' },
    { id: 'crimson', label: 'Crimson', title: '#E0364B', stroke: '#6E0F1C', subtitle: '#F2B8C0' },
    { id: 'ocean',   label: 'Ocean',   title: '#38BDF8', stroke: '#0C4A6E', subtitle: '#BAE6FD' },
    { id: 'emerald', label: 'Emerald', title: '#34D399', stroke: '#065F46', subtitle: '#A7F3D0' },
    { id: 'violet',  label: 'Violet',  title: '#A78BFA', stroke: '#3B1D77', subtitle: '#DDD6FE' }
  ];
  const DURATIONS = [8, 12, 16];
  // Intro styles — ids must mirror introgen.STYLES on the server (unknown ids fall
  // back to mosaic there). Descriptions double as the picker's explainer line.
  const STYLES = [
    { id: 'mosaic',  label: 'Mosaic',  desc: (n) => `A title card over a grid of ${n} clip${n === 1 ? '' : 's'} picked at random from your set.` },
    { id: 'epic',    label: 'Epic',    desc: () => 'Full-screen slow push across up to 3 clips behind widescreen bars — the movie-trailer opener.' },
    { id: 'cascade', label: 'Cascade', desc: () => 'A scrolling wall of your clips drifting at different speeds behind a frosted title band.' },
    { id: 'prism',   label: 'Prism',   desc: () => 'Your clips folded into a slowly turning mirror kaleidoscope behind bold glass typography.' }
  ];
  // Texts/style persist across exports (loadIntroPrefs); the toggle itself is always
  // off on open — adding an intro is a per-export decision.
  const prefs = loadIntroPrefs() || {};
  let introOn = $state(false);
  let introTitle = $state(typeof prefs.title === 'string' ? prefs.title : '');
  let introSubtitle = $state(typeof prefs.subtitle === 'string' ? prefs.subtitle : '');
  let introStyle = $state(STYLES.some((s) => s.id === prefs.style) ? prefs.style : 'mosaic');
  let introPreset = $state(PRESETS.some((p) => p.id === prefs.preset) || prefs.preset === 'custom' ? prefs.preset : 'gold');
  let customColor = $state(/^#[0-9a-fA-F]{6}$/.test(prefs.custom || '') ? prefs.custom : '#d4af37');
  let introDur = $state(DURATIONS.includes(prefs.duration) ? prefs.duration : 12);

  const tileCount = $derived(Math.min(9, list.length));
  const styleDesc = $derived((STYLES.find((s) => s.id === introStyle) || STYLES[0]).desc(tileCount));
  // A one-off selection has the meaningless name 'selection' — don't title the movie that.
  const fallbackTitle = $derived(name && name !== 'selection' ? name : '');

  function mixHex(a, b, t) {
    const ch = (h, i) => parseInt(h.replace('#', '').slice(i, i + 2), 16);
    const px = (i) => Math.round(ch(a, i) + (ch(b, i) - ch(a, i)) * t).toString(16).padStart(2, '0');
    return `#${px(0)}${px(2)}${px(4)}`;
  }
  function introColors() {
    if (introPreset === 'custom') {
      return { title: customColor, stroke: mixHex(customColor, '#000000', 0.55), subtitle: mixHex(customColor, '#ffffff', 0.55) };
    }
    return PRESETS.find((p) => p.id === introPreset) || PRESETS[0];
  }
  function introPayload() {
    const c = introColors();
    return {
      style: introStyle,
      title: introTitle.trim() || fallbackTitle,
      subtitle: introSubtitle.trim(),
      title_color: c.title,
      stroke_color: c.stroke,
      subtitle_color: c.subtitle,
      border_color: c.title,
      duration: introDur
    };
  }

  function move(id, d) {
    const a = [...list];
    const from = a.findIndex((v) => v.id === id);
    const to = from + d;
    if (from < 0 || to < 0 || to >= a.length) return;
    a.splice(to, 0, a.splice(from, 1)[0]);
    list = a;
  }
  function remove(id) { list = list.filter((v) => v.id !== id); }
  // Re-clickable, like the Play Queue / playlist Randomize. Unlike those, re-roll when
  // the shuffle lands on the current order: with 2–3 clips that's a 50%/17% outcome, and
  // the only feedback here is the badges visibly renumbering — an identical result reads
  // as a broken button. Bounded so a pathological RNG streak can't spin forever.
  function randomize() {
    if (list.length < 2) return;
    for (let tries = 0; tries < 10; tries++) {
      const a = shuffled(list);
      if (a.some((v, i) => v.id !== list[i].id)) { list = a; break; }
    }
  }
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
      if (introOn) {
        saveIntroPrefs({ title: introTitle.trim(), subtitle: introSubtitle.trim(), style: introStyle, preset: introPreset, custom: customColor, duration: introDur });
      }
      await exportSelection(list.map((v) => v.id), name, introOn ? introPayload() : null);
      toast(`Exported ${list.length} video${list.length === 1 ? '' : 's'}${introOn ? ' with intro' : ''}`, { type: 'success' });
      onclose();
    } catch (e) {
      toast(e?.message || 'Export failed.', { type: 'error' });
    } finally {
      busy = false;
    }
  }
</script>

<!-- While a merge is preparing, freeze dismissal (Escape / click-outside) so the export
     can't be orphaned mid-request; the Cancel button is disabled to match.
     z-[70]: this modal can stack over the PlaylistEditor (default z-[60]). -->
<Modal onclose={busy ? () => {} : onclose} ariaLabel="Arrange videos before export" z="z-[70]" overlay="overlay-strong" closeOnEscape={!busy}
       panelClass="panel flex max-h-[88dvh] w-full max-w-2xl flex-col overflow-hidden rounded-card">
  <div class="flex items-center gap-3 border-b border-line p-4">
    <div class="min-w-0 flex-1">
      <p class="text-base font-bold text-ink">Arrange before merging</p>
      <p class="text-xs text-muted">Drag the handle (or ▲/▼) to set play order · the top clip plays first</p>
    </div>
    <button type="button"
      class="grid h-7 w-7 shrink-0 place-items-center pointer-coarse:h-11 pointer-coarse:w-11 rounded-md border border-line text-muted transition hover:border-[color-mix(in_srgb,var(--accent)_45%,var(--line))] hover:text-ink disabled:opacity-35 disabled:hover:border-line disabled:hover:text-muted"
      disabled={list.length < 2 || busy} onclick={randomize} title="Randomize order" aria-label="Randomize clip order">
      <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"/><path d="m18 2 4 4-4 4"/><path d="M2 6h1.9c1.5 0 2.9.9 3.6 2.2"/><path d="M22 18h-5.9c-1.3 0-2.6-.7-3.3-1.8l-.5-.8"/><path d="m18 14 4 4-4 4"/></svg>
    </button>
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

  <div class="space-y-3 border-t border-line px-4 py-3">
    <label class="flex cursor-pointer items-start gap-3">
      <input type="checkbox" bind:checked={introOn} disabled={busy} class="mt-0.5 h-4 w-4 shrink-0 accent-[var(--accent)]" />
      <span class="min-w-0">
        <span class="block text-sm font-semibold text-ink">Cinematic intro</span>
        <span class="mt-0.5 block text-[11px] leading-snug text-muted">
          Opens the movie with a rendered title-card opener — pick from four styles.
        </span>
      </span>
    </label>
    {#if introOn}
      <div class="flex flex-wrap items-center gap-1.5">
        {#each STYLES as s (s.id)}
          <button type="button" disabled={busy} onclick={() => (introStyle = s.id)}
            class="rounded-full border px-2.5 py-1 text-xs transition {introStyle === s.id ? 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-ink' : 'border-line text-muted hover:text-ink'}"
            aria-pressed={introStyle === s.id}>{s.label}</button>
        {/each}
      </div>
      <p class="text-[11px] leading-snug text-muted">{styleDesc}</p>
      <div class="grid gap-2 sm:grid-cols-2">
        <input type="text" maxlength="80" bind:value={introTitle} disabled={busy} aria-label="Intro title"
          placeholder={fallbackTitle || 'Title (optional)'}
          class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none transition focus:border-[var(--accent)]" />
        <input type="text" maxlength="120" bind:value={introSubtitle} disabled={busy} aria-label="Intro subtitle"
          placeholder="Subtitle (optional)"
          class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none transition focus:border-[var(--accent)]" />
      </div>
      <div class="flex flex-wrap items-center gap-1.5">
        {#each PRESETS as p (p.id)}
          <button type="button" disabled={busy} onclick={() => (introPreset = p.id)}
            class="flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition {introPreset === p.id ? 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-ink' : 'border-line text-muted hover:text-ink'}"
            aria-pressed={introPreset === p.id}>
            <span class="h-2.5 w-2.5 rounded-full" style="background:{p.title}" aria-hidden="true"></span>{p.label}
          </button>
        {/each}
        <label class="flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition {introPreset === 'custom' ? 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-ink' : 'border-line text-muted hover:text-ink'}">
          <input type="color" bind:value={customColor} disabled={busy} oninput={() => (introPreset = 'custom')} onclick={() => (introPreset = 'custom')}
            class="h-3.5 w-5 cursor-pointer rounded-sm border-0 bg-transparent p-0" aria-label="Custom intro color" />
          Custom
        </label>
        <span class="mx-1 hidden h-4 w-px bg-line sm:inline-block" aria-hidden="true"></span>
        {#each DURATIONS as d (d)}
          <button type="button" disabled={busy} onclick={() => (introDur = d)}
            class="rounded-full border px-2.5 py-1 text-xs tabular-nums transition {introDur === d ? 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-ink' : 'border-line text-muted hover:text-ink'}"
            aria-pressed={introDur === d}>{d}s</button>
        {/each}
      </div>
    {/if}
  </div>

  <div class="flex items-center gap-2 border-t border-line p-4">
    <span class="hidden text-xs text-muted sm:inline">Combined into one MP4, top to bottom.</span>
    <Button class="ml-auto" variant="secondary" disabled={busy} onclick={onclose}>Cancel</Button>
    <Button disabled={!list.length || busy} onclick={doExport}>{busy ? (introOn ? 'Rendering intro…' : 'Preparing…') : 'Export merged'}</Button>
  </div>
</Modal>
