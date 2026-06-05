<script>
  import { onMount, onDestroy } from 'svelte';
  import { startSync, startSubtitles, syncStatus } from '$lib/api.js';
  import { settings, loadSettings } from '$lib/state.js';
  import { portal } from '$lib/portal.js';
  import { toast } from '$lib/toast.js';
  import ConfigModal from './ConfigModal.svelte';

  let { onrefresh = () => {} } = $props();

  let status = $state({ running: false, step: 'idle', job: 'sync', log: [], finished_at: '', auth_hint: false });
  let showLog = $state(false);
  let showConfig = $state(false);
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

  const pillText = $derived(
    status.running ? `${status.job === 'subtitles' ? 'Subtitles' : 'Syncing'}: ${status.step}`
      : status.step === 'error' ? (status.auth_hint ? 'Auth failed' : 'Failed')
      : status.step === 'done' ? `Synced ${status.finished_at || ''}` : 'Ready'
  );
  const statusTone = $derived(
    status.running ? 'status-info'
      : status.step === 'error' ? 'status-danger'
      : status.step === 'done' ? 'status-success'
      : 'status-idle'
  );
  const logLabel = $derived(status.running || status.step !== 'idle' ? pillText : 'Ready · Log');
</script>

<div class="system-controls flex flex-wrap items-center gap-1.5">
  <!-- Status pill + Subtitles are hidden on phones to keep the top bar tidy (sync
       progress still surfaces via the disabled Sync button + a toast on finish). -->
  <button class="status-btn hidden max-w-[min(16rem,48vw)] truncate rounded-full px-3 py-1.5 text-xs font-semibold md:inline-flex {statusTone}" onclick={() => (showLog = !showLog)} title="View job log">
    <span class="status-dot" aria-hidden="true"></span>
    <span class="truncate">{logLabel}</span>
  </button>
  <span class="mx-0.5 hidden h-6 w-px self-center bg-line md:block" aria-hidden="true"></span>
  <button class="rounded-lg border border-transparent bg-[var(--accent)] px-3 py-1.5 text-sm font-semibold text-[var(--on-accent)] transition enabled:hover:brightness-110 enabled:active:brightness-95 disabled:opacity-50" onclick={doSync} disabled={status.running}>Sync</button>
  {#if $settings.whisper_configured}
    <button class="secondary-btn hidden rounded-lg border border-line px-3 py-1.5 text-sm font-semibold md:inline-block disabled:opacity-50" onclick={doSubs} disabled={status.running}>Subtitles</button>
  {/if}
  <button class="grid h-9 w-9 place-items-center rounded-lg border border-line" title="Config" onclick={() => (showConfig = true)}>⚙</button>
</div>

<style>
  .secondary-btn,
  .status-btn {
    background: color-mix(in srgb, var(--surface-2) 72%, transparent);
    box-shadow: inset 0 1px 0 var(--surface-highlight);
    transition: background 160ms ease, border-color 160ms ease, color 160ms ease, box-shadow 160ms ease;
  }

  .secondary-btn:hover:not(:disabled),
  .secondary-btn:focus-visible,
  .status-btn:hover,
  .status-btn:focus-visible {
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
    border-color: var(--accent);
    color: var(--ink);
    box-shadow:
      inset 0 1px 0 var(--surface-highlight),
      0 0 0 1px color-mix(in srgb, var(--accent) 16%, transparent);
  }

  .status-btn {
    align-items: center;
    border: 1px solid var(--line);
    gap: 0.45rem;
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
  <div use:portal class="panel fixed bottom-4 right-4 z-[55] w-[min(680px,calc(100vw-2rem))] overflow-hidden rounded-card">
    <div class="flex items-center justify-between border-b border-line px-4 py-2.5 text-sm font-semibold">
      <span>Job log</span>
      <button class="rounded-sm border border-line px-2 py-0.5 text-xs" onclick={() => (showLog = false)}>Close</button>
    </div>
    <pre class="m-0 max-h-[46vh] overflow-auto whitespace-pre-wrap break-words p-3.5 font-mono text-xs leading-relaxed text-[var(--log-ink)]">{(status.log || []).join('\n') || 'No output yet.'}</pre>
  </div>
{/if}

{#if showConfig}
  <ConfigModal onclose={() => (showConfig = false)} />
{/if}
