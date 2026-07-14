/* ========================================
   Agent State Component (unified)
   Shared by agent_detail.html & sessions.html
   ======================================== */

var _stateAgentId = null;
var _stateSessionId = null;

function esc(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * Build HTML for loaded skill badges.
 * Returns an object: { html, hasBadges }
 */
function _buildSkillBadges(skills) {
    if (!skills || skills.length === 0) return { html: '', hasBadges: false, count: 0 };

    var maxVisible = 4;
    var visible = skills.slice(0, maxVisible);
    var hidden = skills.slice(maxVisible);
    var parts = [];

    for (var i = 0; i < visible.length; i++) {
        var s = visible[i];
        // Error detection: name === skill_id means manifest wasn't found
        var isError = (s.name === s.skill_id);
        var errorClass = isError ? ' border border-yellow-400 dark:border-yellow-500' : '';
        var tooltip = isError ? 'Skill error: failed to load metadata' : (s.tool_count ? s.tool_count + ' tools' : '');
        parts.push(
            '<span class="skill-badge inline-flex items-center px-2 py-0.5 rounded text-xs font-medium' +
            ' bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300 ml-1 group' +
            errorClass +
            '" style="transition: opacity 0.15s ease"' +
            (tooltip ? ' title="' + esc(tooltip) + '"' : '') +
            ' data-skill-id="' + esc(s.skill_id) + '">' +
            esc(s.name) +
            '<button onclick="event.stopPropagation();_unloadSkill(\'' + esc(s.skill_id) + '\')"' +
            ' class="ml-1.5 inline-flex items-center justify-center w-4 h-4 rounded-full' +
            ' opacity-0 group-hover:opacity-100' +
            ' bg-gray-300 hover:bg-red-300 dark:bg-gray-600 dark:hover:bg-red-600' +
            ' text-gray-500 hover:text-red-600 dark:text-gray-400 dark:hover:text-red-300' +
            ' transition-all cursor-pointer"' +
            ' title="Unload skill">\u00d7</button>' +
            '</span>'
        );
    }

    // Truncation: "+N more" pill
    if (hidden.length > 0) {
        parts.push(
            '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium' +
            ' bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300 ml-1' +
            ' cursor-pointer" style="transition: opacity 0.15s ease"' +
            ' onclick="var p=this.parentElement;var all=p.querySelectorAll(\'.skill-badge-hidden\');' +
            'for(var i=0;i<all.length;i++)all[i].classList.toggle(\'hidden\');' +
            'this.textContent=all[0].classList.contains(\'hidden\')?\'+' + hidden.length + ' more\':\'Show less\'">' +
            '+' + hidden.length + ' more</span>'
        );
        // Add hidden badges
        for (var j = 0; j < hidden.length; j++) {
            var hs = hidden[j];
            var hsError = (hs.name === hs.skill_id);
            var hsErrorClass = hsError ? ' border border-yellow-400 dark:border-yellow-500' : '';
            var hsTooltip = hsError ? 'Skill error: failed to load metadata' : (hs.tool_count ? hs.tool_count + ' tools' : '');
            parts.push(
                '<span class="skill-badge-hidden skill-badge hidden inline-flex items-center px-2 py-0.5 rounded text-xs font-medium' +
                ' bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300 ml-1 group' +
                hsErrorClass +
                '" style="transition: opacity 0.15s ease"' +
                (hsTooltip ? ' title="' + esc(hsTooltip) + '"' : '') +
                ' data-skill-id="' + esc(hs.skill_id) + '">' +
                esc(hs.name) +
                '<button onclick="event.stopPropagation();_unloadSkill(\'' + esc(hs.skill_id) + '\')"' +
                ' class="ml-1.5 inline-flex items-center justify-center w-4 h-4 rounded-full' +
                ' opacity-0 group-hover:opacity-100' +
                ' bg-gray-300 hover:bg-red-300 dark:bg-gray-600 dark:hover:bg-red-600' +
                ' text-gray-500 hover:text-red-600 dark:text-gray-400 dark:hover:text-red-300' +
                ' transition-all cursor-pointer"' +
                ' title="Unload skill">\u00d7</button>' +
                '</span>'
            );
        }
    }

    return { html: parts.join(''), hasBadges: true, count: skills.length };
}

/**
 * Core rendering logic (no debounce).
 */
function _renderAgentStateCore(containerIds, data) {
    var empty = '<p class="text-sm text-gray-400 dark:text-gray-500 italic">No state yet.</p>';
    var hasAnyState = data.focus ||
        data.active_model ||
        (data.cmp && data.cmp.paths && data.cmp.paths.length > 0) ||
        (data.states && Object.keys(data.states).length > 0);
    if (!hasAnyState) {
        (Array.isArray(containerIds) ? containerIds : [containerIds]).forEach(function(id) {
            var el = document.getElementById(id);
            if (el) el.innerHTML = empty;
        });
        return;
    }

    // Build status cards row (Focus + Model + Skills)
    var cards = '';

    // Focus badge
    if (data.focus) {
        var reasonText = data.focus_reason ? ' \u2014 ' + esc(data.focus_reason) : '';
        cards += '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300 ml-1">Focus' + reasonText + '</span>';
    }

    // Active model badge
    if (data.active_model) {
        var am = data.active_model;
        if (am.is_fallback) {
            cards += '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300 ml-1" title="Using fallback model due to primary failure">Model: ' + esc(am.name) + ' <span class="ml-1 text-[10px] opacity-75">(fallback)</span>' +
                '<button onclick="event.stopPropagation();_resetActiveModel()" class="ml-1.5 inline-flex items-center justify-center w-4 h-4 rounded-full bg-amber-200 hover:bg-red-200 dark:bg-amber-700 dark:hover:bg-red-700 text-amber-600 hover:text-red-600 dark:text-amber-300 dark:hover:text-red-300 transition-colors cursor-pointer" title="Reset to primary model">\u00d7</button>' +
                '</span>';
        } else {
            cards += '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 ml-1">Model: ' + esc(am.name) + '</span>';
        }
    }

    // CMP session-path map badge (clickable — opens the graph modal)
    if (data.cmp && data.cmp.paths && data.cmp.paths.length > 0) {
        _cmpMapData = data.cmp;
        var activePath = null;
        for (var pi = 0; pi < data.cmp.paths.length; pi++) {
            if (data.cmp.paths[pi].id === data.cmp.active_id) { activePath = data.cmp.paths[pi]; break; }
        }
        var activeTitle = activePath ? activePath.title : '';
        cards += '<span onclick="_openCmpMap()"' +
            ' class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium' +
            ' bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300 ml-1' +
            ' cursor-pointer hover:bg-purple-200 dark:hover:bg-purple-800"' +
            ' title="' + esc(activeTitle) + ' — click to view the session path map">' +
            'CMP: ' + data.cmp.paths.length + ' cards</span>';
    }

    // Raw JSON for debug
    var rawJson = JSON.stringify(data, null, 2);

    var html = '<div class="space-y-2 text-sm">';

    // Status cards row
    html += '<div class="flex flex-wrap gap-1">' + cards + '</div>';

    // Plugin states section
    if (data.states && Object.keys(data.states).length > 0) {
        html += '<div class="border-t border-gray-100 dark:border-gray-700 pt-2"><div class="text-gray-500 dark:text-gray-400 font-medium mb-1 text-xs uppercase tracking-wide">Plugin States</div><ul class="space-y-1">';
        var stateEntries = Object.entries(data.states);
        for (var si = 0; si < stateEntries.length; si++) {
            var ns = stateEntries[si][0];
            var slot = stateEntries[si][1];
            var stateVal = slot.state || 'unknown';
            var dataStr = slot.data ? JSON.stringify(slot.data) : '';
            html += '<li><div class="flex items-center gap-1"><span class="font-medium text-xs text-gray-700 dark:text-gray-200">' + esc(ns) + ':</span><code class="text-xs bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">' + esc(stateVal) + '</code></div>';
            if (dataStr) {
                html += '<div class="text-[10px] text-gray-400 dark:text-gray-500 mt-0.5 font-mono break-all">' + esc(dataStr) + '</div>';
            }
            html += '</li>';
        }
        html += '</ul></div>';
    }

    // Debug: raw JSON toggle
    html += '<div class="mt-2 pt-2 border-t border-gray-100 dark:border-gray-700">';
    html += '<div class="flex justify-end">';
    html += '<button onclick="this.parentElement.nextElementSibling.classList.toggle(\'hidden\');this.textContent=this.parentElement.nextElementSibling.classList.contains(\'hidden\')?\'Show Raw JSON\':\'Hide Raw JSON\'" class="text-[10px] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 underline cursor-pointer">Show Raw JSON</button>';
    html += '</div>';
    html += '<pre class="hidden mt-1 rounded p-2 text-[10px] font-mono overflow-x-auto whitespace-pre-wrap break-all max-h-40 overflow-y-auto">' + esc(rawJson) + '</pre>';
    html += '</div>';

    html += '</div>';

    (Array.isArray(containerIds) ? containerIds : [containerIds]).forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.innerHTML = html;
    });
}

