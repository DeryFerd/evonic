// Dashboard lazy loading — fetches data from /api/dashboard/data and populates UI

$(function() {
    loadDashboard();
});

function loadDashboard() {
    $.getJSON('/api/dashboard/data', function(data) {
        renderStats(data.stats);
        renderSecondaryStats(data.skill_stats, data.plugin_stats, data.schedule_stats);
        renderAgents(data.recent_agents);
        renderScheduledTasks(data.schedules);
        renderLeaderboard(data.leaderboard);
        renderRecentRuns(data.recent_runs);
        renderModelUsage(data.model_usage);
        renderPluginCards(data.plugin_cards);
    }).fail(function() {
        $('#agent-empty, #schedules-empty, #leaderboard-empty, #recent-runs-empty, #model-usage-empty')
            .html('<p class="p-6 text-sm text-red-500 dark:text-red-400">Failed to load data. Please try refreshing the page.</p>');
    });
}

function emptyState(message) {
    return '<div class="p-8 text-center text-sm text-gray-400 dark:text-gray-500">' + message + '</div>';
}

function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
}

function truncate(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.substring(0, n) + '…' : s;
}

function scorePillClass(score) {
    return score >= 80 ? 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300' :
           score >= 60 ? 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300' :
                         'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300';
}

// ── Hero + compact strip stats ────────────────────────────────────────────────
function renderStats(stats) {
    $('#stat-agent-count').text(stats.agent_count);
    $('#stat-session-count').text(stats.session_count);
    $('#stat-tool-count').text(stats.tool_count);
    $('#stat-active-channel-count').text(stats.active_channel_count);
    $('#stat-channel-count').text(stats.channel_count);
}

function renderSecondaryStats(skillStats, pluginStats, scheduleStats) {
    $('#stat-skill-enabled').text(skillStats.enabled);
    $('#stat-skill-total').text(skillStats.total);
    var setWord = skillStats.skillset_count === 1 ? 'skillset' : 'skillsets';
    $('#stat-skill-skillsets').text(skillStats.skillset_count + ' ' + setWord);
    $('#stat-plugin-enabled').text(pluginStats.enabled);
    $('#stat-plugin-total').text(pluginStats.total);
    $('#stat-schedule-active').text(scheduleStats.active);
    $('#stat-schedule-total').text(scheduleStats.total);
}

// ── Agents ────────────────────────────────────────────────────────────────────
function renderAgents(agents) {
    var $list = $('#agent-list'), $empty = $('#agent-empty');
    if (!agents || agents.length === 0) {
        $list.hide();
        $empty.html(emptyState('No agents yet')).show();
        return;
    }
    $empty.hide();
    $list.show();

    var html = '';
    for (var i = 0; i < agents.length; i++) {
        var a = agents[i];
        var name = a.name || a.id;
        var initial = name.charAt(0).toUpperCase();
        var desc = truncate(a.description || 'No description', 48);
        var avatar = a.avatar_path
            ? '<img src="/api/agents/' + a.id + '/avatar?size=small" class="w-9 h-9 rounded-full object-cover flex-shrink-0" alt="">'
            : '<div class="w-9 h-9 rounded-full bg-indigo-100 dark:bg-indigo-500/20 grid place-items-center flex-shrink-0 text-xs font-bold text-indigo-600 dark:text-indigo-300">' + escapeHtml(initial) + '</div>';
        var modelBadge = a.model_id
            ? '<span class="text-[11px] bg-gray-100 dark:bg-gray-700/60 text-gray-500 dark:text-gray-400 px-2 py-0.5 rounded font-mono hidden md:inline">' + escapeHtml(truncate(a.model_id, 22)) + '</span>'
            : '';

        html += '<a href="/agents/' + encodeURIComponent(a.id) + '" class="flex items-center justify-between px-5 py-3.5 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors no-underline text-inherit">'
              +   '<div class="flex items-center gap-3 min-w-0">' + avatar
              +     '<div class="min-w-0">'
              +       '<div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">' + escapeHtml(name) + '</div>'
              +       '<div class="text-xs text-gray-400 dark:text-gray-500 truncate">' + escapeHtml(desc) + '</div>'
              +     '</div>'
              +   '</div>'
              +   '<div class="flex items-center gap-3 flex-shrink-0 ml-3">' + modelBadge
              +     '<div class="flex gap-2 text-xs text-gray-400 dark:text-gray-500">'
              +       '<span title="Tools">' + a.tool_count + ' tools</span>'
              +       '<span title="Channels">' + a.channel_count + ' ch</span>'
              +     '</div>'
              +   '</div>'
              + '</a>';
    }
    $list.html(html);
}

