/* Mobile sidebar drawer (≤900px). The desktop sidebar is always visible.
   Opened by #sidebarToggle; closed by the scrim, Esc, or following a nav link. */
(function () {
  var toggle  = document.getElementById('sidebarToggle');
  var overlay = document.getElementById('sidebarOverlay');
  var sidebar = document.getElementById('sidebar');

  function setOpen(open) {
    document.body.classList.toggle('sidebar-open', open);
    if (toggle) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    }
    if (open && sidebar) {
      var current = sidebar.querySelector('[aria-current="page"]') || sidebar.querySelector('a');
      if (current) current.focus();
    } else if (!open && toggle && sidebar && sidebar.contains(document.activeElement)) {
      toggle.focus();
    }
  }

  if (toggle) {
    toggle.addEventListener('click', function () {
      setOpen(!document.body.classList.contains('sidebar-open'));
    });
  }
  if (overlay) overlay.addEventListener('click', function () { setOpen(false); });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) setOpen(false);
  });

  // Back to desktop width with the drawer open: drop the open state (and its scroll lock).
  window.addEventListener('resize', function () {
    if (window.innerWidth > 900 && document.body.classList.contains('sidebar-open')) setOpen(false);
  });
})();
