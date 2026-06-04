<script>
  import { onMount } from 'svelte';
  import { getConfig, postConfig, getSettings, postSettings, authStatus, logout } from '$lib/api.js';
  import { loadSettings, theme, setTheme, THEMES, mode } from '$lib/state.js';
  import { portal } from '$lib/portal.js';

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
    try { await postSettings(body); } catch {}
    await loadSettings();
    if (curlErr) { msg = curlErr; msgClass = 'text-[var(--danger-ink)]'; }
    else { msg = 'Saved.'; msgClass = 'text-[var(--success-ink)]'; setTimeout(onclose, 800); }
  }
</script>

<svelte:window onkeydown={(e) => e.key === 'Escape' && onclose()} />

<!-- Backdrop is presentational chrome; dismissal is mirrored by Escape (above) and the buttons inside. -->
<div use:portal class="fixed inset-0 z-[60] grid place-items-center bg-[var(--overlay)] p-4 backdrop-blur-sm" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="panel w-full max-w-2xl overflow-hidden rounded-card p-5" role="dialog" aria-modal="true" aria-label="Config" tabindex="-1">
    <h2 class="mb-3 text-lg font-bold">Config</h2>

    <h3 class="mb-2 font-bold">Appearance</h3>
    <div class="theme-picker mb-3 grid gap-2 sm:grid-cols-2">
      {#each THEMES as t (t.id)}
        <button type="button"
          class="theme-choice rounded-lg border p-2 text-left transition {$theme === t.id ? 'theme-choice-active border-transparent' : 'border-line hover:border-[var(--accent)]'}"
          aria-pressed={$theme === t.id}
          onclick={() => setTheme(t.id)}>
          <span class="theme-swatch mb-2 grid h-9 overflow-hidden rounded-md border border-line" style={`--sw-bg:${t.preview[0]}; --sw-panel:${t.preview[1]}; --sw-a:${t.preview[2]}; --sw-b:${t.preview[3]};`}>
            <span class="theme-swatch-bg">
              <span class="theme-swatch-panel"></span>
              <span class="theme-swatch-accent"></span>
              <span class="theme-swatch-secondary"></span>
            </span>
          </span>
          <span class="block truncate text-sm font-bold">{t.label}</span>
        </button>
      {/each}
    </div>
    <div class="mb-4 flex gap-2">
      {#each layouts as l (l.id)}
        <button class="rounded-lg border px-3 py-1.5 text-sm font-semibold {$mode === l.id ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line'}" onclick={() => mode.set(l.id)}>{l.label}</button>
      {/each}
    </div>
    <hr class="my-4 border-line" />

    <h3 class="mb-2 font-bold">Grok account</h3>
    <p class="mb-2 text-sm text-muted">Paste the <code class="rounded-sm bg-[var(--code-bg)] px-1">Copy as cURL (bash)</code> request from <code class="rounded-sm bg-[var(--code-bg)] px-1">grok.com/rest/media/post/list</code>. Stored only on this server.</p>
    <textarea class="h-40 w-full resize-y rounded-lg border border-line bg-[var(--input-code-bg)] p-3 font-mono text-xs outline-none"
      placeholder="curl 'https://grok.com/rest/media/post/list' ..." bind:value={curl}></textarea>
    <p class="mt-1 text-xs text-muted">{curlNote}</p>

    <hr class="my-4 border-line" />
    <h3 class="mb-1 font-bold">Subtitles (Whisper)</h3>
    <p class="mb-2 text-sm text-muted">Optional whisper-asr-webservice endpoint, e.g. <code class="rounded-sm bg-[var(--code-bg)] px-1">http://192.168.1.10:9000/asr</code></p>
    <input class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
      placeholder={envLocked ? 'Set by WHISPER_SERVER_URL env var' : 'http://host:9000/asr'} bind:value={whisper} disabled={envLocked} />
    <label class="mt-3 flex cursor-pointer items-center gap-2 text-sm">
      <input type="checkbox" bind:checked={burn} /> Burn subtitles into merged playlist exports
    </label>

    {#if authRequired}
      <hr class="my-4 border-line" />
      <div class="flex items-center justify-between">
        <h3 class="font-bold">Account</h3>
        <button class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold" onclick={doLogout}>Log out</button>
      </div>
    {/if}

    <div class="mt-4 flex items-center justify-end gap-2">
      <span class="mr-auto text-sm {msgClass}">{msg}</span>
      <button class="rounded-lg border border-line px-4 py-2 font-semibold" onclick={onclose}>Cancel</button>
      <button class="rounded-lg bg-[var(--accent)] px-4 py-2 font-bold text-[var(--on-accent)]" onclick={save}>Save</button>
    </div>
  </div>
</div>

<style>
  .theme-choice {
    background: color-mix(in srgb, var(--surface-2) 45%, transparent);
  }

  .theme-choice-active {
    background: color-mix(in srgb, var(--accent) 14%, var(--surface-2));
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 42%, transparent);
  }

  .theme-swatch-bg {
    background:
      radial-gradient(circle at 82% 18%, color-mix(in srgb, var(--sw-b) 70%, transparent) 0 10%, transparent 24%),
      linear-gradient(135deg, var(--sw-bg), color-mix(in srgb, var(--sw-bg) 72%, var(--sw-a)));
    display: block;
    height: 100%;
    position: relative;
  }

  .theme-swatch-panel {
    background: var(--sw-panel);
    border-radius: 0.35rem;
    bottom: 0.35rem;
    left: 0.45rem;
    position: absolute;
    top: 0.5rem;
    width: 56%;
  }

  .theme-swatch-accent,
  .theme-swatch-secondary {
    border-radius: 999px;
    position: absolute;
  }

  .theme-swatch-accent {
    background: var(--sw-a);
    height: 0.45rem;
    left: 0.8rem;
    top: 0.85rem;
    width: 2.25rem;
  }

  .theme-swatch-secondary {
    background: var(--sw-b);
    bottom: 0.75rem;
    height: 0.65rem;
    right: 0.75rem;
    width: 0.65rem;
  }
</style>
