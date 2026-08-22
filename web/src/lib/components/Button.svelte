<script>
  // Shared button vocabulary so the primary/secondary/danger controls stop drifting
  // in padding/weight/colour across dialogs. Variants map to the token palette; the
  // `class` prop is appended for per-call tweaks (width, margin, etc.). Anything not
  // named here (icon-only glyph buttons, SelectBar's stateful dock) keeps its own CSS.
  let {
    variant = 'primary', // 'primary' | 'secondary' | 'danger'
    size = 'md', // 'md' (px-4 py-2) | 'lg' (px-4 py-2.5)
    type = 'button',
    disabled = false,
    onclick = () => {},
    class: extra = '',
    children,
    ...rest
  } = $props();

  const VARIANTS = {
    // Solid variants carry a transparent border so every variant shares box metrics —
    // without it, bordered and borderless buttons in the same row/stack differ by 2px.
    primary: 'cta-primary border border-transparent bg-[var(--accent)] font-bold text-[var(--on-accent)] enabled:hover:brightness-110 enabled:active:brightness-95',
    secondary: 'border border-line font-semibold enabled:hover:bg-[var(--surface-2)]',
    danger: 'border border-transparent bg-[var(--danger)] font-bold text-[var(--on-accent)] enabled:hover:bg-[var(--danger-hover)]',
    // Destructive-but-not-shouting: red border + red text, tint on hover — the SelectBar's
    // delete-button vocabulary. Use when a dialog already has a solid CTA above it, so
    // only ONE saturated block competes for the eye.
    'danger-outline': 'border border-[color-mix(in_srgb,var(--danger)_55%,transparent)] font-semibold text-[var(--danger-ink)] enabled:hover:border-[var(--danger)] enabled:hover:bg-[color-mix(in_srgb,var(--danger)_12%,transparent)]'
  };
  const SIZES = { md: 'px-4 py-2', lg: 'px-4 py-2.5' };
  const base = 'rounded-lg transition disabled:opacity-50';
</script>

<button {type} {disabled} {onclick} class="{base} {SIZES[size]} {VARIANTS[variant]} {extra}" {...rest}>
  {@render children?.()}
</button>
