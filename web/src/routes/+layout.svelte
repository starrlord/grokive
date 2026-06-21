<script>
  import '../app.css';
  import { onMount } from 'svelte';
  import { theme, mode, settings, loadSettings, subtitleCueRule, captionVideoHeight } from '$lib/state.js';
  import { authStatus } from '$lib/api.js';
  import Login from '$lib/components/Login.svelte';

  let { children } = $props();

  let checked = $state(false);
  let authed = $state(true);

  // Reflect persisted prefs onto <html> on first paint.
  $effect(() => {
    document.documentElement.dataset.theme = $theme;
    document.documentElement.dataset.mode = $mode;
    const meta = document.querySelector('meta[name="theme-color"]');
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
    if (meta && bg) meta.setAttribute('content', bg);
  });

  // Apply the subtitle display style app-wide by rewriting a single injected
  // <style> with a literal video::cue rule whenever the settings — or the active
  // video's rendered height (for device-consistent px sizing) — change.
  $effect(() => {
    const rule = subtitleCueRule($settings, $captionVideoHeight);
    let el = document.getElementById('grok-sub-cue');
    if (!el) {
      el = document.createElement('style');
      el.id = 'grok-sub-cue';
      document.head.appendChild(el);
    }
    el.textContent = rule;
  });

  onMount(async () => {
    const s = await authStatus();
    authed = !s.auth_required || s.authed;
    checked = true;
    // Pull the saved settings (incl. subtitle style) once at startup so the cue
    // rule reflects the user's choices before any player opens.
    loadSettings();
  });
</script>

{#if !checked}
  <div class="grid min-h-[100dvh] place-items-center text-sm text-muted">Loading…</div>
{:else if !authed}
  <Login onLoggedIn={() => (authed = true)} />
{:else}
  {@render children()}
{/if}
