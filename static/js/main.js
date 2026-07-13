/* BookHub — main.js. No ripple/spark animations. */

/* ── Preserve scroll position across plain form POSTs (add/remove/reserve
   a book etc.) so the page doesn't snap back to the top after a redirect. ── */
document.addEventListener('submit', function (e) {
  const form = e.target;
  if (form.classList && form.classList.contains('js-preserve-scroll')) {
    try { sessionStorage.setItem('lms_scroll_pos', String(window.scrollY)); } catch (err) {}
  }
});
(function () {
  try {
    const y = sessionStorage.getItem('lms_scroll_pos');
    if (y !== null) {
      sessionStorage.removeItem('lms_scroll_pos');
      window.addEventListener('load', function () { window.scrollTo(0, parseInt(y, 10)); });
    }
  } catch (e) {}
})();

document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.alert').forEach(function (el) {
    setTimeout(function () {
      el.style.transition = 'opacity 0.4s';
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 400);
    }, 4500);
  });
  document.addEventListener('click', function (e) {
    const sb = document.getElementById('sidebar');
    const toggle = document.getElementById('mobile-toggle');
    if (!sb) return;
    if (sb.classList.contains('open') && !sb.contains(e.target) && toggle && !toggle.contains(e.target)) {
      sb.classList.remove('open');
    }
  });
  document.addEventListener('click', function (e) {
    const wrap = document.getElementById('profile-menu-wrap');
    if (!wrap) return;
    if (!wrap.contains(e.target)) closeProfileMenu();
  });
  const sidebarEl = document.getElementById('sidebar');
  if (sidebarEl) {
    sidebarEl.addEventListener('click', function (e) {
      // Nav links navigate, the toggle button has its own handler —
      // everywhere else on the sidebar (empty space, gaps) collapses/expands it.
      if (e.target.closest('.nav-item') || e.target.closest('.sidebar-toggle-icon') || e.target.closest('.sidebar-profile')) return;
      toggleSidebarCollapse();
    });
  }
});
function toggleSidebar() { const sb = document.getElementById('sidebar'); if (sb) sb.classList.toggle('open'); }

/* ── Sidebar folder-style nav groups (Delivery, Payments, etc.) ── */
function toggleNavGroup(e, btn) {
  if (e) e.preventDefault();
  const group = btn.closest('.nav-group');
  if (!group) return;
  const open = group.classList.toggle('open');
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

/* ── Sidebar collapse/expand (desktop) ── */
function toggleSidebarCollapse() {
  const collapsed = document.body.classList.toggle('sidebar-collapsed');
  try { localStorage.setItem('lms_sidebar_collapsed', collapsed ? '1' : '0'); } catch (e) {}
  _updateSidebarCollapseIcon(collapsed);
}
function _updateSidebarCollapseIcon(collapsed) {
  document.querySelectorAll('.sidebar-toggle-icon i').forEach(function (i) {
    i.className = collapsed ? 'ti ti-layout-sidebar-left-expand' : 'ti ti-layout-sidebar-left-collapse';
  });
  document.querySelectorAll('.sidebar-toggle-icon').forEach(function (btn) {
    btn.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  });
}
(function () {
  try {
    if (localStorage.getItem('lms_sidebar_collapsed') === '1') {
      document.body.classList.add('sidebar-collapsed');
    }
  } catch (e) {}
})();
document.addEventListener('DOMContentLoaded', function () {
  _updateSidebarCollapseIcon(document.body.classList.contains('sidebar-collapsed'));
});
/* ── Dark mode / light mode toggle ── */
function _updateThemeIcon(isDark) {
  document.querySelectorAll('.theme-toggle-icon').forEach(function (i) {
    i.className = 'ti theme-toggle-icon ' + (isDark ? 'ti-sun' : 'ti-moon');
  });
  document.querySelectorAll('.theme-toggle-btn').forEach(function (btn) {
    btn.setAttribute('aria-label', isDark ? 'Switch to light mode' : 'Switch to dark mode');
  });
}
function toggleTheme() {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  if (next === 'dark') { document.documentElement.setAttribute('data-theme', 'dark'); }
  else { document.documentElement.removeAttribute('data-theme'); }
  try { localStorage.setItem('lms_theme', next); } catch (e) {}
  _updateThemeIcon(next === 'dark');
}
document.addEventListener('DOMContentLoaded', function () {
  _updateThemeIcon(document.documentElement.getAttribute('data-theme') === 'dark');
});

function toggleProfile(e) { if (e) e.preventDefault(); const ov = document.getElementById('profile-overlay'); if (ov) ov.classList.toggle('open'); }
function closeProfile() { const ov = document.getElementById('profile-overlay'); if (ov) ov.classList.remove('open'); }
function closeProfileOutside(e) { if (e.target === document.getElementById('profile-overlay')) closeProfile(); }

/* ── Avatar → small popover menu (Profile settings / Membership / Logout) ── */
function toggleProfileMenu(e) {
  if (e) e.preventDefault();
  const menu = document.getElementById('profile-dropdown');
  if (menu) menu.classList.toggle('open');
}
function closeProfileMenu() {
  const menu = document.getElementById('profile-dropdown');
  if (menu) menu.classList.remove('open');
}
function openProfilePanel(e) {
  if (e) e.preventDefault();
  closeProfileMenu();
  const ov = document.getElementById('profile-overlay');
  if (ov) ov.classList.add('open');
}

document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape') return;
  closeProfile();
  closeProfileMenu();
  closeFormModal();
  closeConfirmModal();
});

