/**
 * Evonic — Unified global JS entry point.
 * Loaded once on every page via base.html.
 */
(function () {
    'use strict';

    var agentsCache = null;
    var agentsPromise = null;   // deduplicate concurrent fetchAgents() calls
    var overlayEl = null;
    var inputEl = null;
    var dropdownEl = null;
    var selectedIndex = -1;
    var filteredAgents = [];

    // ============================================================
    //  Agent Quick Search (Ctrl+G / Cmd+G)
    // ============================================================

    function fetchAgents() {
        if (agentsCache) return Promise.resolve(agentsCache);
        if (agentsPromise) return agentsPromise;  // deduplicate concurrent calls
        agentsPromise = fetch('/api/agents')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                agentsCache = (data.agents || []).filter(function (a) {
                    return a.id && a.name;
                });
                agentsPromise = null;
                return agentsCache;
            })
            .catch(function () {
                agentsCache = [];
                agentsPromise = null;
                return agentsCache;
            });
        return agentsPromise;
    }

    function getInitial(name) {
        if (!name) return '?';
        return name.charAt(0).toUpperCase();
    }

    function buildOverlay() {
        if (overlayEl) return;

        overlayEl = document.createElement('div');
        overlayEl.className = 'fixed inset-0 z-[9999] flex items-start justify-center pt-[22vh]';
        overlayEl.style.background = 'rgba(0,0,0,0.4)';
        overlayEl.style.backdropFilter = 'blur(2px)';
        overlayEl.addEventListener('click', function (e) {
            if (e.target === overlayEl) closeOverlay();
        });

        var box = document.createElement('div');
        box.className = 'w-full max-w-lg mx-4 bg-white dark:bg-gray-800 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-700 overflow-hidden';
        box.style.marginTop = '100px';
        box.addEventListener('click', function (e) { e.stopPropagation(); });

        // Search input row
        var inputRow = document.createElement('div');
        inputRow.className = 'flex items-center px-4 py-3 border-b border-gray-200 dark:border-gray-700';

        var icon = document.createElement('span');
        icon.className = 'mr-3 text-gray-400 dark:text-gray-500 flex-shrink-0';
        icon.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';

        inputEl = document.createElement('input');
        inputEl.type = 'text';
        inputEl.placeholder = 'Search agents...';
        inputEl.className = 'flex-1 bg-transparent border-none outline-none text-gray-900 dark:text-gray-100 text-base placeholder-gray-400 dark:placeholder-gray-500 py-2 px-2';
        inputEl.setAttribute('autocomplete', 'off');
        inputEl.setAttribute('spellcheck', 'false');

        inputRow.appendChild(icon);
        inputRow.appendChild(inputEl);

        // Dropdown
        dropdownEl = document.createElement('div');
        dropdownEl.className = 'max-h-72 overflow-y-auto';

        box.appendChild(inputRow);
        box.appendChild(dropdownEl);
        overlayEl.appendChild(box);

        // Delegated mouseover on dropdown — update highlight without DOM rebuild
        dropdownEl.addEventListener('mouseover', function (e) {
            var item = e.target.closest('[data-index]');
            if (!item) return;
            var idx = parseInt(item.getAttribute('data-index'));
            if (idx === selectedIndex) return;
            var prev = dropdownEl.querySelector('[data-index].bg-blue-50');
            if (prev) {
                prev.classList.remove('bg-blue-50', 'dark:bg-blue-900/30');
            }
            selectedIndex = idx;
            item.classList.add('bg-blue-50', 'dark:bg-blue-900/30');
        });

        // Input events
        inputEl.addEventListener('input', onInput);
        inputEl.addEventListener('keydown', onKeyDown);

        document.body.appendChild(overlayEl);
    }

    function showOverlay() {
        buildOverlay();
        overlayEl.style.display = 'flex';
        selectedIndex = -1;
        filteredAgents = [];
        inputEl.value = '';
        renderDropdown([]);
        setTimeout(function () { inputEl.focus(); }, 50);
        fetchAgents(); // warm cache
    }

    function closeOverlay() {
        if (overlayEl) {
            overlayEl.style.display = 'none';
        }
    }

    function onInput() {
        var query = inputEl.value.trim().toLowerCase();
        if (!query) {
            filteredAgents = [];
            selectedIndex = -1;
            renderDropdown([]);
            return;
        }

        // Capture query in a closure so async callback always uses the
        // correct value even when the user types quickly.
        (function (q) {
            fetchAgents().then(function (agents) {
                filteredAgents = agents.filter(function (a) {
                    var name = String(a.name || '').toLowerCase();
                    var id   = String(a.id   || '').toLowerCase();
                    return name.indexOf(q) !== -1 || id.indexOf(q) !== -1;
                });
                selectedIndex = filteredAgents.length > 0 ? 0 : -1;
                renderDropdown(filteredAgents);
            });
        })(query);
    }

    function renderDropdown(agents) {
        dropdownEl.innerHTML = '';
        if (agents.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'px-4 py-8 text-center text-sm text-gray-400 dark:text-gray-500';
            empty.textContent = 'No agents found';
            dropdownEl.appendChild(empty);
            return;
        }

        agents.forEach(function (agent, i) {
            var item = document.createElement('div');
            item.className = 'flex items-center gap-3 px-4 py-2.5 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors';
            if (i === selectedIndex) {
                item.classList.add('bg-blue-50', 'dark:bg-blue-900/30');
            }
            item.setAttribute('data-index', i);
            item.addEventListener('click', function () { selectAgent(agent); });

            // Avatar circle — custom image when available, initial letter fallback
            var avatar = document.createElement('div');
            avatar.className = 'w-9 h-9 rounded-full flex items-center justify-center text-sm font-semibold text-white flex-shrink-0 overflow-hidden';

            if (agent.avatar_path) {
                var avatarImg = document.createElement('img');
                avatarImg.src = '/api/agents/' + encodeURIComponent(agent.id) + '/avatar?size=small';
                avatarImg.alt = agent.name;
                avatarImg.className = 'w-9 h-9 rounded-full object-cover';
                avatarImg.onerror = function () {
                    avatarImg.remove();
                    avatar.style.backgroundColor = agentColor(agent.id);
                    avatar.textContent = getInitial(agent.name);
                };
                avatar.appendChild(avatarImg);
            } else {
                avatar.style.backgroundColor = agentColor(agent.id);
                avatar.textContent = getInitial(agent.name);
            }

            // Info
            var info = document.createElement('div');
            info.className = 'flex-1 min-w-0';

            var nameLine = document.createElement('div');
            nameLine.className = 'flex items-center gap-2';

            var nameSpan = document.createElement('span');
            nameSpan.className = 'text-sm font-medium text-gray-900 dark:text-gray-100 truncate';
            nameSpan.textContent = agent.name;

            var idSpan = document.createElement('span');
            idSpan.className = 'text-xs text-gray-400 dark:text-gray-500';
            idSpan.textContent = agent.id;

            nameLine.appendChild(nameSpan);
            nameLine.appendChild(idSpan);

            // Enabled/disabled badge
            var badge = document.createElement('span');
            if (agent.enabled) {
                badge.className = 'text-xs px-1.5 py-0.5 rounded bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300';
                badge.textContent = 'active';
            } else {
                badge.className = 'text-xs px-1.5 py-0.5 rounded bg-gray-200 dark:bg-gray-600 text-gray-500 dark:text-gray-400';
                badge.textContent = 'disabled';
            }

            info.appendChild(nameLine);

            item.appendChild(avatar);
            item.appendChild(info);
            item.appendChild(badge);
            dropdownEl.appendChild(item);
        });
    }

    function onKeyDown(e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            closeOverlay();
            return;
        }

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (filteredAgents.length === 0) return;
            selectedIndex = Math.min(selectedIndex + 1, filteredAgents.length - 1);
            renderDropdown(filteredAgents);
            return;
        }

        if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (filteredAgents.length === 0) return;
            selectedIndex = Math.max(selectedIndex - 1, 0);
            renderDropdown(filteredAgents);
            return;
        }

        if (e.key === 'Enter') {
            e.preventDefault();
            if (filteredAgents.length > 0 && selectedIndex >= 0 && selectedIndex < filteredAgents.length) {
                selectAgent(filteredAgents[selectedIndex]);
            }
        }
    }

    function selectAgent(agent) {
        closeOverlay();
        window.location.href = '/agents/' + agent.id;
    }

    // Deterministic color from agent id hash
    function agentColor(id) {
        var colors = [
            '#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6',
            '#ec4899', '#06b6d4', '#f97316', '#6366f1', '#14b8a6',
            '#e11d48', '#7c3aed', '#0891b2', '#ca8a04', '#4f46e5'
        ];
        var hash = 0;
        for (var i = 0; i < id.length; i++) {
            hash = ((hash << 5) - hash + id.charCodeAt(i)) | 0;
        }
        return colors[Math.abs(hash) % colors.length];
    }

    // ============================================================
    //  Global keyboard listener
    // ============================================================

    document.addEventListener('keydown', function (e) {
        // Ctrl+G or Cmd+G
        if ((e.ctrlKey || e.metaKey) && e.key === 'g') {
            e.preventDefault();
            showOverlay();
        }

        // Escape to close overlay when it's open
        if (e.key === 'Escape' && overlayEl && overlayEl.style.display === 'flex') {
            // handled by onKeyDown on input, but double-guard
            closeOverlay();
        }
    });

})();
