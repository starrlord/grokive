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
    primary: 'bg-[var(--accent)] font-bold text-[var(--on-accent)] enabled:hover:brightness-110 enabled:active:brightness-95',
    secondary: 'border border-line font-semibold enabled:hover:bg-[var(--surface-2)]',
    danger: 'bg-[var(--danger)] font-bold text-[var(--on-accent)] enabled:hover:bg-[var(--danger-hover)]'
  };
  const SIZES = { md: 'px-4 py-2', lg: 'px-4 py-2.5' };
  const base = 'rounded-lg transition disabled:opacity-50';
</script>

<button {type} {disabled} {onclick} class="{base} {SIZES[size]} {VARIANTS[variant]} {extra}" {...rest}>
  {@render children?.()}
</button>
