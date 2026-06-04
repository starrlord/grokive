<script>
  // Colourful drifting-bokeh + aurora particle field on a single <canvas> that
  // fills its positioned parent. Used twice in GenerateMovie: a full-viewport
  // backdrop (large soft parallax bokeh + colour aurora) and a much subtler
  // in-panel field that throws sparks off the progress bar's leading edge, plus
  // a one-shot celebratory burst when a render finishes. Pauses when unmounted,
  // honours prefers-reduced-motion, and is devicePixelRatio-aware.
  import { onMount } from 'svelte';

  // Vibrant festival palette — warm + cool so the field never reads monochrome.
  const COLORFUL = ['#22d3ee', '#f472b6', '#a78bfa', '#fbbf24', '#34d399', '#60a5fa', '#fb7185'];

  let {
    active = true,            // emit progress sparks while true
    animate = true,           // when false, settle to a static frozen frame (no perpetual paint)
    layers = 2,               // parallax depth bands
    intensity = 1,            // bokeh density multiplier
    count = null,             // explicit ember count (overrides area formula — for tiny fields)
    scale = 1,                // bokeh size multiplier (small for the in-panel field)
    palette = COLORFUL,       // bokeh colours
    aurora = false,           // draw the slow breathing colour wash
    auroraColors = null,      // override wash colours (defaults to palette)
    auroraAlpha = 0.28,       // wash strength — keep low behind text
    emitEl = null,            // progress-bar element; sparks spawn along its width
    emitAt = 0,               // 0..1 leading-edge position along emitEl
    burst = 0,                // increment this to fire a multi-colour centre burst
    class: klass = ''
  } = $props();

  let canvas;

  onMount(() => {
    const ctx = canvas.getContext('2d');
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    const rgb = palette.map(hexRgb);
    const sprites = rgb.map(makeSprite);

    let W = 0, H = 0, dpr = 1;
    let embers = [];
    let sparks = [];
    let raf = 0, last = 0, lastBurst = burst;
    // Animation clock that only advances while animating, so embers + aurora
    // freeze in place when `animate` goes false. `idleDrawn` lets the loop paint
    // exactly one settled frame and then go quiet (zero canvas repaints → the
    // backdrop-blur over us stops re-rasterizing) until there's motion again.
    let aclock = 0, idleDrawn = false;

    // Aurora blobs: a few large, slow, additive radial gradients that wander.
    const aurRgb = (auroraColors || palette).map(hexRgb);
    const aur = aurora
      ? Array.from({ length: 3 }, (_, i) => ({
          ...aurRgb[(i * 2) % aurRgb.length],
          sx: 0.05 + Math.random() * 0.05,
          sy: 0.04 + Math.random() * 0.05,
          ph: Math.random() * 6.283
        }))
      : [];

    function hexRgb(h) {
      const n = parseInt(h.slice(1), 16);
      return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
    }

    function makeSprite({ r, g: gg, b }) {
      // Soft bokeh profile: faint white sparkle core fading smoothly to colour
      // then transparent — reads as an out-of-focus glowing orb, not a hard dot.
      const s = document.createElement('canvas');
      s.width = s.height = 96;
      const c = s.getContext('2d');
      const rg = c.createRadialGradient(48, 48, 0, 48, 48, 48);
      rg.addColorStop(0, 'rgba(255,255,255,0.6)');
      rg.addColorStop(0.18, `rgba(${r},${gg},${b},0.85)`);
      rg.addColorStop(0.5, `rgba(${r},${gg},${b},0.32)`);
      rg.addColorStop(1, `rgba(${r},${gg},${b},0)`);
      c.fillStyle = rg;
      c.fillRect(0, 0, 96, 96);
      return s;
    }

    function resize() {
      const r = canvas.getBoundingClientRect();
      W = r.width; H = r.height;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.round(W * dpr));
      canvas.height = Math.max(1, Math.round(H * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      seed();
      idleDrawn = false; // canvas was cleared by the resize — repaint one frame
    }

    function seed() {
      const n = count != null ? count : Math.max(10, Math.min(130, Math.round((intensity * W * H) / 9000)));
      embers = Array.from({ length: n }, () => spawn(Math.random() * H));
    }

    function spawn(y) {
      // Discrete depth bands give a clean parallax read (near = big/fast/bright).
      const band = layers > 1 ? Math.floor(Math.random() * layers) / (layers - 1) : Math.random();
      return {
        x: Math.random() * W,
        y,
        r: (3 + band * 17 + Math.random() * 4) * scale,
        ci: (Math.random() * sprites.length) | 0,
        vy: 5 + band * 20 + Math.random() * 9,
        amp: 4 + band * 14,
        sw: 0.3 + Math.random() * 0.7,
        ph: Math.random() * 6.283,
        a: 0.16 + band * 0.4
      };
    }

    function emitSparks(dt) {
      if (!emitEl) return;
      const cr = canvas.getBoundingClientRect();
      const er = emitEl.getBoundingClientRect();
      const x = er.left - cr.left + Math.max(0, Math.min(1, emitAt)) * er.width;
      const y = er.top - cr.top + er.height / 2;
      const n = Math.random() < dt * 70 ? 2 : 1;
      for (let i = 0; i < n; i++) {
        const ang = -Math.PI / 2 + (Math.random() - 0.5) * 1.5;
        const sp = 45 + Math.random() * 100;
        sparks.push({ x, y, vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp,
          life: 0, ttl: 0.5 + Math.random() * 0.55, r: 2 + Math.random() * 3,
          ci: (Math.random() * sprites.length) | 0 });
      }
    }

    function fireBurst() {
      const cx = W / 2, cy = H / 2;
      for (let i = 0; i < 90; i++) {
        const ang = Math.random() * 6.283, sp = 70 + Math.random() * 300;
        sparks.push({ x: cx, y: cy, vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp,
          life: 0, ttl: 0.9 + Math.random() * 1.0, r: 2.5 + Math.random() * 3.5,
          ci: (Math.random() * sprites.length) | 0 });
      }
    }

    function drawAurora(t) {
      ctx.globalCompositeOperation = 'lighter';
      const rad = Math.max(W, H) * 0.6;
      for (const a of aur) {
        const x = (0.5 + 0.45 * Math.sin(t * a.sx + a.ph)) * W;
        const y = (0.5 + 0.45 * Math.cos(t * a.sy + a.ph)) * H;
        const g = ctx.createRadialGradient(x, y, 0, x, y, rad);
        g.addColorStop(0, `rgba(${a.r},${a.g},${a.b},${auroraAlpha})`);
        g.addColorStop(1, `rgba(${a.r},${a.g},${a.b},0)`);
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, W, H);
      }
    }

    function drawSprite(p, alpha, r) {
      ctx.globalAlpha = alpha;
      ctx.drawImage(sprites[p.ci], p.x - r, p.y - r, r * 2, r * 2);
    }

    function frame(ts) {
      raf = requestAnimationFrame(frame);
      const dt = Math.min(0.05, (ts - last) / 1000 || 0);
      last = ts;

      if (burst !== lastBurst) { lastBurst = burst; fireBurst(); }

      // Nothing moving and we've already painted the settled frame → skip all
      // drawing so the canvas (and the blur layered over it) stays idle.
      const idle = !animate && sparks.length === 0;
      if (idle && idleDrawn) return;

      if (animate) aclock += dt;
      const t = aclock;

      ctx.clearRect(0, 0, W, H);
      if (aur.length) drawAurora(t);

      ctx.globalCompositeOperation = 'lighter';
      for (const p of embers) {
        if (animate) {
          p.y -= p.vy * dt;
          p.x += Math.sin(t * p.sw + p.ph) * p.amp * dt;
          if (p.y < -p.r * 2) { Object.assign(p, spawn(H + p.r * 2)); }
        }
        drawSprite(p, p.a, p.r);
      }

      if (active) emitSparks(dt);
      for (let i = sparks.length - 1; i >= 0; i--) {
        const s = sparks[i];
        s.life += dt;
        if (s.life >= s.ttl) { sparks.splice(i, 1); continue; }
        s.vy += 60 * dt;                 // gentle gravity
        s.x += s.vx * dt; s.y += s.vy * dt;
        const k = 1 - s.life / s.ttl;
        drawSprite(s, k, s.r * (0.6 + k));
      }

      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
      idleDrawn = idle; // just painted the settled frame; go quiet until motion resumes
    }

    function staticFrame() {
      ctx.clearRect(0, 0, W, H);
      if (aur.length) drawAurora(0);
      ctx.globalCompositeOperation = 'lighter';
      for (const p of embers) drawSprite(p, p.a, p.r);
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }

    const ro = new ResizeObserver(() => { resize(); if (reduce) staticFrame(); });
    ro.observe(canvas);
    resize();
    if (reduce) staticFrame();
    else { last = performance.now(); raf = requestAnimationFrame(frame); }

    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  });
</script>

<canvas bind:this={canvas} class={klass} aria-hidden="true"></canvas>