/**
 * Public entry point. Fetches state API and renders.
 * Pass `preloadedData` to render from an already-fetched state payload
 * (avoids a duplicate /chat/state request when the caller has one).
 */
async function renderAgentState(agentId, userId, containerIds, sessionId, preloadedData) {
    if (!agentId) return;
    _stateAgentId = agentId;
    _stateSessionId = sessionId || null;
    try {
        var data = preloadedData;
        if (!data) {
            var url = '/api/agents/' + agentId + '/chat/state?user_id=' + encodeURIComponent(userId || 'web_test');
            if (sessionId) url += '&session_id=' + encodeURIComponent(sessionId);
            var res = await fetch(url);
            if (!res.ok) { console.warn('[AgentState] API error:', res.status, res.statusText); return; }
            data = await res.json();
        }
        _renderAgentStateCore(containerIds, data);
    } catch (e) { console.error('[AgentState] error:', e); }
}

function clearAgentState(containerIds) {
    var empty = '<p class="text-sm text-gray-400 dark:text-gray-500 italic">No state yet.</p>';
    (Array.isArray(containerIds) ? containerIds : [containerIds]).forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.innerHTML = empty;
    });
}

function _resetActiveModel() {
    if (!_stateAgentId) { console.warn('[AgentState] No agent ID for reset'); return; }
    fetch('/api/agents/' + _stateAgentId + '/model/reset', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                document.dispatchEvent(new CustomEvent('evonic:agent-state-changed'));
            } else {
                console.warn('[AgentState] Reset failed:', data.error || data.result);
            }
        })
        .catch(function(e) { console.error('[AgentState] Reset error:', e); });
}

