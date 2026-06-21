<script>
  import { onMount, onDestroy } from 'svelte';
  import { startSync, startSubtitles, syncStatus } from '$lib/api.js';
  import { settings, loadSettings } from '$lib/state.js';
  import { portal } from '$lib/portal.js';
  import { toast } from '$lib/toast.js';
  import { copyText } from '$lib/clipboard.js';
  import ConfigModal from './ConfigModal.svelte';
  import StatsModal from './StatsModal.svelte';
  import Popover from './Popover.svelte';

  let { onrefresh = () => {} } = $props();

  let status = $state({ running: false, step: 'idle', job: 'sync', log: [], finished_at: '', auth_hint: false });
  let showLog = $state(false);
  let showConfig = $state(false);
  let showStats = $state(false);
  let timer;
  let polling = false;
  // Only announce a finished job we actually watched run (so a leftover 'done'
  // status doesn't fire a toast on every page load).
  let observedRunning = false;

  // Single self-scheduling poll loop. `polling` is set synchronously at the top so a
  // Sync click during the in-flight request can't spawn a second concurrent loop, and
  // `schedule()` always clears the prior timer so exactly one timeout is ever pending
  // (no leak on unmount). `kick()` just ensures a loop is running.
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(poll, 2000);
  }
  async function poll() {
    polling = true;
    try { status = await syncStatus(); } catch { polling = false; return; }
    if (status.running) {
      observedRunning = true;
      schedule();
    } else {
      polling = false;
      if (!observedRunning) return;
      observedRunning = false;
      const subs = status.job === 'subtitles';
      if (status.step === 'done') {
        onrefresh();
        toast(subs ? 'Subtitles generated' : 'Sync complete', { type: 'success' });
      } else if (status.step === 'error') {
        toast(status.auth_hint ? 'Sync failed — check your Grok auth' : `${subs ? 'Subtitles' : 'Sync'} failed`, { type: 'error' });
        showLog = true; // surface the log so the failure detail is one glance away
      }
    }
  }
  function kick() { observedRunning = true; if (!polling) poll(); }

  async function doSync() { await startSync(); kick(); }
  async function doSubs() {
    const r = await startSubtitles();
    if (r.status === 400) { toast('Set a Whisper URL in Config', { type: 'error' }); return; }
    kick();
  }

  onMount(() => { loadSettings(); poll(); });
  onDestroy(() => clearTimeout(timer));

  // Friendly labels for the optional Autonomous Mode post-sync steps (server step names
  // are terse). Everything else shows its raw step name, as before.
  const STEP_LABELS = { embed: 'Updating prompt index', library: 'Importing prompts', autotag: 'Tagging prompts' };
  const stepLabel = (s) => STEP_LABELS[s] || s;
  const stepTitle = (name) => STEP_LABELS[name] || title(name);
  const pillText = $derived(
    status.running ? `${status.job === 'subtitles' ? 'Subtitles' : 'Syncing'}: ${stepLabel(status.step)}`
      : status.step === 'error' ? (status.auth_hint ? 'Auth failed' : 'Failed')
      : status.step === 'done' ? 'Synced' : 'Ready'
  );
  // The exact finish time moves to the hover so the pill stays compact (it was the
  // longest thing in the top bar).
  const pillTitle = $derived(
    status.step === 'done' && status.finished_at ? `Synced ${status.finished_at} · view job log` : 'View job log'
  );
  const statusTone = $derived(
    status.running ? 'status-info'
      : status.step === 'error' ? 'status-danger'
      : status.step === 'done' ? 'status-success'
      : 'status-idle'
  );
  const logLabel = $derived(status.running || status.step !== 'idle' ? pillText : 'Ready · Log');

  // --- Job-log parsing: the server frames each subcommand as
  //   === <step>: <cmd> ===  …output…  --- <step> exited with code <N> ---
  // (see _run_step in server.py). Parse that into steps so the log reads as a
  // checklist with key metrics, while the Raw tab keeps the full console output.
  let logTab = $state('summary'); // 'summary' | 'raw'
  let rawEl = $state(null);

  const logText = $derived((status.log || []).join('\n'));

  const steps = $derived.by(() => {
    const out = [];
    let cur = null;
    for (const raw of status.log || []) {
      const line = String(raw).replace(/\r?\n$/, '');
      const head = line.match(/^=== (.+?): (.+) ===$/);
      const foot = line.match(/^--- (.+?) exited with code (-?\d+) ---$/);
      if (head) { cur = { name: head[1], command: head[2], lines: [], code: null }; out.push(cur); }
      else if (foot && cur) { cur.code = parseInt(foot[2], 10); cur = null; }
      else if (cur) cur.lines.push(line);
    }
    return out;
  });

  const title = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : 'Step');

  // One concise metric per known step; falls back to the last non-empty output line.
  function stepMetric(step) {
    const t = step.lines.join('\n');
    const grab = (re) => { const m = t.match(re); return m && m[1]; };
    switch (step.name) {
      case 'reindex': { const n = grab(/reindex: (\d[\d,]*) records/) || grab(/(\d[\d,]*) records/); return n ? `${n} records` : ''; }
      case 'download': {
        const n = grab(/metadata records: (\d[\d,]*)/);
        const up = grab(/HD upgrades: (\d[\d,]*)/);
        return [n && `${n} items`, up && `${up} HD`].filter(Boolean).join(' · ');
      }
      case 'agents': { const c = grab(/found (\d+) agent canvas/); return c ? `${c} canvas` : ''; }
      case 'index': {
        const rows = grab(/index\.db: (\d[\d,]*) media rows/);
        // New thumbnails == new media items (each fresh download arrives without a
        // thumbnail), so this doubles as the "new this sync" count for the step.
        const th = grab(/thumbnails: (\d+) generated/);
        return [rows && `${rows} rows`, th && th !== '0' && `+${th} new`].filter(Boolean).join(' · ');
      }
      // Autonomous Mode post-sync steps (server log lines from _run_autonomous_steps).
      case 'embed': {
        if (/skipped/.test(t)) return 'skipped';
        const m = t.match(/embedded ([\d,]+)\/([\d,]+)/);
        return m ? `${m[1]}/${m[2]} embedded` : '';
      }
      case 'library': { const n = grab(/imported ([\d,]+) new/); return n ? (n === '0' ? 'nothing new' : `+${n} imported`) : ''; }
      case 'autotag': {
        if (/no new prompts/.test(t)) return 'nothing new';
        if (/skipped/.test(t)) return 'skipped';
        const m = t.match(/tagged ([\d,]+)\/([\d,]+)/);
        return m ? `${m[1]}/${m[2]} tagged` : '';
      }
      default: { const last = [...step.lines].reverse().find((l) => l.trim()); return last ? last.trim().slice(0, 60) : ''; }
    }
  }

  const errorCount = $derived(steps.filter((s) => s.code != null && s.code !== 0).length);
  // New items this sync, inferred from thumbnails generated by the index step (a
  // fresh download has no thumbnail yet, so one gets made per new item).
  const newItems = $derived.by(() => {
    const m = logText.match(/thumbnails: (\d+) generated/);
    return m ? parseInt(m[1], 10) : 0;
  });
  const summaryLine = $derived.by(() => {
    if (!steps.length) return status.running ? 'Starting…' : 'No output yet.';
    if (status.running) return `Running… · ${stepTitle(steps[steps.length - 1]?.name || status.step)}`;
    if (errorCount) return `Finished with ${errorCount} error${errorCount === 1 ? '' : 's'} · ${steps.length} step${steps.length === 1 ? '' : 's'}`;
    const newBit = newItems ? ` · ${newItems} new` : ' · nothing new';
    return `Completed · ${steps.length} step${steps.length === 1 ? '' : 's'}${newBit}`;
  });

  async function copyLog() {
    const ok = await copyText(logText || '');
    toast(ok ? 'Log copied' : 'Copy failed', { type: ok ? 'success' : 'error' });
  }

  // Follow the tail while a job is running and the Raw tab is open.
  $effect(() => {
    logText; // track new lines
    if (logTab === 'raw' && status.running && rawEl) rawEl.scrollTop = rawEl.scrollHeight;
  });
