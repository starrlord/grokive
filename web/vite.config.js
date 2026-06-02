import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    // During `vite dev`, proxy the API to the running Flask server so the SPA
    // can hit real data without CORS.
    proxy: {
      '/api': 'http://localhost:8080',
      '/media': 'http://localhost:8080',
      '/thumbnails': 'http://localhost:8080'
    }
  }
});
