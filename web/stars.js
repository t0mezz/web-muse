// stars.js — zero-dependency recreation of the React + motion starfield.
// Plain classic script: no imports, no exports, no frameworks.
// Load with <script src="./stars.js"></script>, then use window.Stars.
//
// Effect being recreated:
//   - Three star layers (1000x1px, 400x2px, 200x3px) scrolling upward in a
//     seamless -2000px loop, with durations speed / speed*2 / speed*3.
//   - Mouse parallax: the field shifts opposite the cursor by `factor`
//     through a spring (stiffness/damping), like motion's useSpring.
//   - Deep-space backdrop: radial gradient, light at the bottom.
//
// web-muse adaptations (marked ADAPT below):
//   - Backdrop gradient matched to the app theme (#0c0e12 base).
//   - Mouse listener on window: our container is a fixed fullscreen
//     backdrop behind the app, so it never receives mouse events itself.
//
// Usage:
//   <script src="./stars.js"></script>
//   <script>
//     const destroy = Stars.createStarsBackground(document.getElementById('sky'), {
//       factor: 0.05, speed: 50, stiffness: 50, damping: 20, starColor: '#fff',
//     });
//     // later: destroy();
//   </script>

(function (root) {
  'use strict';

  /**
   * Build a CSS box-shadow list of `count` stars scattered over a
   * 4000x4000px area, each rendered in `starColor`.
   * Mirrors generateStars() from the original component exactly
   * (Math.floor(Math.random() * 4000) - 2000 per axis).
   */
  function themeColor(key, fallback) {
    var T = root.WebMuseTheme;
    if (T) return T.get(key, fallback);
    return fallback;
  }

  function generateStars(count, starColor) {
    if (starColor === undefined) starColor = themeColor('star', '#4C4541');
    const shadows = [];
    for (let i = 0; i < count; i++) {
      const x = Math.floor(Math.random() * 4000) - 2000;
      const y = Math.floor(Math.random() * 4000) - 2000;
      shadows.push(x + 'px ' + y + 'px ' + starColor);
    }
    return shadows.join(', ');
  }

  /**
   * One semi-implicit Euler spring integration step (mass = 1, matching
   * motion's useSpring defaults). Mutates and returns `state` ({ x, v }).
   */
  function springStep(state, target, stiffness, damping, dt) {
    const force = -stiffness * (state.x - target) - damping * state.v;
    state.v += force * dt;
    state.x += state.v * dt;
    return state.x;
  }

  /**
   * Create one scrolling star layer inside `parent`.
   * Two identical dot-fields are stacked 2000px apart and translated
   * 0 -> -2000px on a linear loop, so the wrap is seamless.
   * Returns { element, destroy }.
   */
  function createStarLayer(parent, options) {
    options = options || {};
    const count = options.count !== undefined ? options.count : 1000;
    const size = options.size !== undefined ? options.size : 1;
    const duration = options.duration !== undefined ? options.duration : 50;
    const starColor = options.starColor !== undefined ? options.starColor : themeColor('star', '#4C4541');

    const layer = document.createElement('div');
    layer.setAttribute('data-slot', 'star-layer');
    layer.style.position = 'absolute';
    layer.style.top = '0';
    layer.style.left = '0';
    layer.style.width = '100%';
    layer.style.height = '2000px';
    layer.style.willChange = 'transform';

    const boxShadow = generateStars(count, starColor);

    const makeDots = (top) => {
      const dots = document.createElement('div');
      dots.style.position = 'absolute';
      dots.style.top = top;
      dots.style.left = '0';
      dots.style.width = size + 'px';
      dots.style.height = size + 'px';
      dots.style.background = 'transparent';
      dots.style.borderRadius = '9999px';
      dots.style.boxShadow = boxShadow;
      return dots;
    };

    layer.appendChild(makeDots('0'));
    layer.appendChild(makeDots('2000px'));
    parent.appendChild(layer);

    const start = performance.now();
    let raf = 0;
    const tick = (now) => {
      const y = -(((now - start) / 1000 % duration) / duration) * 2000;
      layer.style.transform = 'translate3d(0, ' + y + 'px, 0)';
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return {
      element: layer,
      destroy() {
        cancelAnimationFrame(raf);
        layer.remove();
      },
    };
  }

  // Default per-layer star counts (far 1px -> near 3px) and backdrop
  // paint. ADAPT: gradient matched to the app theme (starBg0 -> starBg1).
  // The fallback literals below are the current theme.js defaults; the
  // live theme values are read at each call (never cached at load), so a
  // later theme.apply is picked up by the next field created.
  const DEFAULT_COUNTS = [1000, 400, 200];
  function defaultBackground() {
    return 'radial-gradient(ellipse at bottom, ' +
      themeColor('starBg0', '#F7DAA2') + ' 0%, ' +
      themeColor('starBg1', '#FCF0DA') + ' 100%)';
  }

  /**
   * Turn `container` into the full starfield backdrop.
   * Own content already inside the container stays put and paints above
   * the field (same as passing `children` in the React version).
   *
   * Positioning and stacking come from the caller's CSS (e.g. a fixed
   * fullscreen `#stars-bg` rule): only the backdrop paint and clipping
   * are set here, so callers never have to patch inline styles back.
   *
   * Options mirror StarsBackgroundProps, plus local tuning knobs:
   *   factor (0.05)     mouse-parallax strength, px shift per px off-center
   *   speed (50)        base layer loop duration in seconds (others x2, x3)
   *   stiffness (50)    spring stiffness, as in useSpring({stiffness, damping})
   *   damping (20)      spring damping
   *   starColor (theme star / '#4C4541')
   *   counts ([1000, 400, 200])  per-layer star counts, far -> near
   *   opacity (1)       field transparency, applied to the parallax wrapper
   *   background (theme gradient)  container backdrop paint
   *   pointerEvents (true)  false -> field ignores mouse events
   *
   * Returns a destroy() function that stops all animation, removes the
   * field, and restores the container's previous inline styles.
   */
  function createStarsBackground(container, options) {
    options = options || {};
    const factor = options.factor !== undefined ? options.factor : 0.05;
    const speed = options.speed !== undefined ? options.speed : 50;
    const stiffness = options.stiffness !== undefined ? options.stiffness : 50;
    const damping = options.damping !== undefined ? options.damping : 20;
    const starColor = options.starColor !== undefined ? options.starColor : themeColor('star', '#4C4541');
    const counts = options.counts !== undefined ? options.counts : DEFAULT_COUNTS;
    const opacity = options.opacity !== undefined ? options.opacity : 1;
    const background =
      options.background !== undefined ? options.background : defaultBackground();
    const pointerEvents = options.pointerEvents !== undefined ? options.pointerEvents : true;

    const previous = {
      overflow: container.style.overflow,
      background: container.style.background,
    };
    container.style.overflow = 'hidden';
    container.style.background = background;
    container.setAttribute('data-slot', 'stars-background');

    const parallax = document.createElement('div');
    parallax.setAttribute('data-slot', 'stars-parallax');
    parallax.style.willChange = 'transform';
    parallax.style.opacity = String(opacity);
    if (!pointerEvents) parallax.style.pointerEvents = 'none';
    container.prepend(parallax);

    // Far -> near: 1px, 2px, 3px dots looping over speed, speed*2, speed*3.
    const layers = [0, 1, 2].map((i) =>
      createStarLayer(parallax, {
        count: counts[i] !== undefined ? counts[i] : DEFAULT_COUNTS[i],
        size: i + 1,
        duration: speed * (i + 1),
        starColor,
      }),
    );

    // Spring state. Motion values start at 1, so the field does too.
    const stateX = { x: 1, v: 0 };
    const stateY = { x: 1, v: 0 };
    let targetX = 1;
    let targetY = 1;

    const handleMouseMove = (e) => {
      targetX = -(e.clientX - window.innerWidth / 2) * factor;
      targetY = -(e.clientY - window.innerHeight / 2) * factor;
    };
    // ADAPT: listen on window, not the container. Our container is a fixed
    // fullscreen backdrop behind the app UI, so mouse events over the app
    // never reach it; coordinates are identical for a fullscreen field.
    window.addEventListener('mousemove', handleMouseMove);

    let raf = 0;
    let last = -1;
    const tick = (now) => {
      if (last < 0) last = now;
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      springStep(stateX, targetX, stiffness, damping, dt);
      springStep(stateY, targetY, stiffness, damping, dt);
      parallax.style.transform =
        'translate3d(' + stateX.x + 'px, ' + stateY.x + 'px, 0)';
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return function destroy() {
      cancelAnimationFrame(raf);
      window.removeEventListener('mousemove', handleMouseMove);
      for (const layer of layers) layer.destroy();
      parallax.remove();
      container.style.overflow = previous.overflow;
      container.style.background = previous.background;
      container.removeAttribute('data-slot');
    };
  }

  root.Stars = {
    generateStars,
    springStep,
    createStarLayer,
    createStarsBackground,
  };
})(typeof window !== 'undefined' ? window : globalThis);