// ── Scheduled Tasks (replaces Todo Tasks) ─────────────────────────────────────
function renderScheduledTasks(schedules) {
    var $list = $('#schedules-list'), $empty = $('#schedules-empty');
    if (!schedules || schedules.length === 0) {
        $list.hide();
        $empty.html(emptyState('No scheduled tasks')).show();
        return;
    }
    $empty.hide();
    $list.show();

    var html = '';
    for (var i = 0; i < schedules.length; i++) {
        var s = schedules[i];
        var active = !!s.enabled;
        var pill = active
            ? '<span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300">Active</span>'
            : '<span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 dark:bg-gray-700/60 dark:text-gray-400">Paused</span>';
        var meta = [];
        if (s.trigger_type) meta.push(escapeHtml(s.trigger_type));
        var next = formatDate(s.next_run_at);
        if (next) meta.push('next ' + next);
        var dotColor = active ? 'bg-green-400 dark:bg-green-500' : 'bg-gray-300 dark:bg-gray-600';

        html += '<a href="/scheduler" class="flex items-start gap-3 px-5 py-3 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors no-underline text-inherit">'
              +   '<span class="w-2 h-2 rounded-full mt-1.5 flex-shrink-0 ' + dotColor + '"></span>'
              +   '<div class="min-w-0 flex-1">'
              +     '<div class="flex items-center gap-2">'
              +       '<span class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">' + escapeHtml(s.name || 'Untitled') + '</span>' + pill
              +     '</div>'
              +     (meta.length ? '<div class="text-xs text-gray-400 dark:text-gray-500 mt-0.5 truncate">' + meta.join(' · ') + '</div>' : '')
              +   '</div>'
              + '</a>';
    }
    $list.html(html);
}

function formatDate(v) {
    if (!v) return '';
    try {
        var d = new Date(typeof v === 'number' ? v * 1000 : v);
        if (isNaN(d.getTime())) return '';
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    } catch (e) { return ''; }
}

// ── Model Leaderboard ─────────────────────────────────────────────────────────
function renderLeaderboard(leaderboard) {
    var $list = $('#leaderboard-list'), $empty = $('#leaderboard-empty');
    if (!leaderboard || leaderboard.length === 0) {
        $list.hide();
        $empty.html(emptyState('No model scores yet')).show();
        return;
    }
    $empty.hide();
    $list.show();

    var html = '';
    for (var i = 0; i < leaderboard.length; i++) {
        var m = leaderboard[i], idx = i + 1;
        var medal = idx === 1 ? 'bg-yellow-100 text-yellow-700 dark:bg-yellow-500/20 dark:text-yellow-300' :
                    idx === 2 ? 'bg-gray-200 text-gray-600 dark:bg-gray-600/40 dark:text-gray-300' :
                    idx === 3 ? 'bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300' :
                                'bg-gray-100 text-gray-500 dark:bg-gray-700/50 dark:text-gray-400';
        var score = m.best_score * 100;
        var runWord = m.run_count === 1 ? 'run' : 'runs';
        var href = m.best_run_id ? '/history/' + m.best_run_id : '/history';

        html += '<a href="' + href + '" class="flex items-center justify-between px-5 py-3.5 no-underline hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors">'
              +   '<div class="flex items-center gap-3 min-w-0">'
              +     '<div class="w-7 h-7 rounded-full grid place-items-center flex-shrink-0 text-xs font-bold ' + medal + '">' + idx + '</div>'
              +     '<div class="min-w-0">'
              +       '<div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate" title="' + escapeHtml(m.model_name) + '">' + escapeHtml(truncate(m.model_name, 24)) + '</div>'
              +       '<div class="text-xs text-gray-400 dark:text-gray-500">' + m.run_count + ' ' + runWord + '</div>'
              +     '</div>'
              +   '</div>'
              +   '<span class="text-xs font-semibold px-2.5 py-1 rounded-full flex-shrink-0 ' + scorePillClass(score) + '">' + score.toFixed(0) + '%</span>'
              + '</a>';
    }
    $list.html(html);
}

// ── Recent Evaluations ────────────────────────────────────────────────────────
function renderRecentRuns(runs) {
    var $list = $('#recent-runs-list'), $empty = $('#recent-runs-empty');
    if (!runs || runs.length === 0) {
        $list.hide();
        $empty.html(emptyState('No evaluation runs yet')).show();
        return;
    }
    $empty.hide();
    $list.show();

    var html = '';
    for (var i = 0; i < runs.length; i++) {
        var r = runs[i];
        var modelName = r.model_name || 'Unknown model';
        var scoreHtml;
        if (r.overall_score !== null && r.overall_score !== undefined) {
            var score = r.overall_score * 100;
            scoreHtml = '<span class="text-xs font-semibold px-2.5 py-1 rounded-full ' + scorePillClass(score) + '">' + score.toFixed(0) + '%</span>';
        } else {
            scoreHtml = '<span class="text-xs bg-gray-100 dark:bg-gray-700/60 text-gray-500 dark:text-gray-400 px-2.5 py-1 rounded-full">--</span>';
        }

        html += '<a href="/history/' + encodeURIComponent(r.run_id) + '" class="flex items-center justify-between px-5 py-3.5 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors no-underline text-inherit">'
              +   '<div class="min-w-0 mr-3">'
              +     '<div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate" title="' + escapeHtml(modelName) + '">' + escapeHtml(truncate(modelName, 24)) + '</div>'
              +     '<div class="text-xs text-gray-400 dark:text-gray-500"><span class="font-mono">#' + escapeHtml(String(r.run_id)) + '</span> · ' + r.passed_count + '/' + r.test_count + ' passed</div>'
              +   '</div>' + scoreHtml
              + '</a>';
    }
    $list.html(html);
}