/* ── Profile form: submit via AJAX so it never navigates away from
   the current page. Route must return JSON when asked for it. ── */
function submitProfileForm(e, form) {
  e.preventDefault();
  const btn = form.querySelector('button[type="submit"]');
  const original = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }

  fetch(form.action, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    body: new FormData(form),
  })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      showInlineMessage(form, data.message, data.success ? 'success' : 'danger');
      if (data.success) {
        // Reflect the new name/initials in the avatar + sidebar without navigating.
        // Some avatars (members) have a crown badge as a sibling element inside
        // .user-avatar — target .avatar-initials if present so textContent
        // doesn't wipe it out; fall back to the avatar itself otherwise.
        document.querySelectorAll('.user-avatar').forEach(function (av) {
          if (!data.initials) return;
          var initialsEl = av.querySelector('.avatar-initials') || av;
          initialsEl.textContent = data.initials;
        });
        const nameEl = form.closest('.profile-panel').querySelector('.profile-name');
        if (nameEl && data.name) nameEl.textContent = data.name;
      }
    })
    .catch(function () { showInlineMessage(form, 'Something went wrong. Please try again.', 'danger'); })
    .finally(function () { if (btn) { btn.disabled = false; btn.textContent = original; } });
}
function showInlineMessage(form, message, kind) {
  let box = form.querySelector('.inline-msg');
  if (!box) {
    box = document.createElement('div');
    box.className = 'inline-msg';
    form.insertBefore(box, form.firstChild);
  }
  box.textContent = message;
  box.className = 'inline-msg alert alert-' + kind;
  box.style.marginBottom = '12px';
}

function openNotificationsModal(btn, url) {
  const badge = btn.querySelector('.notif-badge');
  if (badge) badge.remove();
  openFormModal(url || '/user/notifications', true);
}

/* ── Generic centered form modal (edit book / log damage, etc.) ──
   Fetches a form fragment and injects it; submits via AJAX; on
   success, reloads so the underlying table + any flash message
   are fresh. ── */
function openFormModal(url, wide) {
  const overlay = document.getElementById('form-modal-overlay');
  const box     = document.getElementById('form-modal-box');
  if (!overlay || !box) return;
  box.classList.toggle('wide', !!wide);
  box.innerHTML = '<p style="text-align:center;color:var(--text3);padding:30px 0;">Loading…</p>';
  overlay.classList.add('open');
  fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
    .then(function (r) { return r.text(); })
    .then(function (html) {
      box.innerHTML = html;
      const form = box.querySelector('form');
      if (form) form.addEventListener('submit', function (e) { submitModalForm(e, form); });
    })
    .catch(function () { box.innerHTML = '<p style="text-align:center;color:var(--red);padding:30px 0;">Couldn\'t load this form.</p>'; });
}
function closeFormModal() {
  const overlay = document.getElementById('form-modal-overlay');
  if (overlay) overlay.classList.remove('open');
}
function closeFormModalOutside(e) { if (e.target === document.getElementById('form-modal-overlay')) closeFormModal(); }
function submitModalForm(e, form) {
  e.preventDefault();
  const btn = form.querySelector('button[type="submit"]');
  const original = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }

  fetch(form.action, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    body: new FormData(form),
  })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.success) {
        window.location.reload();
      } else {
        showInlineMessage(form, data.message || 'Something went wrong.', 'danger');
        if (btn) { btn.disabled = false; btn.textContent = original; }
      }
    })
    .catch(function () {
      showInlineMessage(form, 'Something went wrong. Please try again.', 'danger');
      if (btn) { btn.disabled = false; btn.textContent = original; }
    });
}

/* ── Generic confirm modal, replacing native confirm() ── */
let _confirmCallback = null;
function openConfirmModal(message, onConfirm, confirmLabel) {
  const overlay = document.getElementById('confirm-modal-overlay');
  const msgEl   = document.getElementById('confirm-modal-message');
  const btnEl   = document.getElementById('confirm-modal-yes-btn');
  if (!overlay || !msgEl) { if (onConfirm) onConfirm(); return; }
  msgEl.textContent = message;
  if (btnEl) btnEl.textContent = confirmLabel || 'Yes, confirm';
  _confirmCallback = onConfirm;
  overlay.classList.add('open');
}
function closeConfirmModal() {
  const overlay = document.getElementById('confirm-modal-overlay');
  if (overlay) overlay.classList.remove('open');
  _confirmCallback = null;
}
function confirmModalYes() {
  const cb = _confirmCallback;
  closeConfirmModal();
  if (cb) cb();
}
function confirmActionForm(e, form, message, confirmLabel) {
  e.preventDefault();
  openConfirmModal(message, function () {
    form.submit();
  }, confirmLabel);
}
function confirmDeleteForm(e, form, itemLabel) {
  confirmActionForm(e, form, 'Do you want to delete ' + (itemLabel || 'this item') + '? Please confirm.', 'Yes, delete');
}

/* ── Realtime-ish order tracking: poll a JSON status endpoint on an
   interval, pausing while the tab is hidden and stopping once the
   server reports a terminal state. ── */
function startStatusPolling(url, onUpdate, intervalMs) {
  intervalMs = intervalMs || 15000;
  let timer = null;
  function tick() {
    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        onUpdate(data);
        if (data.is_terminal) stop();
      })
      .catch(function () {});
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  tick();
  timer = setInterval(tick, intervalMs);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { stop(); }
    else if (!timer) { tick(); timer = setInterval(tick, intervalMs); }
  });
  return stop;
}
