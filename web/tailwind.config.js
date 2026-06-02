/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{html,js,svelte}'],
  theme: {
    extend: {
      colors: {
        // Map Tailwind color utilities onto our themeable CSS variables so
        // bg-surface / text-ink / border-line respond to theme + mode.
        bg: 'var(--bg)',
        surface: 'var(--surface)',
        'surface-2': 'var(--surface-2)',
        ink: 'var(--ink)',
        muted: 'var(--muted)',
        line: 'var(--line)',
        accent: 'var(--accent)'
      },
      borderRadius: { card: 'var(--radius)' }
    }
  },
  plugins: []
};