// ── Model Distribution ────────────────────────────────────────────────────────
function renderModelUsage(modelUsage) {
    var $list = $('#model-usage-list'), $empty = $('#model-usage-empty');
    if (!modelUsage || modelUsage.length === 0) {
        $list.hide();
        $empty.html(emptyState('No model usage data yet')).show();
        return;
    }
    $empty.hide();
    $list.show();

    var maxCount = modelUsage[0].agent_count || 1;
    var html = '';
    for (var i = 0; i < modelUsage.length; i++) {
        var item = modelUsage[i];
        var pct = Math.max(4, Math.round(item.agent_count / maxCount * 100));
        html += '<div>'
              +   '<div class="flex justify-between items-center mb-1.5 gap-2">'
              +     '<span class="text-sm text-gray-700 dark:text-gray-300 font-medium truncate" title="' + escapeHtml(item.model) + '">' + escapeHtml(truncate(item.model, 28)) + '</span>'
              +     '<span class="text-xs text-gray-400 dark:text-gray-500 flex-shrink-0">' + item.agent_count + '</span>'
              +   '</div>'
              +   '<div class="w-full bg-gray-100 dark:bg-gray-700/60 rounded-full h-2">'
              +     '<div class="bg-indigo-500 dark:bg-indigo-400 h-2 rounded-full" style="width:' + pct + '%"></div>'
              +   '</div>'
              + '</div>';
    }
    $list.html(html);
}

// ── Plugin cards (right column; reflows main grid when present) ────────────────
function renderPluginCards(pluginCards) {
    var $container = $('#plugin-cards-container');
    $container.empty();
    if (!pluginCards || pluginCards.length === 0) {
        $container.hide();
        return;
    }

    var html = '';
    for (var i = 0; i < pluginCards.length; i++) {
        var card = pluginCards[i];
        var items = card.items || [];
        html += '<div class="rounded-xl border border-gray-200 dark:border-gray-700/60 bg-white dark:bg-gray-800/40 overflow-hidden">'
              +   '<div class="flex justify-between items-center px-5 py-4 border-b border-gray-100 dark:border-gray-700/60">'
              +     '<h3 class="text-base font-semibold text-gray-800 dark:text-gray-100 m-0">' + escapeHtml(card.title || 'Plugin Card') + '</h3>'
              +     (card.link ? '<a href="' + escapeHtml(card.link) + '" class="text-xs text-indigo-600 dark:text-indigo-400 hover:underline no-underline">View all</a>' : '')
              +   '</div>';
        if (items.length === 0) {
            html += emptyState('No items');
        } else {
            html += '<div class="divide-y divide-gray-100 dark:divide-gray-700/50">';
            var maxShow = Math.min(items.length, 5);
            for (var j = 0; j < maxShow; j++) {
                var it = items[j];
                var desc = it.description ? truncate(it.description, 60) : '';
                var created = formatDate(it.created_at);
                html += '<a href="' + escapeHtml(card.link || '#') + '" class="block px-5 py-3.5 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors no-underline text-inherit">'
                      +   '<div class="flex items-start gap-3">'
                      +     '<span class="w-2 h-2 rounded-full bg-amber-400 dark:bg-amber-500 mt-1.5 flex-shrink-0"></span>'
                      +     '<div class="min-w-0">'
                      +       '<div class="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">' + escapeHtml(it.title || 'Untitled') + '</div>'
                      +       (desc ? '<div class="text-xs text-gray-400 dark:text-gray-500 mt-0.5">' + escapeHtml(desc) + '</div>' : '')
                      +       (created ? '<div class="text-xs text-gray-400 dark:text-gray-500 mt-1">Created ' + created + '</div>' : '')
                      +     '</div>'
                      +   '</div>'
                      + '</a>';
            }
            html += '</div>';
        }
        html += '</div>';
    }
    $container.html(html).show();

    // Reflow: make room for the 3-col plugin column (default is 7 / 5 / hidden).
    $('#agents-card').removeClass('lg:col-span-7').addClass('lg:col-span-5');
    $('#middle-col').removeClass('lg:col-span-5').addClass('lg:col-span-4');
}