function _unloadSkill(skillId) {
    if (!_stateAgentId || !_stateSessionId) {
        console.warn('[AgentState] No agent/session ID for skill unload');
        return;
    }
    var url = '/api/agents/' + _stateAgentId + '/skills/' + encodeURIComponent(skillId) + '/unload?session_id=' + encodeURIComponent(_stateSessionId);
    fetch(url, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                document.dispatchEvent(new CustomEvent('evonic:agent-state-changed'));
            } else {
                console.warn('[AgentState] Skill unload failed:', data.error || data.result);
            }
        })
        .catch(function(e) { console.error('[AgentState] Skill unload error:', e); });
}

/* ========================================
   CMP Session Path Map (modal + SVG graph)
   ======================================== */

var _cmpMapData = null;
var _cmpSelectedPath = null;

var _CMP_STATUS_STYLE = {
    active:   { stroke: '#22c55e', fill: 'rgba(34,197,94,0.12)',  label: 'active' },
    dormant:  { stroke: '#f59e0b', fill: 'rgba(245,158,11,0.10)', label: 'dormant' },
    archived: { stroke: '#9ca3af', fill: 'rgba(156,163,175,0.10)', label: 'archived' }
};

function _openCmpMap() {
    if (!_cmpMapData || !_cmpMapData.paths || !_cmpMapData.paths.length) return;
    _cmpSelectedPath = _cmpMapData.active_id;
    var existing = document.getElementById('cmp-map-modal');
    if (existing) existing.remove();

    var overlay = document.createElement('div');
    overlay.id = 'cmp-map-modal';
    overlay.setAttribute('style',
        'position:fixed;inset:0;z-index:1000;display:flex;align-items:center;' +
        'justify-content:center;background:rgba(0,0,0,0.55);padding:16px;');
    overlay.addEventListener('click', function (e) {
        if (e.target === overlay) _closeCmpMap();
    });

    var panel = document.createElement('div');
    panel.className = 'bg-white dark:bg-gray-800 rounded-lg shadow-xl';
    panel.setAttribute('style',
        'max-width:760px;width:100%;max-height:85vh;overflow-y:auto;padding:20px;');

    panel.innerHTML =
        '<div class="flex items-center justify-between mb-3">' +
        '  <h3 class="text-base font-semibold text-gray-800 dark:text-gray-100">Session Path Map</h3>' +
        '  <div class="flex items-center gap-2">' +
        '    <button onclick="_toggleCmpSource()" class="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 underline cursor-pointer">Mermaid source</button>' +
        '    <button onclick="_closeCmpMap()" class="inline-flex items-center justify-center w-7 h-7 rounded-full bg-gray-100 hover:bg-gray-200 dark:bg-gray-700 dark:hover:bg-gray-600 text-gray-500 dark:text-gray-300 cursor-pointer" title="Close">×</button>' +
        '  </div>' +
        '</div>' +
        '<div id="cmp-map-svg" class="overflow-x-auto text-gray-800 dark:text-gray-100"></div>' +
        '<pre id="cmp-map-source" class="hidden mt-3 rounded p-2 text-[10px] font-mono overflow-x-auto whitespace-pre-wrap break-all bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200">' +
        esc(_cmpMapData.mermaid || '') + '</pre>' +
        '<div id="cmp-map-detail" class="mt-3 border-t border-gray-100 dark:border-gray-700 pt-3 text-sm"></div>';

    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    document.addEventListener('keydown', _cmpEscHandler);

    document.getElementById('cmp-map-svg').innerHTML = _buildCmpSvg(_cmpMapData);
    _cmpRenderDetail();
}

