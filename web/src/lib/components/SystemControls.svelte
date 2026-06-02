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

  async function poll() {
    try { status = await syncStatus(); } catch { return; }
    if (status.running) {
      observedRunning = true;
      timer = setTimeout(poll, 2000);
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
  function kick() { if (!polling) { polling = true; observedRunning = true; poll(); } }

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
  const pillClass = $derived(
    status.running ? 'bg-blue-600 text-white'
      : status.step === 'error' ? 'bg-red-600 text-white'
      : status.step === 'done' ? 'bg-green-600 text-white' : 'border border-line'
  );
</script>

<div class="flex flex-wrap items-center gap-1.5">
  <button class="rounded-full px-3 py-1.5 text-xs font-semibold {pillClass}" onclick={() => (showLog = !showLog)} title="Log">{pillText}</button>
  <button class="rounded-lg border border-transparent bg-[var(--accent)] px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50" onclick={doSync} disabled={status.running}>Sync</button>
  {#if $settings.whisper_configured}
    <button class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold disabled:opacity-50" onclick={doSubs} disabled={status.running}>Subtitles</button>
  {/if}
  <button class="grid h-9 w-9 place-items-center rounded-lg border border-line" title="Config" onclick={() => (showConfig = true)}>⚙</button>
</div>

{#if showLog}
  <div use:portal class="panel fixed bottom-4 right-4 z-[55] w-[min(680px,calc(100vw-2rem))] overflow-hidden rounded-card">
    <div class="flex items-center justify-between border-b border-line px-4 py-2.5 text-sm font-semibold">
      <span>Job log</span>
      <button class="rounded border border-line px-2 py-0.5 text-xs" onclick={() => (showLog = false)}>Close</button>
    </div>
    <pre class="m-0 max-h-[46vh] overflow-auto whitespace-pre-wrap break-words p-3.5 font-mono text-xs leading-relaxed text-green-200">{(status.log || []).join('\n') || 'No output yet.'}</pre>
  </div>
{/if}

{#if showConfig}
  <ConfigModal onclose={() => (showConfig = false)} />
{/if}
