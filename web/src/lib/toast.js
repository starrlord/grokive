// Lightweight transient notifications. Call `toast('Done', { type: 'success' })`
// from anywhere; a single <Toaster /> (mounted in +page.svelte) renders the stack.
import { writable } from 'svelte/store';

export const toasts = writable([]);
let nextId = 1;

export function toast(message, opts = {}) {
  const id = nextId++;
  const duration = opts.duration ?? 3500;
  toasts.update((list) => [...list, { id, message, type: opts.type || 'info' }]);
  if (duration > 0) setTimeout(() => dismiss(id), duration);
  return id;
}

export function dismiss(id) {
  toasts.update((list) => list.filter((t) => t.id !== id));
}
