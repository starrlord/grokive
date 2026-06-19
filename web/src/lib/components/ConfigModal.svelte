<script>
  import { onMount } from 'svelte';
  import { getConfig, postConfig, getSettings, postSettings, fetchProviderModels, authStatus, logout } from '$lib/api.js';
  import { loadSettings, theme, setTheme, THEMES, mode } from '$lib/state.js';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import Button from './Button.svelte';
  import SubtitleStyleModal from './SubtitleStyleModal.svelte';

  const layouts = [
    { id: 'cinematic', label: 'Grid' },
    { id: 'editorial', label: 'Editorial' }
  ];
  const providers = [
    { id: 'local', label: 'Local' },
    { id: 'openai', label: 'OpenAI' },
    { id: 'openrouter', label: 'OpenRouter' },
    { id: 'custom', label: 'Custom' }
  ];
  const providerDefaults = {
    local: {
      llm_server_url: '',
      llm_model: 'dolphin3',
      embed_server_url: '',
      embed_model: 'nomic-embed-text'
    },
    openai: {
      llm_server_url: 'https://api.openai.com/v1',
      llm_model: 'gpt-5.4-mini',
      embed_server_url: 'https://api.openai.com/v1',
      embed_model: 'text-embedding-3-small'
    },
    openrouter: {
      llm_server_url: 'https://openrouter.ai/api/v1',
      llm_model: 'openai/gpt-5.4-mini',
      embed_server_url: 'https://openrouter.ai/api/v1',
      embed_model: 'openai/text-embedding-3-small'
    },
    custom: {
      llm_server_url: '',
      llm_model: '',
      embed_server_url: '',
      embed_model: ''
    }
  };

  // Grok Imagine (xAI) generation defaults — aspect ratios the API accepts.
  const XAI_IMAGE_RATIOS = ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3', '2:1', '1:2', 'auto'];
  const XAI_VIDEO_RATIOS = ['16:9', '9:16', '1:1', '4:3', '3:4', '3:2', '2:3'];

  let { onclose = () => {} } = $props();

  let curl = $state('');
  let curlNote = $state('');
  let whisper = $state('');
  let envLocked = $state(false);
  let burn = $state(false);
  let llmProvider = $state('local');
  let llmUrl = $state('');
  let llmModel = $state('');
  let llmKey = $state('');
  let llmKeyConfigured = $state(false);
  let llmEnvLocked = $state(false);
  let llmModelLocked = $state(false);
  let llmVisionModel = $state('');
  let llmVisionModelLocked = $state(false);
  let llmKeyLocked = $state(false);
  let llmClearKey = $state(false);
  let llmModelOptions = $state([]);
  let llmModelsLoading = $state(false);
  let llmModelsNote = $state('');
  let llmVisionModelOptions = $state([]);
  let llmVisionModelsLoading = $state(false);
  let llmVisionModelsNote = $state('');
  let embedProvider = $state('local');
  let embedUrl = $state('');
  let embedModel = $state('');
  let embedKey = $state('');
  let embedKeyConfigured = $state(false);
  let embedEnvLocked = $state(false);
  let embedModelLocked = $state(false);
  let embedKeyLocked = $state(false);
  let embedClearKey = $state(false);
  let embedModelOptions = $state([]);
  let embedModelsLoading = $state(false);
  let embedModelsNote = $state('');
  let xaiKey = $state('');
  let xaiKeyConfigured = $state(false);
  let xaiKeyLocked = $state(false);
  let xaiClearKey = $state(false);
  let xaiImageModel = $state('');
  let xaiImageModelLocked = $state(false);
  let xaiVideoModel = $state('');
  let xaiVideoModelLocked = $state(false);
  let xaiImageResolution = $state('1k');
  let xaiImageAspect = $state('1:1');
  let xaiVideoResolution = $state('480p');
  let xaiVideoAspect = $state('16:9');
  let xaiVideoDuration = $state(6);
  let msg = $state('');
  let msgClass = $state('');
  let authRequired = $state(false);
  // The 11-theme gallery lives behind a "Change" disclosure instead of dominating
  // the pane — Appearance shows the current theme, the picker opens in-place.
  let pickingTheme = $state(false);
  let pickingPromptAi = $state(false);
  let pickingImagine = $state(false);
  let showSubStyle = $state(false);
  let llmProviderDrafts = {};
  let embedProviderDrafts = {};

  const current = $derived(THEMES.find((t) => t.id === $theme) || THEMES[0]);
  const llmProviderLabel = $derived(providerLabel(llmProvider));
  const embedProviderLabel = $derived(providerLabel(embedProvider));

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
      llmProvider = s.llm_provider || providerFromUrl(s.llm_server_url);
      llmUrl = s.llm_server_url || '';
      llmModel = s.llm_model || '';
      llmKeyConfigured = !!s.llm_api_key_configured;
      llmEnvLocked = !!s.llm_env_locked;
      llmModelLocked = !!s.llm_model_env_locked;
      llmVisionModel = s.llm_vision_model || '';
      llmVisionModelLocked = !!s.llm_vision_model_env_locked;
      llmKeyLocked = !!s.llm_api_key_env_locked;
      rememberProviderDraft('llm');
      embedProvider = s.embed_provider || providerFromUrl(s.embed_server_url);
      embedUrl = s.embed_server_url || '';
      embedModel = s.embed_model || '';
      embedKeyConfigured = !!s.embed_api_key_configured;
      embedEnvLocked = !!s.embed_env_locked;
      embedModelLocked = !!s.embed_model_env_locked;
      embedKeyLocked = !!s.embed_api_key_env_locked;
      rememberProviderDraft('embed');
      xaiKeyConfigured = !!s.xai_api_key_configured;
      xaiKeyLocked = !!s.xai_api_key_env_locked;
      xaiImageModel = s.xai_image_model || '';
      xaiImageModelLocked = !!s.xai_image_model_env_locked;
      xaiVideoModel = s.xai_video_model || '';
      xaiVideoModelLocked = !!s.xai_video_model_env_locked;
      xaiImageResolution = s.xai_image_resolution || '1k';
      xaiImageAspect = s.xai_image_aspect_ratio || '1:1';
      xaiVideoResolution = s.xai_video_resolution || '480p';
      xaiVideoAspect = s.xai_video_aspect_ratio || '16:9';
      xaiVideoDuration = s.xai_video_duration || 6;
    } catch {}
    try { authRequired = !!(await authStatus()).auth_required; } catch {}
  });

  function providerFromUrl(url = '') {
    const lower = String(url || '').toLowerCase();
    if (lower.includes('openrouter.ai')) return 'openrouter';
    if (lower.includes('openai.com')) return 'openai';
    if (lower) return 'custom';
    return 'local';
  }

  function providerLabel(id) {
    return providers.find((p) => p.id === id)?.label || 'Local';
  }

  function providerDefaultsFor(kind, provider) {
    const defaults = providerDefaults[provider] || providerDefaults.custom;
    return kind === 'llm'
      ? { url: defaults.llm_server_url, model: defaults.llm_model }
      : { url: defaults.embed_server_url, model: defaults.embed_model };
  }

  function rememberProviderDraft(kind) {
    if (kind === 'llm') {
      llmProviderDrafts = {
        ...llmProviderDrafts,
        [llmProvider]: { url: llmUrl, model: llmModel }
      };
      return;
    }
    embedProviderDrafts = {
      ...embedProviderDrafts,
      [embedProvider]: { url: embedUrl, model: embedModel }
    };
  }

  function providerDraft(kind, provider) {
    const drafts = kind === 'llm' ? llmProviderDrafts : embedProviderDrafts;
    return drafts[provider] || providerDefaultsFor(kind, provider);
  }

  function applyProvider(kind, provider) {
    if (kind === 'llm') {
      if (provider === llmProvider) return;
      rememberProviderDraft('llm');
      const draft = providerDraft('llm', provider);
      llmProvider = provider;
      if (!llmEnvLocked) llmUrl = draft.url;
      if (!llmModelLocked) llmModel = draft.model;
      llmKey = '';
      llmClearKey = false;
      llmModelOptions = [];
      llmModelsNote = '';
      llmVisionModelOptions = [];
      llmVisionModelsNote = '';
      return;
    }
    if (provider === embedProvider) return;
    rememberProviderDraft('embed');
    const draft = providerDraft('embed', provider);
    embedProvider = provider;
    if (!embedEnvLocked) embedUrl = draft.url;
    if (!embedModelLocked) embedModel = draft.model;
    embedKey = '';
    embedClearKey = false;
    embedModelOptions = [];
    embedModelsNote = '';
  }

  async function loadProviderModels(kind) {
    const isEmbed = kind === 'embed';
    const isVision = kind === 'vision';
    // Vision shares the chat (LLM) endpoint/provider/key — it's just a different model.
    const payload = {
      kind,
      provider: isEmbed ? embedProvider : llmProvider,
      url: isEmbed ? embedUrl : llmUrl
    };
    const typedKey = isEmbed ? embedKey.trim() : llmKey.trim();
    if (typedKey && !(isEmbed ? embedClearKey : llmClearKey)) payload.api_key = typedKey;

    const setLoading = (v) => { if (isEmbed) embedModelsLoading = v; else if (isVision) llmVisionModelsLoading = v; else llmModelsLoading = v; };
    const setNote = (v) => { if (isEmbed) embedModelsNote = v; else if (isVision) llmVisionModelsNote = v; else llmModelsNote = v; };
    const setOptions = (v) => { if (isEmbed) embedModelOptions = v; else if (isVision) llmVisionModelOptions = v; else llmModelOptions = v; };

    setLoading(true);
    setNote('Loading models...');
    try {
      const data = await fetchProviderModels(payload);
      const models = Array.isArray(data.models) ? data.models : [];
      setOptions(models);
      setNote(models.length ? `${models.length} models loaded.` : (data.note || 'No matching models returned.'));
    } catch (err) {
      setOptions([]);
      setNote(err?.message || 'Could not load models.');
    } finally {
      setLoading(false);
    }
  }

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
    body.llm_provider = llmProvider;
    body.embed_provider = embedProvider;
    if (!llmEnvLocked) body.llm_server_url = llmUrl.trim();
    if (!llmModelLocked) body.llm_model = llmModel.trim();
    if (!llmVisionModelLocked) body.llm_vision_model = llmVisionModel.trim();
    if (!llmKeyLocked) {
      if (llmKey.trim()) body.llm_api_key = llmKey.trim();
      if (llmClearKey) body.llm_api_key_clear = true;
    }
    if (!embedEnvLocked) body.embed_server_url = embedUrl.trim();
    if (!embedModelLocked) body.embed_model = embedModel.trim();
    if (!embedKeyLocked) {
      if (embedKey.trim()) body.embed_api_key = embedKey.trim();
      if (embedClearKey) body.embed_api_key_clear = true;
    }
    if (!xaiKeyLocked) {
      if (xaiKey.trim()) body.xai_api_key = xaiKey.trim();
      if (xaiClearKey) body.xai_api_key_clear = true;
    }
    if (!xaiImageModelLocked) body.xai_image_model = xaiImageModel.trim();
    if (!xaiVideoModelLocked) body.xai_video_model = xaiVideoModel.trim();
    body.xai_image_resolution = xaiImageResolution;
    body.xai_image_aspect_ratio = xaiImageAspect;
    body.xai_video_resolution = xaiVideoResolution;
    body.xai_video_aspect_ratio = xaiVideoAspect;
    body.xai_video_duration = xaiVideoDuration;
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

  // Escape backs out of nested config pages first, then closes the modal.
  function onkey(e) {
    if (e.key !== 'Escape') return;
    if (showSubStyle) return; // the subtitle dialog handles its own Escape
    if (pickingTheme) pickingTheme = false;
    else if (pickingPromptAi) pickingPromptAi = false;
    else if (pickingImagine) pickingImagine = false;
    else onclose();
  }
