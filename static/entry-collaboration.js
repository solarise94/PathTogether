/* Illustration of the existing share/annotation/note workflow. No app requests. */
(function () {
  'use strict';
  var root = document.querySelector('[data-collab-demo]');
  if (!root) return;
  var buttons = Array.from(root.querySelectorAll('[data-collab-step]'));
  var toggle = root.querySelector('[data-collab-toggle]');
  var replay = root.querySelector('[data-collab-replay]');
  var status = root.querySelector('[data-collab-status]');
  var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var step = motion.matches ? 2 : 0;
  var paused = motion.matches;
  var visible = !('IntersectionObserver' in window);
  var timer = null;
  function text(key) { return window.HP_I18N.t('entry.collab.' + key); }
  function paint() {
    root.dataset.step = String(step);
    root.dataset.paused = String(paused);
    buttons.forEach(function (button, i) {
      button.setAttribute('aria-pressed', String(i === step));
    });
    toggle.dataset.i18n = 'entry.collab.' + (paused ? 'play' : 'pause');
    status.dataset.i18n = 'entry.collab.phase.' + step;
    toggle.textContent = text(paused ? 'play' : 'pause');
    toggle.setAttribute('aria-label', text(paused ? 'play.aria' : 'pause.aria'));
    status.textContent = text('phase.' + step);
  }
  function schedule() {
    clearTimeout(timer); timer = null;
    if (paused || !visible || document.hidden) return;
    timer = setTimeout(function () {
      step = (step + 1) % buttons.length;
      paint(); schedule();
    }, step === 2 ? 7000 : 5500);
  }
  buttons.forEach(function (button, index) {
    button.addEventListener('click', function () {
      step = index; paused = true; paint(); schedule();
    });
  });
  toggle.addEventListener('click', function () { paused = !paused; paint(); schedule(); });
  replay.addEventListener('click', function () {
    step = 0; paused = motion.matches; paint(); schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  document.addEventListener('hp-lang-change', paint);
  motion.addEventListener('change', function () {
    if (motion.matches) { paused = true; step = 2; }
    paint(); schedule();
  });
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (entries) {
      visible = entries[0].isIntersecting;
      schedule();
    }, { threshold: 0.15 }).observe(root);
  }
  paint(); schedule();
})();
