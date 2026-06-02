import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
export default {
  preprocess: vitePreprocess(),
  kit: {
    // Single-page-app: one static shell, all routing client-side. Served by Flask.
    adapter: adapter({ fallback: 'index.html', strict: false })
  }
};
