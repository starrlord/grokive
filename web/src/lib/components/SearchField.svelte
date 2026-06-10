<script>
  // A search / filter text input with an inline clear (×) button that appears once there's
  // text. `value` is bindable so callers keep using `bind:value`; `oninput` / `onkeydown`
  // are forwarded to the input. `onclear` fires AFTER the value is emptied — TopBar uses it
  // to push the cleared query immediately instead of waiting on its debounce; inputs that
  // derive purely from the bound value (most filters) don't need it.
  //
  // Layout: pass outer sizing (width / flex / order / margin) via `wrapperClass` and the
  // input's own look (border, bg, padding, text) via `inputClass`. Leave right room for the
  // button — `inputClass` should include `pr-10`.
  let {
    value = $bindable(''),
    placeholder = '',
    inputClass = '',
    wrapperClass = '',
    oninput,
    onkeydown,
    onclear,
    ariaLabel = 'search'
  } = $props();

  let el = $state(null);
  function clear() {
    value = '';
    onclear?.();
    el?.focus(); // keep the caret in the field so the user can keep typing
  }
</script>

<div class="relative {wrapperClass}">
  <input bind:this={el} bind:value type="search" {placeholder} {oninput} {onkeydown} class="w-full {inputClass}" />
  {#if value}
    <button type="button" onclick={clear} title="Clear" aria-label="Clear {ariaLabel}"
      class="absolute right-2 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded-full text-muted transition hover:bg-[var(--surface-solid)] hover:text-ink">
      <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>
    </button>
  {/if}
</div>

<style>
  /* Hide the browser's native search-cancel button so the field never shows two × icons. */
  input::-webkit-search-cancel-button { -webkit-appearance: none; appearance: none; }
</style>
