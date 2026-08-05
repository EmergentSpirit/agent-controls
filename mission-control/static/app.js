/* mission control -- vanilla UI, zero build, zero dependency.

   ABSOLUTE SECURITY RULE: journal content, pane titles and script descriptions
   are UNTRUSTED. Every value coming from the API goes through createElement +
   textContent. design-note: no raw-HTML assignment API is used anywhere in
   this file, and the test suite greps the file for those API names. Do not
   "optimize" this into HTML string templates: a summary line written by an
   agent would become script execution. */
(function () {
  'use strict';

  var EVENT_TYPES = ['deliverable', 'dispatch', 'mutation', 'decision',
    'blocker', 'pivot', 'health', 'halt', 'circuit-break'];

  var state = { view: 'overview', timer: null };

  /* ---------- safe DOM factory ---------- */
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function byId(id) { return document.getElementById(id); }

  function fetchJSON(url, options) {
    return fetch(url, options).then(function (r) {
      return r.json().then(function (body) {
        body.__status = r.status;
        return body;
      });
    });
  }

  function stamp() {
    byId('stamp').textContent = 'read at ' + new Date().toLocaleTimeString();
  }

  function headRow(table, labels) {
    var head = el('tr');
    labels.forEach(function (label) { head.appendChild(el('th', null, label)); });
    table.appendChild(head);
  }

  function emptyRow(table, span, message) {
    var row = el('tr');
    var cell = el('td', 'empty', message);
    cell.colSpan = span;
    row.appendChild(cell);
    table.appendChild(row);
  }

  function typeClass(kind) {
    return 'badge badge--' + String(kind || 'unknown').replace(/[^a-z-]/g, '');
  }

  /* ---------- overview ---------- */
  function tile(host, label, value, note, alert) {
    var node = el('div', 'tile');
    node.appendChild(el('div', 'tile-label', label));
    node.appendChild(el('div', 'tile-value' + (alert ? ' alert' : ''), value));
    node.appendChild(el('div', 'tile-note', note));
    host.appendChild(node);
  }

  function loadOverview() {
    fetchJSON('/api/overview').then(function (d) {
      var host = byId('tiles');
      var fleet = d.fleet || {};
      var alerts = d.alerts || {};
      var integrity = d.integrity || {};
      var halt = d.halt || {};
      var days = d.window_days + ' day window';
      clear(host);
      tile(host, 'Roles alive', (fleet.alive || 0) + ' / ' + (fleet.total || 0),
        'panes answering right now');
      tile(host, 'Working', fleet.working || 0, 'mid-turn, not waiting on you');
      tile(host, 'Events recorded', d.total_events || 0, 'the whole signed log');
      tile(host, 'Blockers', alerts.blockers || 0, days, alerts.blockers > 0);
      tile(host, 'Health alerts', alerts.health || 0, days, alerts.health > 0);
      tile(host, 'Circuit breaks', alerts.circuit_break || 0, days,
        alerts.circuit_break > 0);
      tile(host, 'Log integrity', integrity.ok ? 'intact' :
        ('TAMPERED x' + (integrity.tampered || []).length),
        'HMAC recomputed over every row', !integrity.ok);
      tile(host, 'Execution engine', halt.paused ? 'PAUSED' : 'not paused',
        halt.module === false ? 'read from the flag file' : 'reported by the halt module',
        !!halt.paused);

      var table = byId('table-recent');
      clear(table);
      headRow(table, ['When', 'Role', 'Project', 'Kind', 'Summary']);
      var rows = d.recent || [];
      if (!rows.length) { emptyRow(table, 5, 'No events yet.'); }
      rows.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', r.ts || ''));
        tr.appendChild(el('td', null, r.agent || ''));
        tr.appendChild(el('td', 'dim', r.project || ''));
        var kind = el('td');
        kind.appendChild(el('span', typeClass(r.type), r.type || ''));
        tr.appendChild(kind);
        tr.appendChild(el('td', 'summary', r.summary || ''));
        table.appendChild(tr);
      });
      stamp();
    });
  }

  /* ---------- agents ---------- */
  function loadAgents() {
    fetchJSON('/api/agents').then(function (d) {
      var s = d.summary || {};
      byId('fleet-summary').textContent =
        s.alive + ' of ' + s.total + ' alive -- ' + s.working + ' working, ' +
        s.idle + ' idle, ' + s.shell + ' plain shell, ' + s.dead + ' absent';

      var table = byId('table-fleet');
      clear(table);
      headRow(table, ['Role', 'State', 'Doing now', 'Pane', 'Directory', 'Last event']);
      var rows = d.fleet || [];
      if (!rows.length) {
        emptyRow(table, 6, 'No role resolved. Set HARNESS_MC_ROLES, or start a pane named after one.');
      }
      rows.forEach(function (f) {
        var tr = el('tr');
        tr.appendChild(el('td', 'strong', f.role));
        var stateCell = el('td');
        stateCell.appendChild(el('span', 'state state--' + f.state, f.state));
        if (f.resolved === 'discovered') {
          stateCell.appendChild(el('span', 'hint', ' found by name'));
        }
        tr.appendChild(stateCell);
        tr.appendChild(el('td', 'summary', f.activity || '--'));
        tr.appendChild(el('td', 'mono dim', f.pane || '--'));
        tr.appendChild(el('td', 'mono dim', f.cwd || '--'));
        tr.appendChild(el('td', 'summary dim',
          (f.last_event && f.last_event.summary) || '--'));
        table.appendChild(tr);
      });
      stamp();
    });
  }

  /* ---------- schedule ---------- */
  function loadSchedule() {
    fetchJSON('/api/schedule').then(function (d) {
      var timers = byId('table-timers');
      clear(timers);
      headRow(timers, ['Unit', 'Starts', 'Next', 'Last']);
      if (!(d.timers || []).length) {
        emptyRow(timers, 4, 'No timer, or no user manager on this machine.');
      }
      (d.timers || []).forEach(function (t) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', t.unit || ''));
        tr.appendChild(el('td', 'dim', t.activates || ''));
        tr.appendChild(el('td', null, t.next || '--'));
        tr.appendChild(el('td', 'dim', t.last || '--'));
        timers.appendChild(tr);
      });

      var crons = byId('table-crons');
      clear(crons);
      headRow(crons, ['Schedule', 'Command']);
      if (!(d.crons || []).length) { emptyRow(crons, 2, 'Empty crontab.'); }
      (d.crons || []).forEach(function (c) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', c.schedule || ''));
        tr.appendChild(el('td', 'summary mono', c.command || ''));
        crons.appendChild(tr);
      });
      stamp();
    });
  }

  /* ---------- logs ---------- */
  function loadLogs() {
    var query = new URLSearchParams();
    [['agent', 'f-agent'], ['project', 'f-project'], ['type', 'f-type'],
      ['search', 'f-search']].forEach(function (pair) {
      var value = byId(pair[1]).value.trim();
      if (value) { query.set(pair[0], value); }
    });
    fetchJSON('/api/logs?' + query.toString()).then(function (d) {
      var table = byId('table-logs');
      clear(table);
      headRow(table, ['When', 'Role', 'Project', 'Kind', 'Summary', 'Signature']);
      var rows = d.events || [];
      if (!rows.length) { emptyRow(table, 6, 'Nothing matches.'); }
      rows.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', r.ts || ''));
        tr.appendChild(el('td', null, r.agent || ''));
        tr.appendChild(el('td', 'dim', r.project || ''));
        var kind = el('td');
        kind.appendChild(el('span', typeClass(r.type), r.type || ''));
        tr.appendChild(kind);
        tr.appendChild(el('td', 'summary', r.summary || ''));
        tr.appendChild(el('td', r.sig_valid ? 'sig-ok' : 'sig-bad',
          r.sig_valid ? 'valid' : 'TAMPERED'));
        table.appendChild(tr);
      });
      stamp();
    });
  }

  /* ---------- approvals ---------- */
  function approvalCard(item, canApprove) {
    var card = el('div', 'approval');
    var head = el('div', 'approval-head');
    head.appendChild(el('span', 'approval-name', item.name));
    if (item.frozen) {
      head.appendChild(el('span', 'flag flag--frozen', 'written review required'));
    }
    if (item.key_gated) {
      head.appendChild(el('span', 'flag flag--key', 'hardware key required'));
    }
    card.appendChild(head);

    var impact = item.impact || {};
    card.appendChild(el('p', 'approval-impact', impact.impact || ''));
    if (impact.scope) {
      card.appendChild(el('p', 'approval-meta', 'Scope: ' + impact.scope));
    }
    card.appendChild(el('p', 'approval-meta mono dim', item.script));
    card.appendChild(el('p', 'approval-meta mono dim',
      'sha256 ' + String(item.sha256 || 'unreadable').slice(0, 16)));
    if (item.reason) {
      card.appendChild(el('p', 'approval-meta', 'The engine stopped because: ' + item.reason));
    }

    var status = el('p', 'approval-status');
    if (!canApprove) {
      status.textContent = 'Read-only panel: no approve module is installed, ' +
        'so this can only be released outside the panel.';
      status.className = 'approval-status dim';
      card.appendChild(status);
      return card;
    }
    var button = el('button', 'primary', 'Approve');
    button.addEventListener('click', function () {
      button.disabled = true;
      status.textContent = 'sending...';
      fetchJSON('/api/approve', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ script: item.script, sha256: item.sha256 })
      }).then(function (r) {
        status.textContent = r.ok ? 'approved' : ('refused: ' + (r.error || r.__status));
        status.className = 'approval-status ' + (r.ok ? 'sig-ok' : 'sig-bad');
        if (!r.ok) { button.disabled = false; }
        loadApprovals();
      }).catch(function (e) {
        status.textContent = 'error: ' + e;
        button.disabled = false;
      });
    });
    card.appendChild(button);
    card.appendChild(status);
    return card;
  }

  function loadApprovals() {
    fetchJSON('/api/approvals').then(function (d) {
      var host = byId('approvals-list');
      clear(host);
      if (!d.engine_available) {
        host.appendChild(el('p', 'empty',
          'No execution engine audit journal was found, so nothing can be ' +
          'waiting. Point HARNESS_MC_EXECUTOR_AUDIT at yours if you run one.'));
        stamp();
        return;
      }
      var items = d.pending || [];
      if (!items.length) {
        host.appendChild(el('p', 'empty', 'Nothing is waiting on you.'));
        stamp();
        return;
      }
      items.forEach(function (item) {
        host.appendChild(approvalCard(item, !!d.can_approve));
      });
      stamp();
    });
  }

  /* ---------- views ---------- */
  var LOADERS = {
    overview: loadOverview,
    agents: loadAgents,
    schedule: loadSchedule,
    logs: loadLogs,
    approvals: loadApprovals
  };
  var REFRESH_MS = {
    overview: 30000, agents: 10000, schedule: 60000,
    logs: 10000, approvals: 15000
  };

  function show(view) {
    state.view = view;
    Object.keys(LOADERS).forEach(function (name) {
      byId('view-' + name).classList.toggle('hidden', name !== view);
    });
    var buttons = document.querySelectorAll('.tab');
    Array.prototype.forEach.call(buttons, function (b) {
      b.classList.toggle('active', b.getAttribute('data-view') === view);
    });
    if (window.location.hash.slice(1) !== view) {
      window.location.hash = view;
    }
    schedule();
    LOADERS[view]();
  }

  function schedule() {
    if (state.timer) { window.clearInterval(state.timer); }
    state.timer = window.setInterval(function () {
      if (state.view === 'logs' && !byId('f-tail').checked) { return; }
      LOADERS[state.view]();
    }, REFRESH_MS[state.view] || 30000);
  }

  /* ---------- wiring ---------- */
  Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
    b.addEventListener('click', function () { show(b.getAttribute('data-view')); });
  });
  byId('log-filters').addEventListener('submit', function (e) {
    e.preventDefault();
    loadLogs();
  });
  byId('btn-ingest').addEventListener('click', function () {
    var button = byId('btn-ingest');
    button.disabled = true;
    fetchJSON('/api/ingest', { method: 'POST', credentials: 'same-origin' })
      .then(function () { LOADERS[state.view](); })
      .catch(function () { /* the stamp simply will not move */ })
      .then(function () { button.disabled = false; });
  });
  window.addEventListener('hashchange', function () {
    var view = window.location.hash.slice(1);
    if (LOADERS[view] && view !== state.view) { show(view); }
  });

  var typeSelect = byId('f-type');
  EVENT_TYPES.forEach(function (kind) {
    typeSelect.appendChild(el('option', null, kind));
  });

  var initial = window.location.hash.slice(1);
  show(LOADERS[initial] ? initial : 'overview');
})();