</script>

<div class="system-controls flex flex-wrap items-center gap-1.5">
  <!-- Status pill is hidden on phones to keep the top bar tidy; progress still
       surfaces via the disabled Sync button + a toast on finish. -->
  <button class="status-btn hidden max-w-[min(16rem,48vw)] truncate rounded-full px-3 py-1.5 text-xs font-semibold md:inline-flex {statusTone}" onclick={() => (showLog = !showLog)} title={pillTitle}>
    <span class="status-dot" aria-hidden="true"></span>
    <span class="truncate">{logLabel}</span>
  </button>
  <span class="mx-0.5 hidden h-6 w-px self-center bg-line md:block" aria-hidden="true"></span>
  <button class="rounded-lg border border-transparent bg-[var(--accent)] px-3 py-1.5 text-sm font-semibold text-[var(--on-accent)] transition enabled:hover:brightness-110 enabled:active:brightness-95 disabled:opacity-50" onclick={doSync} disabled={status.running}>Sync</button>
  <Popover align="right" ariaLabel="Settings" title="Settings"
    triggerClass="grid h-9 w-9 place-items-center rounded-lg border border-line bg-[var(--surface-2)] text-base transition hover:border-[var(--accent)]">
    {#snippet trigger()}<span aria-hidden="true">⚙</span>{/snippet}
    {#snippet children(close)}
      <div class="w-56 max-w-[calc(100vw-1rem)] rounded-card border border-line bg-[var(--surface-solid)] p-1.5 shadow-[0_18px_44px_-14px_rgba(0,0,0,0.6)]">
        <button type="button" class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm font-semibold transition hover:bg-[var(--surface-2)]"
          onclick={() => { close(); showStats = true; }}>
          <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3v18h18"/><rect x="7" y="11" width="3" height="6" rx="0.5"/><rect x="12" y="7" width="3" height="10" rx="0.5"/><rect x="17" y="13" width="3" height="4" rx="0.5"/></svg>
          Stats
        </button>
        {#if $settings.whisper_configured}
          <button type="button" class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm font-semibold transition hover:bg-[var(--surface-2)] disabled:opacity-50"
            disabled={status.running} onclick={() => { close(); doSubs(); }}>
            <span aria-hidden="true" class="text-xs font-black tracking-tight text-[var(--accent)]">CC</span>
            Generate subtitles
          </button>
        {/if}
        <button type="button" class="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm font-semibold transition hover:bg-[var(--surface-2)]"
          onclick={() => { close(); showConfig = true; }}>
          <span aria-hidden="true">⚙</span>
          Configuration…
        </button>
      </div>
    {/snippet}
  </Popover>
</div>

<style>
  .status-btn {
    align-items: center;
    gap: 0.45rem;
    border: 1px solid var(--line);
    background: color-mix(in srgb, var(--surface-2) 72%, transparent);
    box-shadow: inset 0 1px 0 var(--surface-highlight);
    transition: background 160ms ease, border-color 160ms ease, color 160ms ease, box-shadow 160ms ease;
  }

  .status-btn:hover,
  .status-btn:focus-visible {
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
    border-color: var(--accent);
    color: var(--ink);
    box-shadow:
      inset 0 1px 0 var(--surface-highlight),
      0 0 0 1px color-mix(in srgb, var(--accent) 16%, transparent);
  }

  .status-dot {
    background: var(--muted);
    border-radius: 999px;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--muted) 16%, transparent);
    flex: 0 0 auto;
    height: 0.45rem;
    width: 0.45rem;
  }

  .status-info {
    border-color: color-mix(in srgb, var(--info) 52%, var(--line));
    color: var(--ink);
  }

  .status-info .status-dot {
    background: var(--info);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--info) 18%, transparent);
  }

  .status-success {
    border-color: color-mix(in srgb, var(--success-solid) 52%, var(--line));
    color: var(--ink);
  }

  .status-success .status-dot {
    background: var(--success-solid);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--success-solid) 18%, transparent);
  }

  .status-danger {
    border-color: color-mix(in srgb, var(--danger) 58%, var(--line));
    color: var(--danger-ink);
  }

  .status-danger .status-dot {
    background: var(--danger);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 18%, transparent);
  }

  @media (min-width: 768px) and (max-width: 1279px) {
    .system-controls {
      flex-wrap: nowrap;
      justify-content: flex-end;
    }

    .system-controls :global(button:first-child) {
      max-width: 9.5rem;
    }
  }
