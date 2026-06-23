/**
 * Update notification banner — loaded on every page via base.html.
 *
 * Checks for updates (once per day), shows a persistent banner below the
 * navbar, and maintains live status via SSE during updates.
 */
(function() {
    'use strict';

    var banner = document.getElementById('ev-update-banner');
    if (!banner) return;

    var textEl = document.getElementById('ev-update-banner-text');
    var actionsEl = document.getElementById('ev-update-banner-actions');
    var sse = null;

    // -- Banner styling per state ----------------------------------------

    var STYLES = {
        available: 'border-t border-indigo-200 dark:border-indigo-800 bg-indigo-50 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300',
        updating:  'border-t border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300',
        success:   'border-t border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300',
        failed:    'border-t border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300',
    };

    function updateHeaderHeight() {
        var header = document.querySelector('header');
        if (header) {
            document.documentElement.style.setProperty('--header-h', header.offsetHeight + 'px');
        }
    }

    function showBanner(state, text, actionsHtml) {
        banner.className = STYLES[state] || '';
        textEl.textContent = text;
        actionsEl.innerHTML = actionsHtml || '';
        banner.style.display = '';
        if (typeof refreshLucideIcons === 'function') refreshLucideIcons();
        requestAnimationFrame(updateHeaderHeight);
    }

    function hideBanner() {
        banner.style.display = 'none';
        requestAnimationFrame(updateHeaderHeight);
    }

    var CHANGELOG_BASE = 'https://github.com/anvie/evonic/releases/tag/';

    // -- Dismiss helpers --------------------------------------------------

    function getDismissKey() {
        return 'evonic-update-dismissed-' + new Date().toISOString().slice(0, 10);
    }

    function isDismissedToday() {
        return localStorage.getItem(getDismissKey()) === '1';
    }

    function dismissBanner() {
        localStorage.setItem(getDismissKey(), '1');
        hideBanner();
    }

    // -- Action helpers ---------------------------------------------------

    function linkBtn(href, label) {
        return '<a href="' + href + '" class="text-sm font-medium underline hover:no-underline ml-3">' + label + '</a>';
    }

    function extLinkBtn(href, label) {
        return '<a href="' + href + '" target="_blank" rel="noopener" class="text-sm font-medium underline hover:no-underline ml-3">' + label + '</a>';
    }

    function actionBtn(label, onclick) {
        return '<button onclick="' + onclick + '" class="text-sm font-medium underline hover:no-underline ml-3">' + label + '</button>';
    }

    function closeBtn() {
        return '<button onclick="window._evDismissUpdate()" class="ml-3 opacity-60 hover:opacity-100" aria-label="Dismiss" title="Dismiss">' +
               '<i data-lucide="x" class="w-4 h-4"></i></button>';
    }

    // -- State rendering --------------------------------------------------

    window._evDismissUpdate = function() { dismissBanner(); };

    function renderBannerState(data) {
        if (data.status === 'available') {
            if (isDismissedToday()) { hideBanner(); return; }
            var text = 'Update available: ' + (data.current_version || '?') + ' \u2192 ' + (data.latest_version || '?');
            var actions = extLinkBtn(CHANGELOG_BASE + (data.latest_version || ''), 'Changelog')
                        + linkBtn('/system/update', 'Update')
                        + closeBtn();
            showBanner('available', text, actions);
        } else if (data.status === 'updating') {
            var step = data.step_label ? ' \u2014 ' + data.step_label : '';
            showBanner('updating', 'Updating...' + step, linkBtn('/system/update', 'View Progress'));
            connectSSE();
        } else if (data.status === 'success') {
            if (isDismissedToday()) { hideBanner(); return; }
            showBanner('success', 'Update complete!', actionBtn('Restart', 'window._evUpdateRestart()') + closeBtn());
        } else if (data.status === 'failed') {
            var errMsg = data.error ? ': ' + data.error : '';
            showBanner('failed', 'Update failed' + errMsg, linkBtn('/system/update', 'View Details'));
        } else {
            hideBanner();
        }
    }

    // -- Shared RealtimeClient connection (unified SSE) --------------------------

    var _updateRegistered = false;

    function connectSSE() {
        if (_updateRegistered) return;

        if (typeof getSharedRealtime === 'undefined') {
            // Fallback: old EventSource
            if (sse) return;
            sse = new EventSource('/api/system/update/stream');
            sse.addEventListener('status', function(e) {
                var data = JSON.parse(e.data);
                renderBannerState(data);
            });
            sse.addEventListener('done', function() {
                if (sse) { sse.close(); sse = null; }
            });
            sse.onerror = function() {
                if (sse) { sse.close(); sse = null; }
            };
            return;
        }

        // Use shared RealtimeClient — no separate connection needed.
        _updateRegistered = true;
        var rt = getSharedRealtime();
        rt.on('update', 'update_status', function(data) {
            renderBannerState(data);
        });
        rt.on('update', 'update_done', function() {
            _updateRegistered = false;
        });
    }

    // -- Restart handler (exposed globally for inline onclick) ------------

    window._evUpdateRestart = function() {
        showBanner('updating', 'Restarting server...', '');
        fetch('/api/system/update/restart', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.success) return;
                var interval = setInterval(function() {
                    fetch('/api/health')
                        .then(function(r) {
                            if (r.ok) {
                                clearInterval(interval);
                                window.location.reload();
                            }
                        })
                        .catch(function() { /* still restarting */ });
                }, 2000);
            })
            .catch(function() {});
    };

    // -- Once-per-day check -----------------------------------------------

    function shouldCheck() {
        var last = localStorage.getItem('evonic-update-last-check');
        if (!last) return true;
        return (Date.now() - parseInt(last, 10)) > 86400000;
    }

    function markChecked() {
        localStorage.setItem('evonic-update-last-check', Date.now().toString());
    }

    // -- Init -------------------------------------------------------------

    updateHeaderHeight();

    fetch('/api/system/update/status')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.status === 'updating' || data.status === 'success' || data.status === 'failed') {
                renderBannerState(data);
                return;
            }

            if (data.status === 'available') {
                renderBannerState(data);
                return;
            }

            // idle — check if daily check needed
            if (shouldCheck()) {
                fetch('/api/system/update/check')
                    .then(function(r) { return r.json(); })
                    .then(function(result) {
                        markChecked();
                        if (result.available) {
                            renderBannerState({ status: 'available', current_version: result.current, latest_version: result.latest });
                        }
                    })
                    .catch(function() {});
            }
        })
        .catch(function() {});
})();
