<script>
  import { onMount } from 'svelte';
  import { getConfig, postConfig, getSettings, postSettings, authStatus, logout } from '$lib/api.js';
  import { loadSettings, theme, setTheme, THEMES, mode } from '$lib/state.js';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import Button from './Button.svelte';

  const layouts = [
    { id: 'cinematic', label: 'Grid' },
    { id: 'editorial', label: 'Editorial' }
  ];

  let { onclose = () => {} } = $props();

  let curl = $state('');
  let curlNote = $state('');
  let whisper = $state('');
  let envLocked = $state(false);
  let burn = $state(false);
  let msg = $state('');
  let msgClass = $state('');
  let authRequired = $state(false);
  // The 11-theme gallery lives behind a "Change" disclosure instead of dominating
  // the pane — Appearance shows the current theme, the picker opens in-place.
  let pickingTheme = $state(false);

  const current = $derived(THEMES.find((t) => t.id === $theme) || THEMES[0]);

  onMount(async () => {
    try {
      const c = await getConfig();
      curlNote = c.configured ? `Saved ${c.mtime || ''} — paste again to replace.` : 'No config saved yet.';
    } catch {}
    try {
      const s = await getSettings();
      whisper = s.whisper_server_url || '';
      envLocked = !!s.whisper_env_locked;
      burn = !!s.burn_subtitles;
    } catch {}
    try { authRequired = !!(await authStatus()).auth_required; } catch {}
  });

  async function doLogout() {
    await logout();
    location.reload();
  }

  async function save() {
    msg = 'Saving…'; msgClass = '';
    let curlErr = '';
    if (curl.trim()) {
      const r = await postConfig(curl);
      if (!r.ok) { const j = await r.json().catch(() => ({})); curlErr = j.error || 'cURL save failed.'; }
    }
    const body = { burn_subtitles: burn };
    if (!envLocked) body.whisper_server_url = whisper.trim();
    let settingsErr = '';
    try {
      const r = await postSettings(body);
      if (r && r.ok === false) settingsErr = 'Could not save settings.';
    } catch { settingsErr = 'Could not save settings.'; }
    await loadSettings();
    const err = curlErr || settingsErr;
    if (err) { msg = err; msgClass = 'text-[var(--danger-ink)]'; }
    else { msg = 'Saved.'; msgClass = 'text-[var(--success-ink)]'; setTimeout(onclose, 800); }
  }

  // Escape backs out of the theme picker first, then closes the modal.
  function onkey(e) {
    if (e.key !== 'Escape') return;
    if (pickingTheme) pickingTheme = false;
    else onclose();
  }
</script>

<svelte:window onkeydown={onkey} />

