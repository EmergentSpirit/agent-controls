/* watch -- vanilla UI, zero build, zero dependency.

   ABSOLUTE SECURITY RULE: transcript and journal content is UNTRUSTED. Every
   value coming from the API goes through createElement + textContent.
   design-note: no raw-HTML assignment API is used anywhere in this file, and
   the test suite greps the file for those API names. Do not "optimize" this
   into HTML string templates: a session log would become script execution. */
(function () {
  'use strict';

  var state = { days: 7, view: 'dashboard', session: null, page: 0 };
  var tooltip = document.getElementById('tooltip');

  /* ---------- safe DOM factory ---------- */
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  function fetchJSON(url) {
    return fetch(url).then(function (r) { return r.json(); });
  }

  /* ---------- tooltip ---------- */
  function hover(target, text) {
    target.addEventListener('mousemove', function (e) {
      tooltip.textContent = text;
      tooltip.classList.remove('hidden');
      tooltip.style.left = (e.clientX + 14) + 'px';
      tooltip.style.top = (e.clientY - 30) + 'px';
    });
    target.addEventListener('mouseleave', function () {
      tooltip.classList.add('hidden');
    });
  }

  /* ---------- bars ---------- */
  function renderBars(host, series, barClass, unit) {
    clear(host);
    if (!series.length) {
      host.appendChild(el('div', 'chart-empty', 'Nothing in this window.'));
      return;
    }
    var max = 0;
    series.forEach(function (p) { if (p.n > max) { max = p.n; } });
    series.forEach(function (p) {
      var col = el('div', 'bar-col');
      var bar = el('div', 'bar ' + barClass);
      bar.style.height = (max ? Math.max(2, Math.round(p.n / max * 100)) : 2) + '%';
      col.appendChild(bar);
      hover(col, p.d + ' : ' + p.n + ' ' + unit);
      host.appendChild(col);
    });
    var axis = el('div', 'axis-x');
    axis.appendChild(el('span', null, series[0].d.slice(5)));
    if (series.length > 1) {
      axis.appendChild(el('span', null, series[series.length - 1].d.slice(5)));
    }
    host.parentNode.insertBefore(axis, host.nextSibling);
    var old = host.parentNode.querySelectorAll('.axis-x');
    if (old.length > 1) { old[0].remove(); }
  }

  function renderDayTable(table, series, header) {
    clear(table);
    var head = el('tr');
    head.appendChild(el('th', null, 'Day'));
    head.appendChild(el('th', null, header));
    table.appendChild(head);
    series.forEach(function (p) {
      var row = el('tr');
      row.appendChild(el('td', null, p.d));
      row.appendChild(el('td', 'num', p.n));
      table.appendChild(row);
    });
  }

  /* ---------- dashboard ---------- */
  function loadDashboard() {
    fetchJSON('/api/summary?days=' + state.days).then(function (d) {
      var host = document.getElementById('tiles');
      clear(host);
      [
        { label: 'Sessions observed', v: d.tiles.sessions, note: 'started in the window' },
        { label: 'Gate events', v: d.tiles.events, note: 'every journalled execution' },
        { label: 'Blocks', v: d.tiles.blocks, note: 'block + deny', alert: d.tiles.blocks > 0 },
        { label: 'Sessions with a block', v: d.tiles.blocked_sessions,
          note: 'gate event linked to a session' }
      ].forEach(function (t) {
        var tile = el('div', 'tile');
        tile.appendChild(el('div', 'tile-label', t.label));
        tile.appendChild(el('div', 'tile-value' + (t.alert ? ' alert' : ''), Number(t.v)));
        tile.appendChild(el('div', 'tile-note', t.note));
        host.appendChild(tile);
      });

      renderBars(document.getElementById('chart-sessions'), d.sessions_by_day,
        'bar--session', 'session(s)');
      renderBars(document.getElementById('chart-blocks'), d.blocks_by_day,
        'bar--block', 'block(s)');
      renderDayTable(document.getElementById('table-sessions-day'), d.sessions_by_day, 'Sessions');
      renderDayTable(document.getElementById('table-blocks-day'), d.blocks_by_day, 'Blocks');

      var table = document.getElementById('table-hooks');
      clear(table);
      var head = el('tr');
      ['Hook', 'Result', 'Count'].forEach(function (h) { head.appendChild(el('th', null, h)); });
      table.appendChild(head);
      d.hooks.slice(0, 40).forEach(function (h) {
        var row = el('tr');
        row.appendChild(el('td', null, h.hook || '?'));
        var cell = el('td');
        var badge = el('span', 'badge', h.result || '?');
        if (h.result === 'block' || h.result === 'deny') { badge.className = 'badge badge--block'; }
        if (h.result === 'pass' || h.result === 'allow') { badge.className = 'badge badge--allow'; }
        cell.appendChild(badge);
        row.appendChild(cell);
        row.appendChild(el('td', 'num', Number(h.n)));
        table.appendChild(row);
      });
    });
  }

  /* ---------- session list ---------- */
  /* Poll for an analysis started from the list. It kills itself when the button
     leaves the DOM (a fresh render picks the state back up from
     analysis_status), and it STOPS on a server-side error: without that, a dead
     judge thread means an infinite poll and a frozen button. */
  function pollList(session_id, cell, button) {
    var timer = setInterval(function () {
      if (!document.body.contains(button)) { clearInterval(timer); return; }
      fetchJSON('/api/sessions').then(function (d) {
        var fresh = d.sessions.filter(function (x) { return x.id === session_id; })[0];
        if (!fresh) { return; }
        if (fresh.severity) {
          clearInterval(timer);
          cell.replaceChild(el('span', 'sev sev--' + fresh.severity, fresh.severity), button);
        } else if (fresh.analysis_status === 'error') {
          clearInterval(timer);
          button.textContent = 'Failed, retry';
          button.disabled = false;
          button.title = 'analysis failed server-side (see the watch log)';
        }
      });
    }, 5000);
  }

  function loadSessions() {
    fetchJSON('/api/sessions').then(function (d) {
      var table = document.getElementById('table-session-list');
      clear(table);
      var head = el('tr');
      ['Role', 'Session', 'Start', 'Messages', 'Tools', 'Blocks', 'Analysis'].forEach(
        function (h) { head.appendChild(el('th', null, h)); });
      table.appendChild(head);
      d.sessions.forEach(function (s) {
        var row = el('tr');
        row.appendChild(el('td', null, s.agent));
        var titleCell = el('td');
        var link = el('a', 'session-link', s.title || s.id.slice(0, 8));
        link.addEventListener('click', function () { openTrajectory(s.id, 0); });
        titleCell.appendChild(link);
        row.appendChild(titleCell);
        row.appendChild(el('td', null, (s.first_ts || '').slice(0, 16).replace('T', ' ')));
        row.appendChild(el('td', 'num', s.n_user + s.n_assistant));
        row.appendChild(el('td', 'num', s.n_tool));
        var blocksCell = el('td', 'num');
        if (s.blocks > 0) {
          blocksCell.appendChild(el('span', 'badge badge--block', s.blocks));
        } else { blocksCell.textContent = '0'; }
        row.appendChild(blocksCell);
        var sevCell = el('td');
        if (s.severity) {
          sevCell.appendChild(el('span', 'sev sev--' + s.severity, s.severity));
        } else {
          var button = el('button', 'btn-analyze', 'Analyze');
          if (s.analysis_status === 'running') {
            button.textContent = 'running...';
            button.disabled = true;
            pollList(s.id, sevCell, button);
          } else if (s.analysis_status === 'error') {
            button.textContent = 'Failed, retry';
            button.title = 'analysis failed server-side (see the watch log)';
          }
          button.addEventListener('click', function (ev) {
            ev.stopPropagation();
            button.textContent = 'running...';
            button.disabled = true;
            button.title = '';
            fetch('/api/analyze/' + encodeURIComponent(s.id), { method: 'POST' });
            pollList(s.id, sevCell, button);
          });
          sevCell.appendChild(button);
        }
        row.appendChild(sevCell);
        table.appendChild(row);
      });
    });
  }

  /* ---------- trajectory ---------- */
  function openTrajectory(session_id, page) {
    state.session = session_id;
    state.page = page;
    showView('trajectory');
    fetchJSON('/api/session/' + encodeURIComponent(session_id) + '?page=' + page).then(function (d) {
      document.getElementById('traj-title').textContent = d.meta.title || session_id;
      document.getElementById('traj-meta').textContent =
        d.meta.agent + ' - ' + d.total + ' messages - ' + (d.meta.models || '');
      renderAnalysis(d.analysis);
      trajButtonState(d);
      if (d.analysis_status === 'running') { pollTrajectory(session_id); }

      var gates = document.getElementById('traj-gates');
      clear(gates);
      d.gates.filter(function (g) { return g.result === 'block' || g.result === 'deny'; })
        .forEach(function (g) {
          var badge = el('span', 'badge badge--block',
            g.hook + ' - ' + (g.ts || '').slice(11, 19));
          hover(badge, g.extra ? String(g.extra).slice(0, 160) : g.result);
          gates.appendChild(badge);
        });

      var thread = document.getElementById('traj-thread');
      clear(thread);
      d.messages.forEach(function (m) {
        var box = el('details', 'msg');
        var summary = el('summary');
        summary.appendChild(el('span', 'msg-seq', '#' + m.seq));
        summary.appendChild(el('span', 'msg-type msg-type--' + (m.type || 'system'), m.type));
        if (m.tool) { summary.appendChild(el('span', 'msg-tool', m.tool)); }
        summary.appendChild(el('span', 'msg-ts', (m.ts || '').slice(11, 19)));
        box.appendChild(summary);
        /* Bodies are NOT in the database: they are re-read from the source
           file, one line at a time, only when someone opens the message. */
        box.addEventListener('toggle', function () {
          if (!box.open || box.dataset.loaded) { return; }
          box.dataset.loaded = '1';
          var body = el('div', 'msg-body');
          var pre = el('pre', null, 'loading...');
          body.appendChild(pre);
          box.appendChild(body);
          fetchJSON('/api/content/' + encodeURIComponent(session_id) + '/' + m.seq)
            .then(function (c) {
              pre.textContent = c.error ? c.error : JSON.stringify(c.content, null, 1);
            });
        });
        thread.appendChild(box);
      });

      var pages = document.getElementById('traj-pages');
      clear(pages);
      var count = Math.ceil(d.total / d.page_size);
      for (var i = 0; i < count; i++) {
        (function (index) {
          var button = el('button', 'filter' + (index === page ? ' active' : ''), index + 1);
          button.addEventListener('click', function () { openTrajectory(session_id, index); });
          pages.appendChild(button);
        })(i);
      }
    });
  }

  /* ---------- analysis: a PROPOSAL on a screen, nothing automatic ---------- */
  function renderAnalysis(a) {
    var host = document.getElementById('traj-analysis');
    clear(host);
    if (!a) { return; }
    var panel = el('div', 'analysis-panel');
    var head = el('div', 'ap-head');
    head.appendChild(el('span', 'ap-title', 'Post-hoc analysis'));
    head.appendChild(el('span', 'sev sev--' + a.severity, a.severity));
    head.appendChild(el('span', 'ap-when', (a.ts || '') + ' - ' + (a.model || '')));
    panel.appendChild(head);
    panel.appendChild(el('div', null, a.summary || ''));
    var findings = [];
    try { findings = JSON.parse(a.findings || '[]'); } catch (e) { findings = []; }
    if (findings.length) {
      var list = el('ul');
      findings.forEach(function (f) {
        list.appendChild(el('li', null,
          (f.type || '?') + ': ' + (f.detail || '') + (f.seq ? ' (#' + f.seq + ')' : '')));
      });
      panel.appendChild(list);
    }
    var proposal = null;
    try { proposal = JSON.parse(a.gate_proposal || 'null'); } catch (e) { proposal = null; }
    if (proposal) {
      var box = el('div', 'ap-gate');
      box.appendChild(el('strong', null, 'Gate PROPOSED: ' + (proposal.name || '?')));
      box.appendChild(el('div', null, proposal.trigger || ''));
      box.appendChild(el('div', null, proposal.rationale || ''));
      box.appendChild(el('div', 'ap-note',
        'A proposal. Nothing was armed: a human decides.'));
      panel.appendChild(box);
    }
    host.appendChild(panel);
  }

  var trajPoll = null;

  /* Mirrors the SERVER state (analysis_status) onto the button. */
  function trajButtonState(d) {
    var button = document.getElementById('traj-analyze');
    if (d.analysis_status === 'running') {
      button.textContent = 'Analysis running...';
      button.disabled = true;
      button.title = '';
    } else if (d.analysis_status === 'error') {
      button.textContent = 'Failed, retry';
      button.disabled = false;
      button.title = d.analysis_error || 'analysis failed server-side';
    } else {
      button.textContent = 'Analyze';
      button.disabled = false;
      button.title = '';
    }
  }

  /* session_id is CAPTURED: navigating to another session does not retarget the poll.
     Stops on a verdict OR on a server-side error, never infinite. */
  function pollTrajectory(session_id) {
    if (trajPoll) { clearInterval(trajPoll); }
    trajPoll = setInterval(function () {
      fetchJSON('/api/session/' + encodeURIComponent(session_id) + '?page=0').then(function (d) {
        if (!d.analysis && d.analysis_status !== 'error') { return; }
        clearInterval(trajPoll);
        trajPoll = null;
        if (state.session !== session_id) { return; }
        trajButtonState(d);
        if (d.analysis) { renderAnalysis(d.analysis); }
      });
    }, 5000);
  }

  document.getElementById('traj-analyze').addEventListener('click', function () {
    if (!state.session) { return; }
    var session_id = state.session;
    this.textContent = 'Analysis running...';
    this.disabled = true;
    this.title = '';
    fetch('/api/analyze/' + encodeURIComponent(session_id), { method: 'POST' });
    pollTrajectory(session_id);
  });

  /* ---------- navigation ---------- */
  function showView(name) {
    ['dashboard', 'sessions', 'trajectory'].forEach(function (v) {
      document.getElementById('view-' + v).classList.toggle('hidden', v !== name);
    });
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.classList.toggle('active', tab.dataset.view === name);
    });
    state.view = name;
  }

  document.querySelectorAll('.tab[data-view]').forEach(function (tab) {
    tab.addEventListener('click', function () {
      showView(tab.dataset.view);
      history.replaceState(null, '', '#' + tab.dataset.view);
      if (tab.dataset.view === 'dashboard') { loadDashboard(); }
      if (tab.dataset.view === 'sessions') { loadSessions(); }
    });
  });

  document.querySelectorAll('.filter[data-days]').forEach(function (f) {
    f.addEventListener('click', function () {
      state.days = parseInt(f.dataset.days, 10);
      document.querySelectorAll('.filter[data-days]').forEach(function (x) {
        x.classList.toggle('active', x === f);
      });
      loadDashboard();
    });
  });

  document.getElementById('traj-back').addEventListener('click', function () {
    showView('sessions');
    loadSessions();
  });

  if (location.hash === '#sessions') {
    showView('sessions');
    loadSessions();
  } else {
    loadDashboard();
  }
})();
