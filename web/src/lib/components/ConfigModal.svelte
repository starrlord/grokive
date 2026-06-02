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
    if (curlErr) { msg = curlErr; msgClass = 'text-red-400'; }
    else { msg = 'Saved.'; msgClass = 'text-green-400'; setTimeout(onclose, 800); }
  }
</script>

<svelte:window onkeydown={(e) => e.key === 'Escape' && onclose()} />

<!-- Backdrop is presentational chrome; dismissal is mirrored by Escape (above) and the buttons inside. -->
<div use:portal class="fixed inset-0 z-[60] grid place-items-center bg-black/65 p-4 backdrop-blur-sm" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="panel w-full max-w-2xl overflow-hidden rounded-card p-5" role="dialog" aria-modal="true" aria-label="Config" tabindex="-1">
    <h2 class="mb-3 text-lg font-bold">Config</h2>

    <h3 class="mb-2 font-bold">Appearance</h3>
    <div class="mb-2 flex flex-wrap gap-2">
      {#each THEMES as t}
        <button class="rounded-lg border px-3 py-1.5 text-sm font-semibold {$theme === t.id ? 'border-transparent bg-[var(--accent)] text-white' : 'border-line'}" onclick={() => setTheme(t.id)}>{t.label}</button>
      {/each}
    </div>
    <div class="mb-4 flex gap-2">
      {#each layouts as l}
        <button class="rounded-lg border px-3 py-1.5 text-sm font-semibold {$mode === l.id ? 'border-transparent bg-[var(--accent)] text-white' : 'border-line'}" onclick={() => mode.set(l.id)}>{l.label}</button>
      {/each}
    </div>
    <hr class="my-4 border-line" />

    <h3 class="mb-2 font-bold">Grok account</h3>
    <p class="mb-2 text-sm text-muted">Paste the <code class="rounded-sm bg-white/10 px-1">Copy as cURL (bash)</code> request from <code class="rounded-sm bg-white/10 px-1">grok.com/rest/media/post/list</code>. Stored only on this server.</p>
    <textarea class="h-40 w-full resize-y rounded-lg border border-line bg-black/30 p-3 font-mono text-xs outline-none"
      placeholder="curl 'https://grok.com/rest/media/post/list' ..." bind:value={curl}></textarea>
    <p class="mt-1 text-xs text-muted">{curlNote}</p>

    <hr class="my-4 border-line" />
    <h3 class="mb-1 font-bold">Subtitles (Whisper)</h3>
    <p class="mb-2 text-sm text-muted">Optional whisper-asr-webservice endpoint, e.g. <code class="rounded-sm bg-white/10 px-1">http://192.168.1.10:9000/asr</code></p>
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
      <button class="rounded-lg bg-[var(--accent)] px-4 py-2 font-bold text-white" onclick={save}>Save</button>
    </div>
  </div>
</div>