<!-- Theme swatch — reused in the Appearance row (small) and the picker grid (large). -->
{#snippet swatch(t, cls)}
  <span class="theme-swatch overflow-hidden rounded-md border border-line {cls}"
        style={`--sw-bg:${t.preview[0]}; --sw-panel:${t.preview[1]}; --sw-a:${t.preview[2]}; --sw-b:${t.preview[3]};`}></span>
{/snippet}

<!-- Backdrop: full-screen sheet on phones (panel fills it), centered card on ≥sm. -->
<div use:portal class="fixed inset-0 z-[60] bg-[var(--overlay)] backdrop-blur-sm sm:grid sm:place-items-center sm:p-4" role="presentation"
     onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="config-panel panel flex h-[100dvh] w-full flex-col overflow-hidden sm:h-auto sm:max-h-[calc(100dvh-2rem)] sm:max-w-[640px] sm:rounded-card"
       role="dialog" aria-modal="true" aria-label="Config" tabindex="-1" use:trapFocus>
    <header class="cfg-header flex shrink-0 items-center gap-2 border-b border-line px-4 py-3 sm:px-5">
      {#if pickingTheme}
        <button type="button" class="-ml-1 flex items-center gap-1 rounded-lg px-2 py-1.5 text-sm font-semibold transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-11"
          aria-label="Back to settings" onclick={() => (pickingTheme = false)}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
          Back
        </button>
        <h2 class="text-base font-bold">Theme</h2>
      {:else}
        <h2 class="text-lg font-bold">Config</h2>
        <button type="button" class="ml-auto grid h-9 w-9 place-items-center rounded-lg border border-line transition hover:bg-[var(--surface-2)] pointer-coarse:h-11 pointer-coarse:w-11"
          aria-label="Close" onclick={onclose}>✕</button>
      {/if}
    </header>

    <div class="config-scroll min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5" class:cfg-safe-bottom={pickingTheme}>
      {#if pickingTheme}
        <!-- Theme gallery: 1 col on phone, 2 on tablet, 3 on desktop. Applies live. -->
        <div class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {#each THEMES as t (t.id)}
            <button type="button"
              class="theme-choice flex items-center gap-3 rounded-xl border p-2.5 text-left transition pointer-coarse:min-h-14 {$theme === t.id ? 'theme-choice-active border-transparent' : 'border-line hover:border-[var(--accent)]'}"
              aria-pressed={$theme === t.id} onclick={() => setTheme(t.id)}>
              {@render swatch(t, 'h-9 w-14 shrink-0')}
              <span class="min-w-0 flex-1 truncate text-sm font-bold">{t.label}</span>
              {#if $theme === t.id}
                <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-[var(--accent)]" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>
              {/if}
            </button>
          {/each}
        </div>
      {:else}
        <!-- Appearance: compact settings rows (live-applied, separate from Save). -->
        <section>
          <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Appearance</div>
          <div class="overflow-hidden rounded-xl border border-line">
            <button type="button" class="flex w-full items-center gap-3 px-3 py-2.5 text-left transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-12"
              aria-label="Change theme" onclick={() => (pickingTheme = true)}>
              <span class="text-sm font-semibold">Theme</span>
              <span class="ml-auto flex min-w-0 items-center gap-2 text-sm">
                {@render swatch(current, 'h-8 w-12 shrink-0')}
                <span class="truncate font-medium">{current.label}</span>
                <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-muted" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
              </span>
            </button>
            <div class="border-t border-line"></div>
            <div class="flex items-center gap-3 px-3 py-2.5">
              <span class="text-sm font-semibold">View</span>
              <div class="ml-auto inline-grid grid-cols-2 gap-0.5 rounded-lg border border-line bg-[var(--surface-2)] p-0.5">
                {#each layouts as l (l.id)}
                  <button type="button" class="rounded-md px-4 py-1.5 text-sm font-semibold transition pointer-coarse:py-2 {$mode === l.id ? 'bg-[var(--surface-solid)] shadow-sm' : 'text-muted'}"
                    onclick={() => mode.set(l.id)}>{l.label}</button>
                {/each}
              </div>
            </div>
          </div>
        </section>

        <section class="mt-6">
          <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Grok account</div>
          <p class="mb-2 text-sm text-muted">Paste the <code class="rounded-sm bg-[var(--code-bg)] px-1">Copy as cURL (bash)</code> request from <code class="rounded-sm bg-[var(--code-bg)] px-1">grok.com/rest/media/post/list</code>. Stored only on this server.</p>
          <textarea class="h-28 w-full resize-y rounded-lg border border-line bg-[var(--input-code-bg)] p-3 font-mono text-xs outline-none"
            placeholder="curl 'https://grok.com/rest/media/post/list' ..." bind:value={curl}></textarea>
          <p class="mt-1 text-xs text-muted">{curlNote}</p>
        </section>

        <section class="mt-6">
          <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Subtitles (Whisper)</div>
          <p class="mb-2 text-sm text-muted">Optional whisper-asr-webservice endpoint, e.g. <code class="rounded-sm bg-[var(--code-bg)] px-1">http://192.168.1.10:9000/asr</code></p>
          <input class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
            placeholder={envLocked ? 'Set by WHISPER_SERVER_URL env var' : 'http://host:9000/asr'} bind:value={whisper} disabled={envLocked} />
          <label class="mt-3 flex cursor-pointer items-center gap-2 text-sm">
            <input type="checkbox" class="h-4 w-4 accent-[var(--accent)]" bind:checked={burn} /> Burn subtitles into merged playlist exports
          </label>
        </section>

        {#if authRequired}
          <section class="mt-6 flex items-center justify-between gap-3">
            <div class="text-xs font-bold uppercase tracking-wider text-muted">Account</div>
            <Button variant="secondary" class="text-sm pointer-coarse:min-h-11" onclick={doLogout}>Log out</Button>
          </section>
        {/if}

        <section class="mt-6">
          <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">About</div>
          <a href="https://github.com/starrlord/grokive" target="_blank" rel="noopener noreferrer"
            class="group flex items-center gap-3 rounded-xl border border-line bg-[var(--surface-2)] px-4 py-3 transition hover:border-[var(--accent)] hover:bg-[var(--surface-solid)] pointer-coarse:min-h-14">
            <svg viewBox="0 0 16 16" class="h-6 w-6 shrink-0" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-bold">Grokive</span>
              <span class="block truncate text-xs text-muted">github.com/starrlord/grokive</span>
            </span>
            <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-muted transition group-hover:text-[var(--accent)]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 7h10v10"/><path d="m7 17 10-10"/></svg>
          </a>
        </section>
      {/if}
    </div>

    {#if !pickingTheme}
      <footer class="cfg-footer config-actions flex shrink-0 items-center justify-end gap-2 px-4 py-3 sm:px-5">
        <span class="mr-auto min-w-0 flex-1 truncate text-sm {msgClass}">{msg}</span>
        <Button variant="secondary" size="lg" class="pointer-coarse:min-h-11" onclick={onclose}>Cancel</Button>
        <Button variant="primary" size="lg" class="pointer-coarse:min-h-11" onclick={save}>Save</Button>
      </footer>
    {/if}
  </div>
</div>

<style>
  /* Full-screen sheet on phones reaches the screen edges, so the header clears the
     status bar / notch and the footer clears the home indicator. On ≥sm the card is
     inset from the edges, where these insets resolve to 0 and fall back to the base. */
  .cfg-header {
    padding-top: max(0.75rem, env(safe-area-inset-top));
  }

  .cfg-footer {
    padding-bottom: max(0.75rem, env(safe-area-inset-bottom));
  }

  /* Theme picker has no footer, so its scroll content owns the bottom safe area. */
  .cfg-safe-bottom {
    padding-bottom: max(1rem, env(safe-area-inset-bottom));
  }

  .config-scroll {
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
  }

  .config-actions {
    background: color-mix(in srgb, var(--surface-solid) 88%, transparent);
    border-top: 1px solid var(--line);
  }

  .theme-choice {
    background: color-mix(in srgb, var(--surface-2) 45%, transparent);
  }

  .theme-choice-active {
    background: color-mix(in srgb, var(--accent) 14%, var(--surface-2));
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 42%, transparent);
  }

  /* Gradient blend chip: the theme's accent glows from the top-left, its secondary
     from the bottom-right, over a background-tinted base — so the chip reads as the
     theme's mood and keeps dark vs. Light distinguishable. Purely gradient-based, so
     it scales cleanly to any size (compact row + picker grid) with nothing to clip. */
  .theme-swatch {
    background:
      radial-gradient(115% 115% at 14% 12%, var(--sw-a) 0%, transparent 56%),
      radial-gradient(120% 120% at 86% 90%, var(--sw-b) 0%, transparent 58%),
      linear-gradient(135deg, var(--sw-bg), color-mix(in srgb, var(--sw-bg) 80%, var(--sw-panel)));
  }
</style>
