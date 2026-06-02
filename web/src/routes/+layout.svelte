<script>
  import '../app.css';
  import { onMount } from 'svelte';
  import { theme, mode } from '$lib/state.js';
  import { authStatus } from '$lib/api.js';
  import Login from '$lib/components/Login.svelte';

  let { children } = $props();

  let checked = $state(false);
  let authed = $state(true);

  // Reflect persisted prefs onto <html> on first paint.
  $effect(() => {
    document.documentElement.dataset.theme = $theme;
    document.documentElement.dataset.mode = $mode;
  });

  onMount(async () => {
    const s = await authStatus();
    authed = !s.auth_required || s.authed;
    checked = true;
  });
</script>

{#if !checked}
  <div class="grid min-h-[100dvh] place-items-center text-sm text-muted">Loading…</div>
{:else if !authed}
  <Login onLoggedIn={() => (authed = true)} />
{:else}
  {@render children()}
{/if}