function _closeCmpMap() {
    var el = document.getElementById('cmp-map-modal');
    if (el) el.remove();
    document.removeEventListener('keydown', _cmpEscHandler);
}

function _cmpEscHandler(e) { if (e.key === 'Escape') _closeCmpMap(); }

function _toggleCmpSource() {
    var el = document.getElementById('cmp-map-source');
    if (el) el.classList.toggle('hidden');
}

function _cmpSelectPath(pathId) {
    _cmpSelectedPath = pathId;
    var svgHost = document.getElementById('cmp-map-svg');
    if (svgHost) svgHost.innerHTML = _buildCmpSvg(_cmpMapData);
    _cmpRenderDetail();
}

function _cmpRenderDetail() {
    var host = document.getElementById('cmp-map-detail');
    if (!host || !_cmpMapData) return;
    var p = null;
    for (var i = 0; i < _cmpMapData.paths.length; i++) {
        if (_cmpMapData.paths[i].id === _cmpSelectedPath) { p = _cmpMapData.paths[i]; break; }
    }
    if (!p) { host.innerHTML = ''; return; }
    var st = _CMP_STATUS_STYLE[p.status] || _CMP_STATUS_STYLE.archived;
    var html = '<div class="flex items-center gap-2 mb-1">' +
        '<span class="font-semibold text-gray-800 dark:text-gray-100">' + esc(p.id) + ' — ' + esc(p.title || '(untitled)') + '</span>' +
        '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" style="color:' + st.stroke + ';background:' + st.fill + '">' +
        (p.id === _cmpMapData.active_id ? 'ACTIVE' : esc(p.status)) + '</span></div>';
    if (p.goal) html += '<div class="text-gray-600 dark:text-gray-300 text-xs mb-1"><span class="font-medium">Goal:</span> ' + esc(p.goal) + '</div>';
    if (p.outcome) html += '<div class="text-gray-600 dark:text-gray-300 text-xs mb-1"><span class="font-medium">Outcome:</span> ' + esc(p.outcome) + '</div>';
    if (p.key_facts && p.key_facts.length) {
        html += '<ul class="text-xs text-gray-500 dark:text-gray-400 mt-1 space-y-0.5">';
        for (var k = 0; k < p.key_facts.length; k++) html += '<li>• ' + esc(p.key_facts[k]) + '</li>';
        html += '</ul>';
    }
    if (p.artifacts && p.artifacts.length) {
        html += '<div class="text-[10px] text-gray-400 dark:text-gray-500 mt-1 font-mono break-all">' + esc(p.artifacts.join('  ')) + '</div>';
    }
    if (p.depends_on && p.depends_on.length) {
        html += '<div class="text-xs text-gray-500 dark:text-gray-400 mt-1">depends on: ' + esc(p.depends_on.join(', ')) + '</div>';
    }
    host.innerHTML = html;
}


