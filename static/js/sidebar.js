(function () {
  var toggle  = document.getElementById('sidebarToggle');
  var overlay = document.getElementById('sidebarOverlay');

  if (toggle) {
    toggle.addEventListener('click', function () {
      document.body.classList.toggle('sidebar-open');
    });
  }
  if (overlay) {
    overlay.addEventListener('click', function () {
      document.body.classList.remove('sidebar-open');
    });
  }

  var logoutBtn     = document.getElementById('logoutBtn');
  var logoutPopover = document.getElementById('logoutPopover');
  var logoutCancel  = document.getElementById('logoutCancel');
  var logoutConfirm = document.getElementById('logoutConfirm');

  if (logoutBtn && logoutPopover) {
    logoutBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = logoutPopover.classList.toggle('open');
      logoutBtn.classList.toggle('active', open);
    });

    document.addEventListener('click', function (e) {
      if (!logoutBtn.contains(e.target) && !logoutPopover.contains(e.target)) {
        logoutPopover.classList.remove('open');
        logoutBtn.classList.remove('active');
      }
    });

    if (logoutCancel) {
      logoutCancel.addEventListener('click', function () {
        logoutPopover.classList.remove('open');
        logoutBtn.classList.remove('active');
      });
    }

    if (logoutConfirm) {
      logoutConfirm.addEventListener('click', function () {
        document.getElementById('dashLayout').classList.add('signing-out');
        // Clear stored tokens
        localStorage.removeItem('wam_access_token');
        localStorage.removeItem('wam_refresh_token');
        setTimeout(function () { window.location.href = '/login'; }, 350);
      });
    }
  }
})();