</style>

{#if showLog}
  <div use:portal class="panel fixed bottom-4 right-4 z-[55] flex max-h-[min(70dvh,34rem)] w-[min(680px,calc(100vw-2rem))] flex-col overflow-hidden rounded-card">
    <div class="flex items-center gap-2 border-b border-line px-3 py-2 text-sm">
      <span class="mr-1 font-semibold">Job log</span>
      <div class="inline-grid grid-cols-2 gap-0.5 rounded-lg border border-line bg-[var(--surface-2)] p-0.5 text-xs font-semibold">
        <button class="rounded-md px-2.5 py-1 transition {logTab === 'summary' ? 'bg-[var(--surface-solid)] shadow-sm' : 'text-muted'}" onclick={() => (logTab = 'summary')}>Summary</button>
        <button class="rounded-md px-2.5 py-1 transition {logTab === 'raw' ? 'bg-[var(--surface-solid)] shadow-sm' : 'text-muted'}" onclick={() => (logTab = 'raw')}>Raw log</button>
      </div>
      <button class="ml-auto rounded-sm border border-line px-2 py-0.5 text-xs" title="Copy full log" onclick={copyLog}>Copy</button>
      <button class="rounded-sm border border-line px-2 py-0.5 text-xs" onclick={() => (showLog = false)}>Close</button>
    </div>

    {#if logTab === 'summary'}
      <div class="min-h-0 flex-1 overflow-auto">
        <div class="flex items-center gap-2 border-b border-line px-4 py-2.5 text-sm font-semibold">
          {#if status.running}
            <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--info)]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
          {:else if errorCount}
            <span class="text-[var(--danger-ink)]" aria-hidden="true">✗</span>
          {:else if steps.length}
            <span class="text-[var(--success-ink)]" aria-hidden="true">✓</span>
          {/if}
          <span>{summaryLine}</span>
        </div>
        {#if steps.length}
          <ul class="divide-y divide-line">
            {#each steps as s, i (i)}
              <li class="flex items-center gap-2.5 px-4 py-2">
                {#if s.code == null}
                  <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0 animate-spin text-[var(--info)]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-label="running"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
                {:else if s.code === 0}
                  <span class="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-[var(--success-solid)] text-[10px] font-bold leading-none text-white" aria-label="success">✓</span>
                {:else}
                  <span class="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-[var(--danger)] text-[10px] font-bold leading-none text-white" aria-label="error">✗</span>
                {/if}
                <span class="font-semibold">{stepTitle(s.name)}</span>
                <span class="text-xs {s.code != null && s.code !== 0 ? 'text-[var(--danger-ink)]' : 'text-muted'}">{s.code == null ? 'running…' : s.code === 0 ? 'Done' : `Failed (code ${s.code})`}</span>
                <span class="ml-auto truncate pl-2 text-right text-xs text-muted">{stepMetric(s)}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="px-4 py-6 text-center text-sm text-muted">No output yet.</p>
        {/if}
      </div>
    {:else}
      <pre bind:this={rawEl} class="m-0 min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-3.5 font-mono text-xs leading-relaxed text-[var(--log-ink)]">{logText || 'No output yet.'}</pre>
    {/if}
  </div>
{/if}

{#if showConfig}
  <ConfigModal onclose={() => (showConfig = false)} />
{/if}

{#if showStats}
  <StatsModal onclose={() => (showStats = false)} />
{/if}