/**
 * Offline SVG rendering of the session graph (no mermaid.js dependency) as a
 * proper TREE: the Agent root on top, each path positioned under its
 * dependency parent, solid arrows carrying the action label. Extra
 * dependencies (beyond the primary parent) render as dashed arrows.
 * Click a node to inspect its card.
 */
function _buildCmpSvg(cmp) {
    var paths = cmp.paths || [];
    var NODE_W = 172, NODE_H = 48, GAP_X = 26, GAP_Y = 74;
    var ROOT = '__agent__';

    // Build the tree: primary parent = first known dependency, else Agent.
    var byId = {};
    for (var i = 0; i < paths.length; i++) byId[paths[i].id] = paths[i];
    var children = {};
    children[ROOT] = [];
    var extraDeps = [];  // [fromId, toId] dashed secondary dependencies
    for (var j = 0; j < paths.length; j++) {
        var p = paths[j];
        var deps = (p.depends_on || []).filter(function (d) { return byId[d]; });
        var parent = deps.length ? deps[0] : ROOT;
        (children[parent] = children[parent] || []).push(p.id);
        for (var k = 1; k < deps.length; k++) extraDeps.push([p.id, deps[k]]);
    }

    // Tidy tree layout: subtree width = max(node, sum of children), parents
    // centered over their children.
    var widths = {};
    function subtreeWidth(id) {
        var kids = children[id] || [];
        if (!kids.length) return (widths[id] = NODE_W);
        var total = 0;
        for (var c = 0; c < kids.length; c++) {
            total += subtreeWidth(kids[c]);
            if (c > 0) total += GAP_X;
        }
        return (widths[id] = Math.max(NODE_W, total));
    }
    subtreeWidth(ROOT);

    var pos = {};   // id -> {x, y} of node top-left
    var maxDepth = 0;
    function place(id, left, depth) {
        var kids = children[id] || [];
        var w = widths[id];
        pos[id] = { x: left + (w - NODE_W) / 2, y: depth * (NODE_H + GAP_Y) + 10 };
        if (depth > maxDepth) maxDepth = depth;
        var cursor = left + (w > NODE_W ? 0 : (NODE_W - w) / 2);
        if (w <= NODE_W && kids.length) cursor = left + (NODE_W - widths[kids[0]]) / 2;
        for (var c = 0; c < kids.length; c++) {
            place(kids[c], cursor, depth + 1);
            cursor += widths[kids[c]] + GAP_X;
        }
    }
    place(ROOT, 10, 0);

    var width = widths[ROOT] + 20;
    var height = (maxDepth + 1) * (NODE_H + GAP_Y) - GAP_Y + 24;

    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + width + '" height="' + height +
        '" viewBox="0 0 ' + width + ' ' + height + '" style="min-width:320px">';
    svg += '<defs>' +
        '<marker id="cmp-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">' +
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#9ca3af"/></marker>' +
        '<marker id="cmp-arrow-dep" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">' +
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#8b5cf6"/></marker>' +
        '</defs>';

    // Tree edges (parent -> child) with the action label at the midpoint.
    function edge(fromX, fromY, toX, toY, label) {
        var midX = (fromX + toX) / 2, midY = (fromY + toY) / 2;
        var d = 'M ' + fromX + ' ' + fromY +
                ' C ' + fromX + ' ' + (fromY + 26) + ', ' + toX + ' ' + (toY - 30) +
                ', ' + toX + ' ' + (toY - 4);
        var out = '<path d="' + d + '" fill="none" stroke="#9ca3af" stroke-width="1.2" opacity="0.7" marker-end="url(#cmp-arrow)"/>';
        if (label) {
            out += '<text x="' + midX + '" y="' + (midY - 2) + '" text-anchor="middle" font-size="9"' +
                ' fill="currentColor" opacity="0.65" font-style="italic">' + esc(label) + '</text>';
        }
        return out;
    }

    var edges = '', nodes = '';

    // Agent root
    var rootPos = pos[ROOT];
    var rootCx = rootPos.x + NODE_W / 2;
    var agentLabel = String(cmp.agent_name || cmp.agentName || 'Agent').slice(0, 24);
    var rootRx = Math.max(42, agentLabel.length * 4 + 14);
    nodes += '<ellipse cx="' + rootCx + '" cy="' + (rootPos.y + NODE_H / 2) + '" rx="' + rootRx + '" ry="20"' +
        ' fill="rgba(99,102,241,0.12)" stroke="#6366f1" stroke-width="1.5"/>' +
        '<text x="' + rootCx + '" y="' + (rootPos.y + NODE_H / 2 + 4) + '" text-anchor="middle" font-size="12"' +
        ' font-weight="600" fill="currentColor">' + esc(agentLabel) + '</text>';

    for (var n = 0; n < paths.length; n++) {
        var pn = paths[n];
        var xy = pos[pn.id];
        var st = _CMP_STATUS_STYLE[pn.status] || _CMP_STATUS_STYLE.archived;
        var isActive = pn.id === cmp.active_id;
        var isSelected = pn.id === _cmpSelectedPath;
        var title = (pn.title || '').length > 21 ? (pn.title || '').slice(0, 20) + '…' : (pn.title || '');

        // edge from primary parent
        var deps = (pn.depends_on || []).filter(function (d) { return byId[d]; });
        var parentPos = deps.length ? pos[deps[0]] : rootPos;
        var fromY = deps.length ? parentPos.y + NODE_H : rootPos.y + NODE_H / 2 + 20;
        edges += edge(parentPos.x + NODE_W / 2, fromY,
                      xy.x + NODE_W / 2, xy.y, pn.action || '');

        nodes += '<g style="cursor:pointer" onclick="_cmpSelectPath(\'' + esc(pn.id) + '\')">';
        nodes += '<rect x="' + xy.x + '" y="' + xy.y + '" width="' + NODE_W + '" height="' + NODE_H + '" rx="8"' +
            ' fill="' + st.fill + '" stroke="' + st.stroke + '"' +
            ' stroke-width="' + (isActive ? 2.5 : 1.2) + '"' +
            (isSelected ? ' filter="drop-shadow(0 0 3px ' + st.stroke + ')"' : '') + '/>';
        nodes += '<text x="' + (xy.x + 10) + '" y="' + (xy.y + 19) + '" font-size="11" font-weight="600" fill="currentColor">' +
            esc(pn.id) + (isActive ? ' ●' : '') + '</text>';
        nodes += '<text x="' + (xy.x + 10) + '" y="' + (xy.y + 35) + '" font-size="10" fill="currentColor" opacity="0.75">' + esc(title) + '</text>';
        nodes += '<text x="' + (xy.x + NODE_W - 8) + '" y="' + (xy.y + 19) + '" text-anchor="end" font-size="9" fill="' + st.stroke + '">' +
            (isActive ? 'ACTIVE' : esc(st.label)) + '</text>';
        nodes += '</g>';
    }

    // Secondary dependencies (dashed purple)
    for (var e = 0; e < extraDeps.length; e++) {
        var from = pos[extraDeps[e][0]], to = pos[extraDeps[e][1]];
        if (!from || !to) continue;
        var x1 = from.x + NODE_W / 2, y1 = from.y;
        var x2 = to.x + NODE_W / 2, y2 = to.y + NODE_H;
        edges += '<path d="M ' + x1 + ' ' + y1 + ' C ' + x1 + ' ' + (y1 - 30) + ', ' + x2 + ' ' + (y2 + 30) + ', ' + x2 + ' ' + (y2 + 4) + '"' +
            ' fill="none" stroke="#8b5cf6" stroke-width="1.3" stroke-dasharray="4 3" marker-end="url(#cmp-arrow-dep)" opacity="0.8"/>';
    }

    svg += edges + nodes + '</svg>';
    return svg;
}
