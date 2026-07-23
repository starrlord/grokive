<script>
  // Password prompt for collection locks. One component, five modes:
  //   set         – choose a password and lock an open collection
  //   unlock      – enter the password to unlock for 24h (links to `force` recovery)
  //   force       – enter the MAIN admin password to recover a forgotten lock
  //   manage      – an already-unlocked collection: lock now, or remove the password
  //   unlock-all  – enter one password to unlock EVERY collection that uses it (no
  //                 collection prop; the server matches the password against all locks)
  import { fly, fade } from 'svelte/transition';
  import { portal } from '$lib/portal.js';
  import { trapFocus } from '$lib/focusTrap.js';
  import {
    lockCollection, unlockCollection, forceUnlockCollection, relockCollection, removeCollectionLock,
    unlockAllCollections, lockGroup, unlockGroup, forceUnlockGroup, relockGroup, removeGroupLock
  } from '$lib/api.js';

  let { collection = null, group = null, mode = 'unlock', onclose = () => {}, ondone = () => {} } = $props();

  let m = $state('unlock');      // current sub-mode (unlock <-> force can switch)
  let pw = $state('');
  let confirm = $state('');
  let busy = $state(false);
  let err = $state('');
  let confirmRemove = $state(false);

  $effect(() => {
    m = mode;
  });

  const isGroup = $derived(!!group);
  const subject = $derived(isGroup ? group?.name : collection?.name);
  const subjectKind = $derived(isGroup ? 'group' : 'collection');
  const TITLES = $derived({
    set: `Lock ${subjectKind}`,
    unlock: `Unlock ${subjectKind}`,
    force: 'Admin recovery',
    manage: 'Manage lock',
    'unlock-all': 'Unlock all'
  });

  async function run(fn) {
    if (busy) return;
    busy = true; err = '';
    try { await fn(); ondone(); onclose(); }
    catch (e) { err = e?.message || 'Something went wrong.'; }
    finally { busy = false; }
  }
  const doSet = () => {
    if (pw.length < 1) { err = 'Choose a password.'; return; }
    if (pw !== confirm) { err = 'Passwords don’t match.'; return; }
    run(() => isGroup ? lockGroup(group.name, pw) : lockCollection(collection.id, pw));
  };
  const doUnlock = () => run(() => isGroup ? unlockGroup(group.name, pw) : unlockCollection(collection.id, pw));
  const doForce = () => run(() => isGroup ? forceUnlockGroup(group.name, pw) : forceUnlockCollection(collection.id, pw));
  const doRelock = () => run(() => isGroup ? relockGroup(group.name) : relockCollection(collection.id));
  const doRemove = () => run(() => isGroup ? removeGroupLock(group.name, '') : removeCollectionLock(collection.id, ''));
  const doUnlockAll = () => {
    if (pw.length < 1) { err = 'Enter a password.'; return; }
    run(() => unlockAllCollections(pw));
  };

  function onkey(e) { if (e.key === 'Escape' && !busy) onclose(); }
  function submit(e) {
    e.preventDefault();
    if (m === 'set') doSet();
    else if (m === 'unlock') doUnlock();
    else if (m === 'force') doForce();
    else if (m === 'unlock-all') doUnlockAll();
  }
</script>

<svelte:window onkeydown={onkey} />

<div use:portal class="fixed inset-0 z-[80] grid place-items-center bg-[var(--overlay-strong)] p-4 backdrop-blur-sm" role="presentation"
     transition:fade={{ duration: 120 }} onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="relative w-full max-w-sm overflow-hidden rounded-2xl border border-line bg-[var(--surface-solid)] shadow-[0_30px_80px_-20px_rgba(0,0,0,0.7)]"
       role="dialog" aria-modal="true" aria-label={TITLES[m]} tabindex="-1" use:trapFocus transition:fly={{ y: 18, duration: 180 }}>
    <header class="flex items-center gap-3 border-b border-line px-5 py-4">
      <span class="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--accent)]/15 text-[var(--accent)]">
        <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      </span>
      <div class="min-w-0">
        <h2 class="text-base font-extrabold tracking-tight">{TITLES[m]}</h2>
        <p class="truncate text-sm text-muted">{m === 'unlock-all' ? 'Every collection or group with this password' : subject}</p>
      </div>
    </header>

    <div class="p-5">
      {#if m === 'manage'}
        {#if confirmRemove}
          <p class="mb-4 text-sm text-muted">Remove the password from this {subjectKind}? Its media will reappear everywhere.</p>
          <div class="flex justify-end gap-2">
            <button type="button" class="rounded-lg border border-line px-4 py-2 text-sm font-semibold" onclick={() => (confirmRemove = false)}>Back</button>
            <button type="button" class="rounded-lg bg-[var(--danger)] px-4 py-2 text-sm font-bold text-white disabled:opacity-50" disabled={busy} onclick={doRemove}>Remove password</button>
          </div>
        {:else}
          <p class="mb-4 text-sm text-muted">This {subjectKind} is unlocked. You can re-lock it now or remove its password entirely.</p>
          <div class="grid gap-2">
            <button type="button" class="inline-flex items-center justify-center gap-2 rounded-lg bg-[var(--accent)] px-4 py-2.5 text-sm font-bold text-[var(--on-accent)] disabled:opacity-50" disabled={busy} onclick={doRelock}>
              <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
              Lock now
            </button>
            <button type="button" class="rounded-lg border border-line px-4 py-2.5 text-sm font-semibold" onclick={() => (confirmRemove = true)}>Remove password…</button>
          </div>
        {/if}
      {:else}
        <form onsubmit={submit} class="space-y-3">
          {#if m === 'force'}
            <p class="text-sm text-muted">Forgot this {subjectKind}’s password? Enter your main admin password to unlock it for 24h.</p>
          {:else if m === 'unlock-all'}
            <p class="text-sm text-muted">Enter a password to unlock every collection or group that uses it, for 24h. Other locks stay sealed.</p>
          {/if}
          <input type="password" bind:value={pw} placeholder={m === 'force' ? 'Admin password' : 'Password'} autocomplete={m === 'set' ? 'new-password' : 'current-password'}
            class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2.5 text-sm outline-none focus:border-[var(--accent)]" />
          {#if m === 'set'}
            <input type="password" bind:value={confirm} placeholder="Confirm password" autocomplete="new-password"
              class="w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2.5 text-sm outline-none focus:border-[var(--accent)]" />
          {/if}
          {#if err}<p class="text-sm text-[var(--danger-ink-soft)]">{err}</p>{/if}
          <div class="flex items-center justify-between gap-2 pt-1">
            {#if m === 'unlock'}
              <button type="button" class="text-xs font-semibold text-muted hover:text-[var(--accent)]" onclick={() => { m = 'force'; err = ''; pw = ''; }}>Forgot? Use admin password</button>
            {:else if m === 'force'}
              <button type="button" class="text-xs font-semibold text-muted hover:text-[var(--accent)]" onclick={() => { m = 'unlock'; err = ''; pw = ''; }}>← Back</button>
            {:else}<span></span>{/if}
            <div class="flex gap-2">
              <button type="button" class="rounded-lg border border-line px-4 py-2 text-sm font-semibold" onclick={onclose}>Cancel</button>
              <button type="submit" class="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-bold text-[var(--on-accent)] disabled:opacity-50" disabled={busy}>
                {busy ? 'Working…' : (m === 'set' ? 'Lock' : m === 'unlock-all' ? 'Unlock all' : 'Unlock')}
              </button>
            </div>
          </div>
        </form>
      {/if}
    </div>
  </div>
</div>