</script>

<svelte:window onkeydown={onkey} />

<!-- Theme swatch — reused in the Appearance row (small) and the picker grid (large). -->
{#snippet swatch(t, cls)}
  <span class="theme-swatch overflow-hidden rounded-md border border-line {cls}"
        style={`--sw-bg:${t.preview[0]}; --sw-panel:${t.preview[1]}; --sw-a:${t.preview[2]}; --sw-b:${t.preview[3]};`}></span>
{/snippet}

{#snippet promptAiSettings()}
  <div class="space-y-3">
    <div class="rounded-xl border border-line p-3">
      <div class="mb-2 flex items-center justify-between gap-3">
        <div class="text-sm font-bold">Chat model</div>
        {#if llmKeyConfigured}
          <span class="shrink-0 rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-xs font-semibold text-muted">{llmKeyLocked ? 'Env key' : 'Saved key'}</span>
        {/if}
      </div>
      <div class="mb-3 grid grid-cols-2 gap-1 rounded-lg border border-line bg-[var(--surface-2)] p-1 sm:grid-cols-4">
        {#each providers as p (p.id)}
          <button type="button" class="rounded-md px-2 py-1.5 text-sm font-semibold transition pointer-coarse:min-h-10 {llmProvider === p.id ? 'bg-[var(--surface-solid)] shadow-sm' : 'text-muted'}"
            aria-pressed={llmProvider === p.id} onclick={() => applyProvider('llm', p.id)}>{p.label}</button>
        {/each}
      </div>
      <div class="grid gap-2 sm:grid-cols-[1fr_minmax(10rem,13rem)_auto]">
        <input class="min-w-0 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          placeholder={llmEnvLocked ? 'Set by LLM_SERVER_URL env var' : 'http://host:11434/v1'}
          bind:value={llmUrl} disabled={llmEnvLocked} />
        <input class="min-w-0 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          placeholder={llmModelLocked ? 'Set by LLM_MODEL env var' : 'model'}
          bind:value={llmModel} disabled={llmModelLocked} />
        <button type="button" class="rounded-lg border border-line px-3 text-sm font-semibold transition hover:bg-[var(--surface-2)] disabled:opacity-60 pointer-coarse:min-h-10"
          onclick={() => loadProviderModels('llm')} disabled={llmModelsLoading || !llmUrl || llmModelLocked}>
          {llmModelsLoading ? 'Loading' : 'Models'}
        </button>
      </div>
      {#if llmModelOptions.length}
        <select class="mt-2 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
          bind:value={llmModel}>
          {#each llmModelOptions as model (model.id)}
            <option value={model.id}>{model.name ? `${model.id} — ${model.name}` : model.id}</option>
          {/each}
        </select>
      {/if}
      {#if llmModelsNote}
        <p class="mt-1 text-xs text-muted">{llmModelsNote}</p>
      {/if}
      <div class="mt-3 border-t border-line pt-3">
        <label for="llm-vision-model" class="mb-1 block text-xs font-semibold text-muted">
          Vision model <span class="font-normal">— for “Describe for Grok” on the image lightbox</span>
        </label>
        <div class="grid gap-2 sm:grid-cols-[1fr_auto]">
          <input id="llm-vision-model" class="min-w-0 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
            placeholder={llmVisionModelLocked ? 'Set by LLM_VISION_MODEL env var' : 'blank uses the chat model'}
            bind:value={llmVisionModel} disabled={llmVisionModelLocked} />
          <button type="button" class="rounded-lg border border-line px-3 text-sm font-semibold transition hover:bg-[var(--surface-2)] disabled:opacity-60 pointer-coarse:min-h-10"
            onclick={() => loadProviderModels('vision')} disabled={llmVisionModelsLoading || !llmUrl || llmVisionModelLocked}>
            {llmVisionModelsLoading ? 'Loading' : 'Models'}
          </button>
        </div>
        {#if llmVisionModelOptions.length}
          <select class="mt-2 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
            bind:value={llmVisionModel}>
            {#each llmVisionModelOptions as model (model.id)}
              <option value={model.id}>{model.name ? `${model.id} — ${model.name}` : model.id}</option>
            {/each}
          </select>
        {/if}
        {#if llmVisionModelsNote}
          <p class="mt-1 text-xs text-muted">{llmVisionModelsNote}</p>
        {/if}
      </div>
      <div class="mt-2 flex gap-2">
        <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          type="password" autocomplete="off"
          placeholder={llmKeyLocked ? 'Set by environment' : (llmKeyConfigured ? 'Leave blank to keep saved key' : 'API key')}
          bind:value={llmKey} disabled={llmKeyLocked || llmClearKey} />
        {#if llmKeyConfigured && !llmKeyLocked}
          <button type="button" class="rounded-lg border border-line px-3 text-sm font-semibold transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-10"
            aria-pressed={llmClearKey} onclick={() => { llmClearKey = !llmClearKey; if (llmClearKey) llmKey = ''; }}>{llmClearKey ? 'Keep' : 'Clear'}</button>
        {/if}
      </div>
    </div>

    <div class="rounded-xl border border-line p-3">
      <div class="mb-2 flex items-center justify-between gap-3">
        <div class="text-sm font-bold">Embeddings</div>
        {#if embedKeyConfigured}
          <span class="shrink-0 rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-xs font-semibold text-muted">{embedKeyLocked ? 'Env key' : 'Saved key'}</span>
        {/if}
      </div>
      <div class="mb-3 grid grid-cols-2 gap-1 rounded-lg border border-line bg-[var(--surface-2)] p-1 sm:grid-cols-4">
        {#each providers as p (p.id)}
          <button type="button" class="rounded-md px-2 py-1.5 text-sm font-semibold transition pointer-coarse:min-h-10 {embedProvider === p.id ? 'bg-[var(--surface-solid)] shadow-sm' : 'text-muted'}"
            aria-pressed={embedProvider === p.id} onclick={() => applyProvider('embed', p.id)}>{p.label}</button>
        {/each}
      </div>
      <div class="grid gap-2 sm:grid-cols-[1fr_minmax(10rem,13rem)_auto]">
        <input class="min-w-0 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          placeholder={embedEnvLocked ? 'Set by EMBED_SERVER_URL env var' : 'http://host:11434/v1'}
          bind:value={embedUrl} disabled={embedEnvLocked} />
        <input class="min-w-0 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          placeholder={embedModelLocked ? 'Set by EMBED_MODEL env var' : 'model'}
          bind:value={embedModel} disabled={embedModelLocked} />
        <button type="button" class="rounded-lg border border-line px-3 text-sm font-semibold transition hover:bg-[var(--surface-2)] disabled:opacity-60 pointer-coarse:min-h-10"
          onclick={() => loadProviderModels('embed')} disabled={embedModelsLoading || !embedUrl || embedModelLocked}>
          {embedModelsLoading ? 'Loading' : 'Models'}
        </button>
      </div>
      {#if embedModelOptions.length}
        <select class="mt-2 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none"
          bind:value={embedModel}>
          {#each embedModelOptions as model (model.id)}
            <option value={model.id}>{model.name ? `${model.id} — ${model.name}` : model.id}</option>
          {/each}
        </select>
      {/if}
      {#if embedModelsNote}
        <p class="mt-1 text-xs text-muted">{embedModelsNote}</p>
      {/if}
      <div class="mt-2 flex gap-2">
        <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          type="password" autocomplete="off"
          placeholder={embedKeyLocked ? 'Set by environment' : (embedKeyConfigured ? 'Leave blank to keep saved key' : 'API key')}
          bind:value={embedKey} disabled={embedKeyLocked || embedClearKey} />
        {#if embedKeyConfigured && !embedKeyLocked}
          <button type="button" class="rounded-lg border border-line px-3 text-sm font-semibold transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-10"
            aria-pressed={embedClearKey} onclick={() => { embedClearKey = !embedClearKey; if (embedClearKey) embedKey = ''; }}>{embedClearKey ? 'Keep' : 'Clear'}</button>
        {/if}
      </div>
    </div>
  </div>
{/snippet}

{#snippet imagineSettings()}
  <div class="space-y-3">
    <p class="text-sm text-muted">xAI API key for <strong class="text-ink">Imagine</strong> image &amp; video generation — create one at <code class="rounded-sm bg-[var(--code-bg)] px-1">console.x.ai</code>. Write-only: after saving, Config only shows whether a key exists. Stored only on this server.</p>
    <div class="rounded-xl border border-line p-3">
      <div class="mb-2 flex items-center justify-between gap-3">
        <div class="text-sm font-bold">API key</div>
        {#if xaiKeyConfigured}
          <span class="shrink-0 rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-xs font-semibold text-muted">{xaiKeyLocked ? 'Env key' : 'Saved key'}</span>
        {/if}
      </div>
      <div class="flex gap-2">
        <input class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          type="password" autocomplete="off"
          placeholder={xaiKeyLocked ? 'Set by XAI_API_KEY env var' : (xaiKeyConfigured ? 'Leave blank to keep saved key' : 'xai-...')}
          bind:value={xaiKey} disabled={xaiKeyLocked || xaiClearKey} />
        {#if xaiKeyConfigured && !xaiKeyLocked}
          <button type="button" class="rounded-lg border border-line px-3 text-sm font-semibold transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-10"
            aria-pressed={xaiClearKey} onclick={() => { xaiClearKey = !xaiClearKey; if (xaiClearKey) xaiKey = ''; }}>{xaiClearKey ? 'Keep' : 'Clear'}</button>
        {/if}
      </div>
    </div>

    <div class="grid gap-3 sm:grid-cols-2">
      <div class="rounded-xl border border-line p-3">
        <div class="mb-2 text-sm font-bold">Image defaults</div>
        <input class="mb-2 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          placeholder={xaiImageModelLocked ? 'Set by XAI_IMAGE_MODEL env var' : 'grok-imagine-image-quality'}
          bind:value={xaiImageModel} disabled={xaiImageModelLocked} />
        <div class="grid grid-cols-2 gap-2">
          <label class="block text-xs font-semibold text-muted">Resolution
            <select class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-[var(--ink)] outline-none" bind:value={xaiImageResolution}>
              <option value="1k">1k</option>
              <option value="2k">2k</option>
            </select>
          </label>
          <label class="block text-xs font-semibold text-muted">Aspect ratio
            <select class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-[var(--ink)] outline-none" bind:value={xaiImageAspect}>
              {#each XAI_IMAGE_RATIOS as r (r)}<option value={r}>{r}</option>{/each}
            </select>
          </label>
        </div>
      </div>
      <div class="rounded-xl border border-line p-3">
        <div class="mb-2 text-sm font-bold">Video defaults</div>
        <input class="mb-2 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none disabled:opacity-60"
          placeholder={xaiVideoModelLocked ? 'Set by XAI_VIDEO_MODEL env var' : 'grok-imagine-video'}
          bind:value={xaiVideoModel} disabled={xaiVideoModelLocked} />
        <div class="grid grid-cols-3 gap-2">
          <label class="block text-xs font-semibold text-muted">Resolution
            <select class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-[var(--ink)] outline-none" bind:value={xaiVideoResolution}>
              <option value="480p">480p</option>
              <option value="720p">720p</option>
            </select>
          </label>
          <label class="block text-xs font-semibold text-muted">Aspect
            <select class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-[var(--ink)] outline-none" bind:value={xaiVideoAspect}>
              {#each XAI_VIDEO_RATIOS as r (r)}<option value={r}>{r}</option>{/each}
            </select>
          </label>
          <label class="block text-xs font-semibold text-muted">Seconds
            <input type="number" min="1" max="15" step="1"
              class="mt-1 w-full rounded-lg border border-line bg-[var(--surface-2)] px-2 py-2 text-sm font-normal text-[var(--ink)] outline-none"
              bind:value={xaiVideoDuration} />
          </label>
        </div>
      </div>
    </div>
  </div>
{/snippet}

<!-- Backdrop: full-screen sheet on phones (panel fills it), centered card on ≥sm. -->
<div use:portal class="fixed inset-0 z-[60] bg-[var(--overlay)] backdrop-blur-sm sm:grid sm:place-items-center sm:p-4" role="presentation"
     onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="config-panel panel flex h-[100dvh] w-full flex-col overflow-hidden sm:h-auto sm:max-h-[calc(100dvh-2rem)] sm:max-w-[640px] sm:rounded-card"
       role="dialog" aria-modal="true" aria-label="Config" tabindex="-1" use:trapFocus>
    <header class="cfg-header flex shrink-0 items-center gap-2 border-b border-line px-4 py-3 sm:px-5">
      {#if pickingTheme || pickingPromptAi || pickingImagine}
        <button type="button" class="-ml-1 flex items-center gap-1 rounded-lg px-2 py-1.5 text-sm font-semibold transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-11"
          aria-label="Back to settings" onclick={() => { pickingTheme = false; pickingPromptAi = false; pickingImagine = false; }}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
          Back
        </button>
        <h2 class="text-base font-bold">{pickingTheme ? 'Theme' : pickingImagine ? 'Grok Imagine API' : 'Prompt Studio AI'}</h2>
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
      {:else if pickingPromptAi}
        {@render promptAiSettings()}
      {:else if pickingImagine}
        {@render imagineSettings()}
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
            <input type="checkbox" class="h-4 w-4 accent-[var(--accent)]" bind:checked={burn} /> Burn subtitles into merged exports
          </label>
          <button type="button" class="mt-3 flex w-full items-center gap-3 rounded-xl border border-line px-3 py-2.5 text-left transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-12"
            aria-label="Subtitle display style" onclick={() => (showSubStyle = true)}>
            <span class="text-sm font-semibold">Display style</span>
            <span class="ml-auto flex min-w-0 items-center gap-2 text-sm text-muted">
              <span class="truncate">Font · size · colour · opacity</span>
              <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
            </span>
          </button>
        </section>

        <section class="mt-6">
          <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Prompt Studio AI</div>
          <button type="button" class="flex w-full items-center gap-3 rounded-xl border border-line px-3 py-2.5 text-left transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-12"
            aria-label="Configure Prompt Studio AI" onclick={() => (pickingPromptAi = true)}>
            <span class="text-sm font-semibold">Providers</span>
            <span class="ml-auto flex min-w-0 items-center gap-2 text-sm">
              <span class="min-w-0 truncate font-medium">{llmProviderLabel} chat · {embedProviderLabel} embed</span>
              {#if llmKeyLocked || embedKeyLocked}
                <span class="shrink-0 rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-xs font-semibold text-muted">Env key</span>
              {:else if llmKeyConfigured || embedKeyConfigured}
                <span class="shrink-0 rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-xs font-semibold text-muted">Saved key</span>
              {/if}
              <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-muted" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
            </span>
          </button>
        </section>

        <section class="mt-6">
          <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Grok Imagine API</div>
          <button type="button" class="flex w-full items-center gap-3 rounded-xl border border-line px-3 py-2.5 text-left transition hover:bg-[var(--surface-2)] pointer-coarse:min-h-12"
            aria-label="Configure Grok Imagine API" onclick={() => (pickingImagine = true)}>
            <span class="text-sm font-semibold">Key &amp; defaults</span>
            <span class="ml-auto flex min-w-0 items-center gap-2 text-sm">
              {#if xaiKeyConfigured}
                <span class="shrink-0 rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-xs font-semibold text-muted">{xaiKeyLocked ? 'Env key' : 'Saved key'}</span>
              {:else}
                <span class="shrink-0 text-xs text-muted">Not set</span>
              {/if}
              <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 text-muted" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
            </span>
          </button>
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

{#if showSubStyle}
  <SubtitleStyleModal onclose={() => (showSubStyle = false)} />
{/if}

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
