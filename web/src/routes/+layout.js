// Pure client-side SPA: no SSR, no prerender crawling. The adapter emits a
// single fallback shell that Flask serves for any route.
export const ssr = false;
export const prerender = false;
