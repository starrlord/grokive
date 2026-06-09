<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    fetchPromptVocabulary, composePrompts, parsePrompt,
    promptEmbedStatus, startPromptEmbed, fetchPromptThemes, similarPrompts, generatePrompts,
    fetchPersonas, savePersonas
  } from '$lib/api.js';
  import { copyText } from '$lib/clipboard.js';
  import { toast } from '$lib/toast.js';
  import SceneBuilder from './SceneBuilder.svelte';
  import Freeform from './Freeform.svelte';
  import SavedResponses from './SavedResponses.svelte';
  import { addSavedResponse, loadSavedResponses } from '$lib/state.js';

  // Grok Imagine is two-stage: a detailed still (text-to-image) is then animated with a SHORT
  // motion prompt (image-to-video). So the composer emits TWO prompts — Image and Motion — that
  // you copy at each stage. Suggestion chips are revealed only for the focused field. Phase 1
  // (semantic browse + themes) and Phase 2 (Variations/Remix/Polish + Scene Builder) light up
  // when the matching self-hosted endpoint is configured.

  let vocab = $state(null);
  let loading = $state(true);
  let browseQ = $state('');
  let previews = $state({ image: '', motion: '' });
  let composeTimer;
  let remixBusy = $state(false);
  let focused = $state(null);
  let studioMode = $state('compose'); // 'compose' | 'scene' | 'freeform' | 'saved'

  // Phase 1 browse state.
  let embed = $state({ configured: false, embedded: 0, total_unique: 0, missing: 0, running: false, done: 0, total: 0, error: null });
  let themes = $state([]);
  let results = $state(null);
  let resultsLoading = $state(false);
  let pollTimer;

  // Phase 2 generation state.
  let llmReady = $state(false);
  let gen = $state({ running: false, mode: '', stage: '', items: [] });
  let remixTwist = $state('');

  // Persona cards: named character/voice definitions. Scene and Freeform use the active card from
  // the editor panel; Compose has a separate opt-in selector that defaults to No Persona. Cards persist
  // SERVER-SIDE (shared across devices); only the active selection stays per-device. On first load with
  // no server cards, the device's old localStorage cards are migrated (or the example is seeded) and saved.
  const pid = () => 'pc-' + Math.random().toString(36).slice(2, 9);
  // Seeded for brand-new users (no cards anywhere) so the system is self-explanatory. Deletable.
  const EXAMPLE_CARD = {
    id: 'pc-example',
    name: 'Example — Noir Detective',
    anchor: 'Rain dripping from the brim of her fedora',
    text: `You are Vera Cole, a hard-boiled 1940s noir private detective narrating her own case — world-weary, sardonic, and clipped. Keep everything grounded and realistic; no superpowers, no impossible events.

Rules you MUST follow:
- Stay fully in character: dry wit, cynical similes, period slang (dame, gumshoe, joint, heater).
- Be specific and concrete: real places, real objects, plausible actions and dialogue.
- Keep it grounded — no fantasy, no impossible feats.`
  };
  function loadLocalCards() {
    try {
      const raw = localStorage.getItem('ga.personaCards');
      if (raw) { const a = JSON.parse(raw); if (Array.isArray(a) && a.length) return a; }
      const old = localStorage.getItem('ga.persona') || '';
      if (old.trim()) return [{ id: pid(), name: 'My persona', text: old, anchor: '' }];
    } catch {}
    return [EXAMPLE_CARD];
  }
  function loadActiveId(cards) {
    try { const v = JSON.parse(localStorage.getItem('ga.personaActive')); if (v && cards.some((c) => c.id === v)) return v; } catch {}
    return cards[0]?.id ?? null;
  }
  let personaCards = $state([]);
  let activePersonaId = $state(null);
  let composePersonaId = $state('');
  let personaOpen = $state(false);
  let personaLoaded = $state(false);
  let personaSaveTimer;
  let lastSavedJson = ''; // snapshot of the last loaded/saved cards — gates the save effect
  const activeIdx = $derived(personaCards.findIndex((c) => c.id === activePersonaId));
  const activeCard = $derived(activeIdx >= 0 ? personaCards[activeIdx] : null);
  const persona = $derived(activeCard?.text || '');
  const composePersonaCard = $derived(personaCards.find((c) => c.id === composePersonaId) || null);
  const composePersona = $derived(composePersonaCard?.text || '');

  // The one-time localStorage→server migration decision, recorded per-device. Once set, an empty
  // server list means "the user has no personas" (not "seed/migrate again") — so deleting every
  // card sticks instead of resurrecting the example. Gated on this flag (never on the stale shadow
  // copy), so a returning device with leftover `ga.personaCards` doesn't re-clobber the server.
  const personasMigrated = () => { try { return localStorage.getItem('ga.personasMigrated') === '1'; } catch { return false; } };
  const markPersonasMigrated = () => { try { localStorage.setItem('ga.personasMigrated', '1'); } catch {} };

  async function initPersonas() {
    const fetched = await fetchPersonas(); // null = GET failed; [] = server genuinely empty
    if (fetched === null) {
      // Server unreachable. Show a best-effort local view but NEVER auto-save over whatever is
      // really on the server — only an explicit edit writes (and may then fail loudly). Marking the
      // current cards as "clean" keeps the save effect quiet until the user actually changes something.
      const local = loadLocalCards();
      personaCards = local;
      activePersonaId = loadActiveId(local);
      lastSavedJson = JSON.stringify(local);
      personaLoaded = true;
      toast("Couldn't load your personas — reload before editing so changes save correctly.", { type: 'error' });
      return;
    }
    let cards = fetched;
    if (!cards.length && !personasMigrated()) {
      cards = loadLocalCards();        // one-time: migrate this device's old cards, or seed the example
      if (cards.length) savePersonas(cards);
    }
    markPersonasMigrated();            // the migration decision is now made on this device
    personaCards = cards;
    activePersonaId = loadActiveId(cards);
    lastSavedJson = JSON.stringify(cards);
    personaLoaded = true;
  }
  // Persist cards to the server (debounced) when they actually change; the active selection stays
  // per-device. The lastSavedJson guard skips the initial load and any no-op rewrite, so a page
  // load never POSTs identical data back.
  $effect(() => {
    const json = JSON.stringify(personaCards); // deep-track edits / add / delete
    if (!personaLoaded || json === lastSavedJson) return;
    lastSavedJson = json;
    clearTimeout(personaSaveTimer);
    personaSaveTimer = setTimeout(() => savePersonas(personaCards), 600);
  });
  $effect(() => {
    try { localStorage.setItem('ga.personaActive', JSON.stringify(activePersonaId)); } catch {}
  });
  function newCard() {
    const card = { id: pid(), name: 'New persona', text: '', anchor: '' };
    personaCards.push(card);
    activePersonaId = card.id;
    personaOpen = true;
  }
  function deleteCard(id) {
    const i = personaCards.findIndex((c) => c.id === id);
    if (i < 0) return;
    personaCards.splice(i, 1);
    if (activePersonaId === id) activePersonaId = personaCards[0]?.id ?? null;
    if (composePersonaId === id) composePersonaId = '';
  }

  // The eight composer fields + voice, grouped by Grok stage.
  let fields = $state({
    subject: '', wardrobe: '', setting: '', lighting: '',
    action: '', camera: '', voice: '', dialogue: '', continuity: ''
  });
  const IMAGE_KEYS = ['subject', 'wardrobe', 'setting', 'lighting'];
  const MOTION_KEYS = ['action', 'camera', 'voice', 'dialogue', 'continuity'];
  const STAGE_KEYS = { image: IMAGE_KEYS, motion: MOTION_KEYS };
  const FULL = new Set(['subject', 'lighting', 'action', 'dialogue', 'continuity']);
  const PLACEHOLDERS = {
    subject: 'who or what — age, look, identity', wardrobe: 'what they’re wearing',
    setting: 'where the scene takes place', lighting: 'lighting and film look',
    action: 'what moves — keep it short', camera: 'shot, angle, movement',
    voice: 'accent / delivery — e.g. slurred Southern drawl',
    dialogue: 'a line they say (quotes added for you)', continuity: 'a detail to keep stable across the clip'
  };
  // Curated accent/delivery presets (the corpus is thin on voice vocab), merged with any mined.
  const VOICE_PRESETS = [
    'Southern drawl', 'Midwestern', 'Valley girl', 'New York', 'British',
    'raspy', 'husky', 'breathy', 'whispering', 'slurred drunk', 'baby voice', 'monotone', 'high-pitched'
  ];

  const slotMap = $derived(Object.fromEntries((vocab?.slots || []).map((s) => [s.key, s])));
  const combined = $derived(`${previews.image} ${previews.motion}`.trim());
  const isEmpty = $derived(Object.values(fields).every((v) => !v.trim()));
  const shownPrompts = $derived(
    (vocab?.prompts || [])
      .filter((p) => !browseQ.trim() || p.text.toLowerCase().includes(browseQ.trim().toLowerCase()))
      .slice(0, browseQ.trim() ? 12 : 5)
  );

  function chipsFor(slot) {
    if (slot.key !== 'voice') return slot.chips || [];
    const seen = new Set();
    const out = [];
    for (const t of [...VOICE_PRESETS, ...(slot.chips || []).map((c) => c.text)]) {
      const k = t.toLowerCase();
      if (seen.has(k)) continue;
      seen.add(k);
      out.push({ text: t });
      if (out.length >= 16) break;
    }
    return out;
  }

  onMount(async () => {
    vocab = await fetchPromptVocabulary();
    loading = false;
    loadSavedResponses();
    initPersonas();
    await refreshEmbed();
    if (embed.configured && embed.embedded > 0) loadThemes();
  });
  onDestroy(() => clearInterval(pollTimer));

  async function refreshEmbed() {
    const s = await promptEmbedStatus();
    embed = {
      configured: !!s.embed_configured, embedded: s.embedded || 0, total_unique: s.total_unique || 0,
      missing: s.missing || 0, running: !!s.running, done: s.done || 0, total: s.total || 0, error: s.error || null
    };
    llmReady = !!s.llm_configured;
    return embed;
  }
  async function loadThemes() { themes = (await fetchPromptThemes(12)).themes || []; }
  function buildIndex() {
    startPromptEmbed();
    embed = { ...embed, running: true };
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const s = await refreshEmbed();
      if (!s.running) {
        clearInterval(pollTimer);
        if (s.error) toast(`Embedding failed: ${s.error}`, { type: 'error' });
        else { toast('Prompt index ready', { type: 'success' }); loadThemes(); }
      }
    }, 1500);
  }
  async function showResults(title, query) {
    resultsLoading = true;
    results = { title, items: [] };
    const r = await similarPrompts({ ...query, k: 36 });
    results = { title, items: r.results || [] };
    resultsLoading = false;
  }
  const openTheme = (t) => showResults(t.label, { id: t.rep_id });
  const moreLike = (text) => showResults('Similar prompts', { text });
  const clearResults = () => { results = null; };
  function mediaRatio(it) {
    const w = Number(it?.thumb_w || it?.media_w || 1);
    const h = Number(it?.thumb_h || it?.media_h || 1);
    if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return '1 / 1';
    return `${w} / ${h}`;
  }

  // Live preview of both prompts, debounced.
  $effect(() => {
    JSON.stringify(fields);
    clearTimeout(composeTimer);
    composeTimer = setTimeout(async () => { previews = await composePrompts(fields); }, 150);
    return () => clearTimeout(composeTimer);
  });

  function addChip(key, text) {
    const cur = fields[key].trim();
    if (cur && new RegExp(`(^|,\\s*)${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\s*,|$)`, 'i').test(cur)) return;
    fields[key] = cur ? `${cur}, ${text}` : text;
  }

  // Load a prompt's parts into the composer (LLM decompose when available, else heuristic).
  async function loadIntoFields(text, keys = Object.keys(fields)) {
    remixBusy = true;
    try {
      const comps = await parsePrompt(text, llmReady);
      for (const k of keys) {
        const v = comps[k];
        fields[k] = Array.isArray(v) ? v.join(', ') : (v || '');
      }
    } finally {
      remixBusy = false;
    }
  }
  async function remix(text) {
    studioMode = 'compose';
    await loadIntoFields(text);
    if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' });
    toast('Loaded into the composer', { type: 'success' });
  }

  // --- Per-stage AI generation ---------------------------------------------
  async function runGenerate(mode, stage) {
    const prompt = previews[stage];
    if (!prompt.trim() || gen.running) return;
    gen = { running: true, mode, stage, items: [] };
    try {
      const r = await generatePrompts({ prompt, mode, n: mode === 'polish' ? 1 : 4, instruction: mode === 'remix' ? remixTwist : '', persona: composePersona });
      gen = { running: false, mode, stage, items: r.variations || [] };
      if (!gen.items.length) toast('The model returned nothing usable — try again.', { type: 'error' });
    } catch (e) {
      gen = { running: false, mode: '', stage: '', items: [] };
      toast(e.message || 'Generation failed.', { type: 'error' });
    }
  }
  const clearGen = () => (gen = { running: false, mode: '', stage: '', items: [] });
  async function useGenerated(text, stage) {
    await loadIntoFields(text, STAGE_KEYS[stage]); // only repopulate this stage's fields
    clearGen();
    toast('Loaded into the composer', { type: 'success' });
  }
  async function copyText_(text) {
    if (!text.trim()) return;
    const ok = await copyText(text);
    toast(ok ? 'Prompt copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }
  function clearAll() { for (const k of Object.keys(fields)) fields[k] = ''; }

  // Save a hand-composed stage prompt to the library (only AI variations could be saved before).
  // The stage maps to the folder the auto-tagger already uses, so saves land sorted, not Unfiled.
  function saveStage(stage) {
    addSavedResponse(previews[stage], { folder: stage === 'image' ? 'Character Descriptions' : 'Actions / Motion' });
  }
  // Copy both stages at once, labeled so the two prompts stay distinct on the clipboard.
  async function copyBoth() {
    const parts = [];
    if (previews.image.trim()) parts.push(`IMAGE: ${previews.image}`);
    if (previews.motion.trim()) parts.push(`MOTION: ${previews.motion}`);
    if (!parts.length) return;
    const ok = await copyText(parts.join('\n\n'));
    toast(ok ? 'Both prompts copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }

  // Semantic search from the browse rail's search box: when embeddings are built, Enter (or the
  // button) runs a "more like this" over the typed text instead of the live substring filter.
  const semanticReady = $derived(embed.configured && embed.embedded > 0);
  function runSemanticSearch() {
    const q = browseQ.trim();
    if (!q || !semanticReady) return;
    showResults(`Search: ${q}`, { text: q });
  }
  function onSearchKey(e) {
    if (e.key === 'Enter') { e.preventDefault(); runSemanticSearch(); }
  }
</script>

<div class="mx-auto w-full max-w-[1180px]">
  <div class="mb-4 flex flex-wrap items-end justify-between gap-3">
    <div>
      <h1 class="text-xl font-extrabold tracking-tight text-ink">Prompt Studio</h1>
      <p class="text-sm text-muted">
        Two prompts the Grok way — a detailed <strong class="font-semibold text-ink">image</strong>, then a short <strong class="font-semibold text-ink">motion</strong> to animate it.
        {#if vocab}<span class="opacity-70">· {vocab.unique_prompts.toLocaleString()} prompts mined</span>{/if}
      </p>
    </div>
    <div class="flex shrink-0 items-center gap-3">
      {#if remixBusy}<span class="text-xs font-semibold text-[var(--accent)]">Decomposing…</span>{/if}
      <div class="flex shrink-0 rounded-lg border border-line p-0.5">
        <button type="button" class="rounded-md px-3 py-1.5 text-sm font-semibold transition {studioMode === 'compose' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}" onclick={() => (studioMode = 'compose')}>Compose</button>
        <button type="button" class="rounded-md px-3 py-1.5 text-sm font-semibold transition {studioMode === 'scene' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}" onclick={() => (studioMode = 'scene')}>Scene</button>
        <button type="button" class="rounded-md px-3 py-1.5 text-sm font-semibold transition {studioMode === 'freeform' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}" onclick={() => (studioMode = 'freeform')}>Freeform</button>
        <button type="button" class="rounded-md px-3 py-1.5 text-sm font-semibold transition {studioMode === 'saved' ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'text-muted hover:text-ink'}" onclick={() => (studioMode = 'saved')}>Saved</button>
      </div>
      <!-- Always in the layout (just hidden off Compose) so the toggle never shifts/reflows when
           switching modes — a button appearing/disappearing was moving the bar and even wrapping
           the row, which made tabs like Saved hard to click. -->
      <button type="button"
        class="shrink-0 rounded-lg border border-line px-3 py-2 text-sm font-semibold disabled:opacity-40 {studioMode === 'compose' ? '' : 'invisible'}"
        disabled={isEmpty || studioMode !== 'compose'} aria-hidden={studioMode !== 'compose'}
        tabindex={studioMode === 'compose' ? 0 : -1} onclick={clearAll}>Clear</button>
    </div>
  </div>

  <!-- Persona editing is for Scene/Freeform; Compose has a separate opt-in selector. -->
  {#if llmReady && (studioMode === 'scene' || studioMode === 'freeform')}
    <div class="mb-4 rounded-card border border-line bg-[var(--surface-2)]/40">
      <button type="button" class="flex w-full items-center justify-between px-3 py-2 text-left"
        title="Persona applies only to Scene and Freeform. Compose uses its own No Persona dropdown."
        onclick={() => (personaOpen = !personaOpen)}>
        <span class="flex min-w-0 items-center gap-2 text-sm font-bold text-ink">
          Persona
          {#if activeCard && activeCard.text.trim()}<span class="max-w-[12rem] truncate rounded-full bg-[var(--accent)] px-2 py-0.5 text-[0.625rem] font-bold text-[var(--on-accent)]">{activeCard.name || 'on'}</span>{:else}<span class="text-xs font-normal text-muted">optional</span>{/if}
        </span>
        <span class="text-muted">{personaOpen ? '▴' : '▾'}</span>
      </button>
      {#if personaOpen}
        <div class="border-t border-line p-3">
          <!-- Card switcher -->
          <div class="mb-3 flex flex-wrap items-center gap-1.5">
            {#each personaCards as card (card.id)}
              <button type="button" onclick={() => (activePersonaId = card.id)}
                title={`Use ${card.name || 'Untitled'} for Scene and Freeform`}
                class="max-w-[12rem] truncate rounded-full border px-3 py-1 text-xs font-semibold transition {activePersonaId === card.id ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}">{card.name || 'Untitled'}</button>
            {/each}
            <button type="button" onclick={newCard} class="rounded-full border border-dashed border-line px-3 py-1 text-xs font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink">+ New</button>
          </div>

          {#if activeIdx >= 0}
            <div class="mb-2 flex items-center gap-2">
              <input bind:value={personaCards[activeIdx].name} maxlength="40" placeholder="Card name — e.g. Noir Detective, Ship's AI"
                class="min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-3 py-1.5 text-sm font-semibold outline-none focus:border-[var(--accent)]" />
              <button type="button" onclick={() => deleteCard(personaCards[activeIdx].id)} class="rounded-lg border border-line px-2.5 py-1.5 text-xs font-semibold text-[var(--danger)] transition hover:border-[var(--danger)]">Delete</button>
            </div>
            <input bind:value={personaCards[activeIdx].anchor} maxlength="200" placeholder="Keep in every beat (optional) — e.g. rain falling around her"
              class="mb-2 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-1.5 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
            <textarea rows="6" bind:value={personaCards[activeIdx].text}
              placeholder="Paste a character / voice definition — who they are, tone, vocabulary, rules. Describe the VOICE and content, not the output format. Applied to Scene and Freeform."
              class="w-full resize-y rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]"></textarea>
            <p class="mt-2 text-xs text-muted">Synced across your devices · applied only to Scene and Freeform.</p>
          {:else}
            <p class="py-4 text-center text-sm text-muted">No persona cards yet — <button type="button" onclick={newCard} class="font-semibold text-[var(--accent)] hover:underline">create one</button> to write generated lines in a specific voice.</p>
          {/if}
        </div>
      {/if}
    </div>
  {/if}

  {#if loading}
    <p class="py-16 text-center text-sm text-muted">Reading your prompt library…</p>
  {:else if studioMode === 'scene'}
    <SceneBuilder base={combined} {llmReady} {persona} personaAnchor={activeCard?.anchor || ''} />
  {:else if studioMode === 'freeform'}
    <Freeform {persona} {llmReady} />
  {:else if studioMode === 'saved'}
    <SavedResponses {llmReady} onRemix={remix} />
  {:else}
    {#snippet fieldGrid(keys)}
      <div class="grid grid-cols-1 gap-x-3 gap-y-3 sm:grid-cols-2">
        {#each keys as key (key)}
          {@const slot = slotMap[key]}
          {#if slot}
            <div class={FULL.has(key) ? 'sm:col-span-2' : ''}>
              <label class="mb-1 block text-[0.625rem] font-bold uppercase tracking-wider text-muted" for={`ps-${key}`}>{slot.label}</label>
              <textarea id={`ps-${key}`} rows="2" bind:value={fields[key]} placeholder={PLACEHOLDERS[key]}
                onfocus={() => (focused = key)} onblur={() => (focused = null)}
                class="w-full resize-y rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]"></textarea>
              {#if focused === key}
                {@const chips = chipsFor(slot)}
                {#if chips.length}
                  <div class="mt-1.5 flex max-h-24 flex-wrap gap-1.5 overflow-y-auto">
                    {#each chips as c (c.text)}
                      <button type="button" tabindex="-1" title={c.count ? `Used in ${c.count} prompts` : 'Suggestion'}
                        class="rounded-full border border-line px-2.5 py-0.5 text-xs text-muted transition hover:border-[var(--accent)] hover:text-ink"
                        onpointerdown={(e) => { e.preventDefault(); addChip(key, c.text); }}>{c.text}</button>
                    {/each}
                  </div>
                {/if}
              {/if}
            </div>
          {/if}
        {/each}
      </div>
    {/snippet}

    {#snippet promptBox(stage)}
      <div class="rounded-card border border-line bg-[var(--surface-solid)] p-3">
        <div class="mb-1.5 flex items-center justify-between">
          <span class="text-xs font-bold uppercase tracking-wider text-muted">{stage === 'image' ? 'Image prompt' : 'Motion prompt'}</span>
          <span class="text-xs text-muted">{previews[stage].length} chars</span>
        </div>
        <p class="max-h-24 overflow-y-auto whitespace-pre-wrap break-words text-sm text-ink">
          {#if previews[stage].trim()}{previews[stage]}{:else}<span class="text-muted">{stage === 'image' ? 'Fill the image fields above…' : 'Fill the motion fields above…'}</span>{/if}
        </p>
        <div class="mt-2 flex flex-wrap items-center gap-2">
          {#if llmReady}
            <button type="button" title="Generate several alternate versions of this prompt while keeping the same core idea."
              class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)] disabled:opacity-40" disabled={!previews[stage].trim() || gen.running} onclick={() => runGenerate('variations', stage)}>Variations</button>
            {#if stage === 'motion'}
              <input bind:value={remixTwist} placeholder="Remix → new setting (optional)" class="min-w-[8rem] flex-1 rounded-lg border border-line bg-[var(--surface-2)] px-2.5 py-1.5 text-xs outline-none placeholder:text-muted focus:border-[var(--accent)]" />
              <button type="button" title="Rewrite this motion prompt around the optional remix direction."
                class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)] disabled:opacity-40" disabled={!previews[stage].trim() || gen.running} onclick={() => runGenerate('remix', stage)}>Remix</button>
            {/if}
            <button type="button" title="Tighten and improve wording without changing the prompt's intent."
              class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)] disabled:opacity-40" disabled={!previews[stage].trim() || gen.running} onclick={() => runGenerate('polish', stage)}>Polish</button>
          {/if}
          <button type="button" title="Save this prompt to your library"
            class="ml-auto rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)] disabled:opacity-40"
            disabled={!previews[stage].trim()} onclick={() => saveStage(stage)}>★ Save</button>
          <button type="button" class="rounded-lg bg-[var(--accent)] px-4 py-1.5 text-sm font-bold text-[var(--on-accent)] disabled:opacity-40" disabled={!previews[stage].trim()} onclick={() => copyText_(previews[stage])}>Copy</button>
        </div>
        {#if (gen.running || gen.items.length) && gen.stage === stage}
          <div class="mt-2 border-t border-line pt-2">
            <div class="mb-1.5 flex items-center justify-between">
              <span class="text-[0.625rem] font-bold uppercase tracking-wider text-muted">{gen.mode === 'polish' ? 'Polished' : gen.mode === 'remix' ? 'Remix' : 'Variations'}</span>
              {#if !gen.running}<button type="button" class="text-xs text-muted transition hover:text-ink" onclick={clearGen}>Clear</button>{/if}
            </div>
            {#if gen.running}
              <p class="py-3 text-center text-sm text-muted">Generating…</p>
            {:else}
              <ul class="max-h-72 space-y-2 overflow-y-auto">
                {#each gen.items as v (v)}
                  <li class="rounded-lg border border-line bg-[var(--surface-2)] p-2.5">
                    <p class="whitespace-pre-wrap break-words text-xs leading-relaxed text-ink">{v}</p>
                    <div class="mt-1.5 flex gap-1.5">
                      <button type="button" class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]" onclick={() => useGenerated(v, stage)}>Use</button>
                      <button type="button" class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]" onclick={() => copyText_(v)}>Copy</button>
                      <button type="button" class="rounded-md border border-line px-2 py-0.5 text-[0.6875rem] font-semibold transition hover:border-[var(--accent)]" onclick={() => addSavedResponse(v)}>★ Save</button>
                    </div>
                  </li>
                {/each}
              </ul>
            {/if}
          </div>
        {/if}
      </div>
    {/snippet}

    {#if llmReady}
      <div class="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-[var(--surface-2)]/40 px-3 py-2">
        <label for="compose-persona" class="text-xs font-bold uppercase tracking-wider text-muted">Persona</label>
        <select id="compose-persona" bind:value={composePersonaId}
          title="Only applies to Compose AI actions: Variations, Remix, and Polish."
          class="min-w-[12rem] max-w-full rounded-md border border-line bg-[var(--surface)] px-2.5 py-1.5 text-sm font-semibold text-ink outline-none transition hover:border-[var(--accent)] focus:border-[var(--accent)]">
          <option value="">No Persona</option>
          {#each personaCards as card (card.id)}
            <option value={card.id}>{card.name || 'Untitled'}</option>
          {/each}
        </select>
      </div>
    {/if}

    <div class="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <!-- Composer: two stages -->
      <section class="min-w-0 space-y-3">
        <div class="rounded-card border border-line bg-[var(--surface-2)]/40 p-3">
          <div class="mb-2 flex items-baseline gap-2">
            <span class="text-sm font-bold text-ink">① Image</span>
            <span class="text-xs text-muted">the base frame — copy → make the still</span>
          </div>
          {@render fieldGrid(IMAGE_KEYS)}
          <div class="mt-3">{@render promptBox('image')}</div>
        </div>

        <div class="rounded-card border border-line bg-[var(--surface-2)]/40 p-3">
          <div class="mb-2 flex items-baseline gap-2">
            <span class="text-sm font-bold text-ink">② Motion</span>
            <span class="text-xs text-muted">animate the still — short; copy → make the video</span>
          </div>
          {@render fieldGrid(MOTION_KEYS)}
          <div class="mt-3">{@render promptBox('motion')}</div>
        </div>

        {#if previews.image.trim() && previews.motion.trim()}
          <div class="flex items-center justify-end">
            <button type="button" onclick={copyBoth} title="Copy both prompts at once, labeled IMAGE / MOTION"
              class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold transition hover:border-[var(--accent)]">Copy both</button>
          </div>
        {/if}
      </section>

      <!-- Browse / Remix rail. Sticky + self-start so it pins just under the sticky TopBar and
           scrolls on its OWN axis: without self-start the grid stretches it to the tall composer's
           height and its internal scroll is unreachable, so theme results past the first rows (each
           card is a "Remix this prompt" button) looked cut off. -->
      <aside class="min-w-0 lg:sticky lg:top-16 lg:self-start lg:max-h-[calc(100dvh-5rem)] lg:overflow-y-auto lg:pr-1">
        {#if results}
          <div class="mb-3 flex items-center gap-2">
            <button type="button" onclick={clearResults} class="rounded-lg border border-line px-2.5 py-1 text-xs font-semibold transition hover:border-[var(--accent)]">← Back</button>
            <span class="min-w-0 flex-1 truncate text-xs font-bold uppercase tracking-wider text-muted" title={results.title}>{results.title}</span>
          </div>
          {#if resultsLoading}
            <p class="py-8 text-center text-sm text-muted">Finding similar…</p>
          {:else if results.items.length === 0}
            <p class="py-8 text-center text-sm text-muted">Nothing similar found.</p>
          {:else}
            <div class="grid grid-cols-2 gap-2">
              {#each results.items as it (it.id)}
                <button type="button" onclick={() => remix(it.prompt || '')} title="Load this gallery prompt into Compose for remixing"
                  class="group flex flex-col rounded-lg border border-line bg-[var(--surface-2)] text-left transition hover:border-[var(--accent)]">
                  <div class="relative w-full shrink-0 overflow-hidden rounded-t-lg bg-[var(--media-bg)]" style={`aspect-ratio: ${mediaRatio(it)}`}>
                    {#if it.thumb}<img src={it.thumb} alt="" loading="lazy" class="h-full w-full object-contain transition group-hover:scale-[1.02]" />{/if}
                    {#if it._score != null}<span class="absolute right-1 top-1 rounded bg-black/55 px-1 text-[0.625rem] font-semibold text-white">{it._score.toFixed(2)}</span>{/if}
                  </div>
                  <p class="px-1.5 py-1 text-[0.6875rem] leading-snug text-ink">{it.prompt || ''}</p>
                  <span class="mx-1.5 mb-1 inline-flex self-start rounded-full border border-line px-1.5 py-0.5 text-[0.625rem] font-semibold text-muted transition group-hover:border-[var(--accent)] group-hover:text-[var(--accent)]">Load</span>
                </button>
              {/each}
            </div>
          {/if}
        {:else}
          {#if !embed.configured}
            <p class="mb-3 rounded-lg border border-dashed border-line p-2.5 text-xs text-muted">Set <code class="rounded bg-[var(--surface-2)] px-1">EMBED_SERVER_URL</code> to unlock semantic search &amp; theme clusters.</p>
          {:else if embed.running}
            <div class="mb-3 rounded-lg border border-line p-2.5 text-xs text-muted">Building prompt index… {embed.done}/{embed.total || embed.missing}</div>
          {:else if embed.missing > 0}
            <button type="button" onclick={buildIndex} class="mb-3 w-full rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-bold text-[var(--on-accent)]">
              {embed.embedded > 0 ? `Update index (+${embed.missing})` : `Build prompt index (${embed.total_unique})`}
            </button>
          {/if}

          {#if themes.length}
            <div class="mb-2 text-xs font-bold uppercase tracking-wider text-muted">Themes</div>
            <div class="mb-4 space-y-1">
              {#each themes.slice(0, 8) as t (t.label + t.size)}
                <button type="button" onclick={() => openTheme(t)} title={`View matching gallery prompts: ${t.rep_prompt}`}
                  class="flex w-full items-center gap-2 rounded-lg border border-line px-1.5 py-1 text-left text-xs transition hover:border-[var(--accent)]">
                  {#if t.cover}<img src={t.cover} alt="" class="h-6 w-6 shrink-0 rounded-full object-cover" />{:else}<span class="h-6 w-6 shrink-0 rounded-full bg-[var(--surface-2)]"></span>{/if}
                  <span class="min-w-0 flex-1 truncate capitalize">{t.label}</span>
                  <span class="shrink-0 rounded-full border border-line px-1.5 py-0.5 text-[0.625rem] font-semibold text-muted">View</span>
                  <span class="shrink-0 text-muted">{t.size}</span>
                </button>
              {/each}
            </div>
          {/if}

          <div class="mb-2 flex items-baseline justify-between">
            <span class="text-xs font-bold uppercase tracking-wider text-muted">Your prompts</span>
            {#if !browseQ.trim()}<span class="text-[0.625rem] text-muted">top {shownPrompts.length}</span>{/if}
          </div>
          <input type="search" placeholder={semanticReady ? 'Search prompts…' : 'Filter prompts…'} bind:value={browseQ} onkeydown={onSearchKey}
            class="w-full rounded-full border border-line bg-[var(--surface-2)] px-3.5 py-2 text-sm outline-none placeholder:text-muted focus:border-[var(--accent)]" />
          {#if semanticReady && browseQ.trim()}
            <button type="button" onclick={runSemanticSearch} title="Find the most semantically similar prompts (not just text matches)"
              class="mb-3 mt-1.5 w-full truncate rounded-lg border border-line px-3 py-1.5 text-xs font-semibold text-muted transition hover:border-[var(--accent)] hover:text-ink">≈ Search similar to “{browseQ.trim()}”</button>
          {:else}
            <div class="mb-3"></div>
          {/if}
          {#if shownPrompts.length === 0}
            <p class="py-8 text-center text-sm text-muted">No prompts match.</p>
          {:else}
            <ul class="space-y-1.5">
              {#each shownPrompts as p (p.text)}
                <li class="flex items-start gap-1.5">
                  <button type="button" onclick={() => remix(p.text)} title="Load this prompt into Compose"
                    class="group min-w-0 flex-1 rounded-lg border border-line bg-[var(--surface-2)] p-2 text-left transition hover:border-[var(--accent)]">
                    <span class="line-clamp-2 text-xs leading-relaxed text-ink">{p.text}</span>
                    <span class="mt-1 flex items-center justify-between gap-2">
                      {#if p.count > 1}<span class="text-[0.625rem] text-muted">×{p.count}</span>{:else}<span></span>{/if}
                      <span class="rounded-full border border-line px-1.5 py-0.5 text-[0.625rem] font-semibold text-muted transition group-hover:border-[var(--accent)] group-hover:text-[var(--accent)]">Load</span>
                    </span>
                  </button>
                  {#if embed.configured && embed.embedded > 0}
                    <button type="button" onclick={() => moreLike(p.text)} title="Find similar prompts" aria-label="Find similar prompts"
                      class="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-line text-base text-muted transition hover:border-[var(--accent)] hover:text-ink">≈</button>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
        {/if}
      </aside>
    </div>
  {/if}
</div>
