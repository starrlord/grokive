<script>
  // Grok weekly-usage bolts for the top bar: one ⚡ + "% used" chip per ACTIVE
  // account (mirrors the Firefox extension's quota badge), each opening a popover
  // with the per-product breakdown. The server proxies Grok's credits RPC with each
  // account's stored session and caches ~4 min, so polling here is cheap.
  import { onMount, onDestroy } from 'svelte';
  import { getAccountsQuota } from '$lib/api.js';
  import Popover from './Popover.svelte';

  const POLL_MS = 5 * 60 * 1000; // weekly numbers move slowly — the extension's cadence

  let entries = $state([]);
  let timer;

  async function refresh() {
    try { entries = (await getAccountsQuota()).accounts || []; } catch {}
  }
  onMount(() => { refresh(); timer = setInterval(refresh, POLL_MS); });
  onDestroy(() => clearInterval(timer));

  // Chips: usable numbers always show; a dead/missing session shows a red "!" bolt
  // (that's the account to re-capture). Transient failures (network/5xx) stay hidden
  // rather than flashing a broken readout.
  const shown = $derived((entries || []).filter((e) => e && (e.ok ? typeof e.used_percent === 'number' : e.error === 'auth' || e.error === 'no-session')));

  function fmtPct(v) {
    if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
    const r = Math.round(v * 10) / 10; // one decimal, but drop a trailing .0
    return (Number.isInteger(r) ? String(r) : r.toFixed(1)) + '%';
  }
  const fmtMoney = (cents) => '$' + (Math.max(0, cents) / 100).toFixed(2);
  // Urgency drives the badge colour off the same rounded number it displays.
  function urgency(e) {
    if (!e.ok) return 'out';
    const u = Math.round(e.used_percent * 10) / 10;
    return u >= 100 ? 'out' : u >= 90 ? 'low' : 'ok';
  }
  function fmtResetIn(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `in ${d}d ${h}h`;
    if (h > 0) return `in ${h}h ${m}m`;
    if (m > 0) return `in ${m}m`;
    return 'soon';
  }
  const productRows = (e) => (e.products || []).filter((p) => p.percent > 0).sort((a, b) => b.percent - a.percent);
  function extrasLine(e) {
    const bits = [];
    if (e.prepaid_cents > 0) bits.push(`Extra credits ${fmtMoney(e.prepaid_cents)}`);
    if (e.on_demand_cap_cents > 0) bits.push(`On-demand ${fmtMoney(e.on_demand_used_cents)} / ${fmtMoney(e.on_demand_cap_cents)}`);
    return bits.join(' · ');
  }
  function chipTitle(e) {
    if (!e.ok) return `${e.name} — session expired; re-paste its cURL in Config`;
    const parts = [`${e.name} — ${e.period_type === 'monthly' ? 'monthly' : 'weekly'} usage ${fmtPct(e.used_percent)} used`];
    if (e.reset_at) parts.push(`resets ${fmtResetIn(e.reset_at - Date.now())}`);
    return parts.join(' · ');
  }
  const barWidth = (v) => Math.max(0, Math.min(100, v)) + '%';
</script>

{#each shown as e (e.id)}
  <Popover align="right" title={chipTitle(e)} ariaLabel={`${e.name} Grok usage`}
    triggerClass="quota-chip quota-chip-{urgency(e)} inline-flex h-9 shrink-0 items-center gap-0.5 rounded-lg border px-1.5 text-xs font-bold transition sm:px-2">
    {#snippet trigger()}
      <span class="quota-bolt" aria-hidden="true">⚡</span>
      <span>{e.ok ? fmtPct(e.used_percent) : '!'}</span>
    {/snippet}
    {#snippet children()}
      <div class="w-[17rem] max-w-[calc(100vw-1rem)] rounded-card border border-line bg-[var(--surface-solid)] p-3 shadow-[0_18px_44px_-14px_rgba(0,0,0,0.6)]">
        <div class="mb-1 flex items-baseline justify-between gap-2">
          <span class="min-w-0 truncate text-sm font-bold">{e.name}</span>
          <span class="shrink-0 text-xs text-muted">{e.period_type === 'monthly' ? 'Monthly' : 'Weekly'} usage</span>
        </div>
        {#if e.ok}
          <div class="mb-1.5 flex items-center justify-between gap-2 text-xs text-muted">
            <span class="font-semibold text-ink">{fmtPct(e.used_percent)} used</span>
            {#if e.reset_at}<span>resets {fmtResetIn(e.reset_at - Date.now())}</span>{/if}
          </div>
          <span class="quota-bar mb-2"><i class="quota-fill quota-fill-{urgency(e)}" style={`width:${barWidth(e.used_percent)}`}></i></span>
          {#if productRows(e).length}
            <div class="mt-2 border-t border-line pt-2">
              {#each productRows(e) as p (p.key)}
                <div class="flex items-center gap-2 py-1 text-xs">
                  <span class="w-14 shrink-0 text-muted">{p.label}</span>
                  <span class="quota-bar min-w-0 flex-1"><i class="quota-fill" style={`width:${barWidth(p.percent)}`}></i></span>
                  <span class="w-12 shrink-0 text-right font-semibold">{fmtPct(p.percent)}</span>
                </div>
              {/each}
            </div>
          {/if}
          {#if extrasLine(e)}
            <div class="mt-2 border-t border-line pt-2 text-xs text-muted">{extrasLine(e)}</div>
          {/if}
        {:else}
          <p class="text-xs text-[var(--danger-ink)]">Session expired or missing — open Config → Grok accounts and paste a fresh cURL for this account.</p>
        {/if}
      </div>
    {/snippet}
  </Popover>
{/each}

<style>
  /* triggerClass lands on Popover's button (outside this component's scope), so the
     chip classes are :global. Every chip keeps the same neutral border as its toolbar
     neighbours — urgency tiers colour only the bolt/text (amber at ≥90% used, red at
     ≥100% / dead session), so no chip is outlined by default. */
  :global(.quota-chip) {
    background: color-mix(in srgb, var(--surface-2) 72%, transparent);
    border-color: var(--line);
    box-shadow: inset 0 1px 0 var(--surface-highlight);
  }

  :global(.quota-chip:hover),
  :global(.quota-chip:focus-visible) {
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
    border-color: var(--accent);
  }

  :global(.quota-chip .quota-bolt) {
    color: var(--accent);
    font-size: 0.8rem;
    line-height: 1;
  }

  :global(.quota-chip-low .quota-bolt),
  :global(.quota-chip-low) {
    color: #f5a524;
  }

  :global(.quota-chip-out) {
    color: var(--danger-ink);
  }

  :global(.quota-chip-out .quota-bolt) {
    color: var(--danger);
  }

  .quota-bar {
    background: color-mix(in srgb, var(--muted) 22%, transparent);
    border-radius: 999px;
    display: block;
    height: 0.375rem;
    overflow: hidden;
  }

  .quota-fill {
    background: var(--accent);
    border-radius: 999px;
    display: block;
    height: 100%;
  }

  .quota-fill-low {
    background: #f5a524;
  }

  .quota-fill-out {
    background: var(--danger);
  }
</style>
