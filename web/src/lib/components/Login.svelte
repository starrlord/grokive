<script>
  import { login } from '$lib/api.js';

  let { onLoggedIn = () => {} } = $props();
  let username = $state('');
  let password = $state('');
  let error = $state('');
  let busy = $state(false);

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    busy = true;
    error = '';
    const r = await login(username, password);
    busy = false;
    if (r.ok) onLoggedIn();
    else error = r.error;
  }
</script>

<div class="relative grid min-h-[100dvh] place-items-center overflow-hidden px-4">
  <!-- ambient accent glow -->
  <div class="pointer-events-none absolute -top-1/3 left-1/2 h-[80vmax] w-[80vmax] -translate-x-1/2 rounded-full opacity-40 blur-3xl"
       style="background: radial-gradient(closest-side, var(--accent), transparent 70%)"></div>

  <form class="panel relative w-full max-w-sm rounded-2xl p-7" onsubmit={submit}>
    <div class="mb-6 text-center">
      <div class="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-xl bg-[var(--accent)] text-2xl font-black text-white shadow-lg">◆</div>
      <h1 class="text-xl font-extrabold tracking-tight">Grokive</h1>
      <p class="mt-1 text-sm text-muted">Sign in to continue</p>
    </div>

    <label class="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted" for="ga-user">Username</label>
    <input id="ga-user" class="mb-4 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2.5 outline-none transition focus:border-[var(--accent)]"
      type="text" autocomplete="username" bind:value={username} />

    <label class="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted" for="ga-pass">Password</label>
    <input id="ga-pass" class="mb-4 w-full rounded-lg border border-line bg-[var(--surface-2)] px-3 py-2.5 outline-none transition focus:border-[var(--accent)]"
      type="password" autocomplete="current-password" bind:value={password} />

    {#if error}<p class="mb-3 text-sm text-red-400">{error}</p>{/if}

    <button type="submit" disabled={busy}
      class="w-full rounded-lg bg-[var(--accent)] py-2.5 font-bold text-white shadow-lg transition hover:opacity-90 disabled:opacity-60">
      {busy ? 'Signing in…' : 'Sign in'}
    </button>
  </form>
</div>
