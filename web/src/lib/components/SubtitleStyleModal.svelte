<script>
  import { get } from 'svelte/store';
  import { settings, loadSettings, SUBTITLE_FONTS, SUBTITLE_STYLE_DEFAULTS, subtitlePreviewStyle, captionVideoHeight } from '$lib/state.js';
  import { postSettings } from '$lib/api.js';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';

  let { onclose = () => {} } = $props();

  // Snapshot the current style so Cancel can restore the live preview.
  const pick = (s) => ({
    subtitle_font: s.subtitle_font ?? SUBTITLE_STYLE_DEFAULTS.subtitle_font,
    subtitle_size: s.subtitle_size ?? SUBTITLE_STYLE_DEFAULTS.subtitle_size,
    subtitle_color: s.subtitle_color ?? SUBTITLE_STYLE_DEFAULTS.subtitle_color,
    subtitle_bg_opacity: s.subtitle_bg_opacity ?? SUBTITLE_STYLE_DEFAULTS.subtitle_bg_opacity
  });
  const original = pick(get(settings));

  let font = $state(original.subtitle_font);
  let size = $state(original.subtitle_size);
  let color = $state(original.subtitle_color);
  let bg = $state(original.subtitle_bg_opacity);
  let msg = $state('');
  let saving = $state(false);

  const draft = $derived({ subtitle_font: font, subtitle_size: size, subtitle_color: color, subtitle_bg_opacity: bg });

  // Live-apply to the settings store so the player's captions (and the injected
  // ::cue rule) update as you drag — no Save needed to preview on a real video.
  $effect(() => {
    settings.update((s) => ({ ...s, ...draft }));
  });

  // Pass the live video height (0 when none) so the swatch matches the on-video captions.
  const previewStyle = $derived(subtitlePreviewStyle(draft, $captionVideoHeight));
  const D = SUBTITLE_STYLE_DEFAULTS;
  const atDefault = $derived(
    font === D.subtitle_font && size === D.subtitle_size &&
    color === D.subtitle_color && bg === D.subtitle_bg_opacity
  );

  function reset() {
    font = SUBTITLE_STYLE_DEFAULTS.subtitle_font;
    size = SUBTITLE_STYLE_DEFAULTS.subtitle_size;
    color = SUBTITLE_STYLE_DEFAULTS.subtitle_color;
    bg = SUBTITLE_STYLE_DEFAULTS.subtitle_bg_opacity;
  }

  function cancel() {
    settings.update((s) => ({ ...s, ...original })); // revert the live preview
    onclose();
  }

  async function save() {
    saving = true;
    msg = 'Saving…';
    try {
      const r = await postSettings(draft);
      if (r && r.ok === false) throw new Error('save failed');
      await loadSettings();
      onclose();
    } catch {
      msg = 'Could not save.';
      saving = false;
    }
  }
</script>

<!-- z raised above the Lightbox (z-[60]) so it opens cleanly over a playing video. -->
<Modal onclose={cancel} ariaLabel="Subtitle display" z="z-[70]" panelClass="panel w-full max-w-md overflow-hidden rounded-card">
  <header class="flex items-center gap-2 border-b border-line px-5 py-3.5">
    <span aria-hidden="true" class="grid h-6 w-9 place-items-center rounded-md bg-[var(--surface-2)] text-xs font-black tracking-tight text-[var(--accent)]">CC</span>
    <h2 class="text-lg font-bold">Subtitle Display</h2>
    <button type="button" class="ml-auto grid h-9 w-9 place-items-center rounded-lg border border-line transition hover:bg-[var(--surface-2)] pointer-coarse:h-11 pointer-coarse:w-11"
      aria-label="Close" onclick={cancel}>✕</button>
  </header>

  <div class="p-5">
    <!-- Live preview: a caption over a faux frame so colour + opacity read clearly. -->
    <div class="sub-preview relative mb-5 grid h-28 place-items-end justify-center overflow-hidden rounded-xl border border-line">
      <span class="mb-3 inline-block max-w-[90%] rounded px-2 py-0.5 text-center leading-snug" style={previewStyle}>
        The quick brown fox
      </span>
    </div>

    <div class="space-y-4">
      <label class="block">
        <span class="mb-1 block text-xs font-bold uppercase tracking-wider text-muted">Font</span>
        <select class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm text-[var(--ink)] outline-none" bind:value={font}>
          {#each SUBTITLE_FONTS as f (f.id)}
            <option value={f.id}>{f.label}</option>
          {/each}
        </select>
      </label>

      <label class="block">
        <span class="mb-1 flex items-center justify-between text-xs font-bold uppercase tracking-wider text-muted">
          <span>Size</span><span class="tabular-nums text-[var(--ink)]">{size}%</span>
        </span>
        <input type="range" min="50" max="250" step="5" class="w-full accent-[var(--accent)]" bind:value={size} />
      </label>

      <div class="flex items-center justify-between gap-3">
        <span class="text-xs font-bold uppercase tracking-wider text-muted">Text colour</span>
        <span class="flex items-center gap-2">
          <span class="text-sm tabular-nums text-muted">{color}</span>
          <input type="color" class="h-9 w-12 cursor-pointer rounded-lg border border-line bg-[var(--surface-2)] p-1" bind:value={color} aria-label="Text colour" />
        </span>
      </div>

      <label class="block">
        <span class="mb-1 flex items-center justify-between text-xs font-bold uppercase tracking-wider text-muted">
          <span>Background opacity</span><span class="tabular-nums text-[var(--ink)]">{Math.round(bg * 100)}%</span>
        </span>
        <input type="range" min="0" max="1" step="0.05" class="w-full accent-[var(--accent)]" bind:value={bg} />
      </label>
    </div>
  </div>

  <footer class="flex items-center gap-2 border-t border-line px-5 py-3">
    <button type="button" class="rounded-lg px-2 py-1.5 text-sm font-semibold text-muted transition hover:text-[var(--ink)] disabled:opacity-40"
      onclick={reset} disabled={atDefault}>Reset</button>
    <span class="mr-auto min-w-0 flex-1 truncate text-sm text-muted">{msg}</span>
    <Button variant="secondary" size="lg" class="pointer-coarse:min-h-11" onclick={cancel}>Cancel</Button>
    <Button variant="primary" size="lg" class="pointer-coarse:min-h-11" onclick={save} disabled={saving}>Save</Button>
  </footer>
</Modal>

<style>
  /* Checkerboard + gradient so the caption's background opacity is visible against
     both light and dark patches, like captions sitting over real footage. */
  .sub-preview {
    background:
      linear-gradient(135deg, color-mix(in srgb, var(--accent) 34%, transparent), transparent 60%),
      repeating-conic-gradient(var(--surface-2) 0% 25%, var(--surface-solid) 0% 50%) 0 / 22px 22px;
  }
</style>
