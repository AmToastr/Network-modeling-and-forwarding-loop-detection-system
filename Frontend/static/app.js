const socket = io();
let cy      = null;
let appData = null;
let flapSet = new Set();

// ── Sockets ──────────────────────────────────────────────────────────
socket.on('log', msg => addLog(msg.data));
socket.on('done', () => {
  setStatus('done', 'Done');
  addLog('--- Complete ---', 'info');
  setRoundProgress(0, 0);   // hide progress bar when finished
  loadData();
});
socket.on('reload_data', loadData);

// Step 4 — live round counter
socket.on('round_progress', data => {
  setRoundProgress(data.current, data.total);
  setStatus('running', `Round ${data.current}/${data.total}`);
});

// ── Console ──────────────────────────────────────────────────────────
function addLog(text, type) {
  const con  = document.getElementById('console');
  const line = document.createElement('div');
  line.className = 'log-line ' + (
    type ? type :
    /error|Error/i.test(text)    ? 'error' :
    /!!|flap/i.test(text)        ? 'warn'  :
    /---|Mode|Round/i.test(text) ? 'info'  : ''
  );
  line.textContent = '> ' + text;
  con.appendChild(line);
  con.scrollTop = con.scrollHeight;
}

function setStatus(cls, txt) {
  document.getElementById('status-dot').className    = 'status-dot ' + cls;
  document.getElementById('status-text').textContent = txt;
}

// ── Round progress bar (Step 4) ──────────────────────────────────────
function setRoundProgress(current, total) {
  const wrap = document.getElementById('round-progress-wrap');
  const bar  = document.getElementById('round-progress-bar');
  const lbl  = document.getElementById('round-progress-label');

  if (!total) {
    wrap.style.display = 'none';
    return;
  }
  const pct = Math.round((current / total) * 100);
  wrap.style.display  = 'flex';
  bar.style.width     = pct + '%';
  lbl.textContent     = `Round ${current} / ${total}`;
}

// ── Run ──────────────────────────────────────────────────────────────
async function runMode(mode) {
  const ip            = document.getElementById('ip-input').value.trim();
  const poll_rounds   = parseInt(document.getElementById('rounds-input').value)   || null;
  const poll_interval = parseInt(document.getElementById('interval-input').value) || null;

  if (mode === 'single' && !ip) { addLog('Enter a switch IP for Single Mode.', 'warn'); return; }

  setStatus('running', mode[0].toUpperCase() + mode.slice(1) + '…');
  addLog(`Starting ${mode} mode${ip ? ' → ' + ip : ''}…`, 'info');

  await fetch('/api/run', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ mode, ip, poll_rounds, poll_interval })
  }).catch(e => addLog('Request failed: ' + e, 'error'));
}

// ── Data ─────────────────────────────────────────────────────────────
async function loadData() {
  const res = await fetch('/api/data');
  appData   = await res.json();
  flapSet   = new Set(appData.flap_events.map(f => f.hostid));
  buildSidebar();
  buildGraph();
  if (appData.hosts.length > 0)
    document.getElementById('graph-empty').style.display = 'none';
}

// Pre-fill rounds/interval inputs from server defaults on page load
async function loadConfig() {
  try {
    const res  = await fetch('/api/config');
    const conf = await res.json();
    document.getElementById('rounds-input').value   = conf.poll_rounds;
    document.getElementById('interval-input').value = conf.poll_interval;
  } catch (_) { /* non-fatal */ }
}

// ── Sidebar ──────────────────────────────────────────────────────────
function osColor(os) {
  return {
    boss:         '#4a90d9',
    comware:      '#9b7fd4',
    powerconnect: '#d97a4a',
    'dell-os10':  '#f0c040',
    'fs-switch':  '#4ad97a',
    vrp:          '#d94a7a',
    ios:          '#4ab8d9',
  }[(os || '').toLowerCase()] || '#888';
}

function buildSidebar() {
  document.getElementById('sw-count').textContent = appData.hosts.length;
  document.getElementById('switch-list').innerHTML = appData.hosts.map(h => {
    const hasFlap   = flapSet.has(h.hostid);
    const flapCount = appData.flap_events.filter(f => f.hostid === h.hostid).length;
    return `<div class="switch-item ${hasFlap ? 'flapping' : ''}" data-id="${h.hostid}" onclick="selectHost('${h.hostid}')">
      <div class="sw-dot" style="background:${hasFlap ? '#d94a4a' : osColor(h.os)}"></div>
      <div class="sw-text">
        <div class="sw-name">${h.hostname}</div>
        <div class="sw-ip">${h.ip_address}</div>
      </div>
      ${hasFlap ? `<span class="flap-badge">⚠ ${flapCount}</span>` : ''}
    </div>`;
  }).join('');
}

// ── Graph ────────────────────────────────────────────────────────────
function buildGraph() {
  const elements = [];
  const edgeSet  = new Set();

  appData.hosts.forEach(h => elements.push({ data: {
    id:      h.hostid,
    label:   h.hostname,
    ip:      h.ip_address,
    os:      h.os || '—',
    hardware: h.hardware || '—',
    hasFlap: flapSet.has(h.hostid),
    isCore:  h.hostid === '1',
  }}));

  appData.topology.forEach(t => {
    const key = [t.local_hostid, t.remote_hostid].sort().join('-');
    if (edgeSet.has(key)) return;
    edgeSet.add(key);
    elements.push({ data: {
      id:     key,
      source: t.local_hostid,
      target: t.remote_hostid,
      lport:  t.local_port  || '',
      rport:  t.remote_port || '',
    }});
  });

  if (cy) cy.destroy();

  cy = cytoscape({
    container: document.getElementById('cy'),
    elements,
    style: [
      { selector: 'node', style: {
          label:                'data(label)',
          color:                '#ffffff',
          'font-size':          '10px',
          'text-valign':        'bottom',
          'text-margin-y':      '4px',
          'text-outline-color': '#0f1117',
          'text-outline-width': '2px',
          'background-color':   n => n.data('hasFlap') ? '#d94a4a' : n.data('isCore') ? '#f0c040' : osColor(n.data('os')),
          'border-color':       n => n.data('hasFlap') ? '#ff7070' : n.data('isCore') ? '#ffe080' : '#2a2d3a',
          'border-width':       n => n.data('hasFlap') || n.data('isCore') ? 2 : 1,
          width:                n => n.data('isCore') ? 36 : 22,
          height:               n => n.data('isCore') ? 36 : 22,
      }},
      { selector: 'edge', style: {
          width: 1.5, 'line-color': '#2a3a4a', opacity: 0.8, 'curve-style': 'bezier',
      }},
      { selector: 'node:selected', style: { 'border-color': '#ffffff', 'border-width': 3 }},
      { selector: 'edge:selected', style: { 'line-color': '#4a90d9', width: 2.5 }},
    ],
    layout: {
      name: 'cose', idealEdgeLength: 100, nodeOverlap: 20,
      fit: true, padding: 40, randomize: false,
      nodeRepulsion: 400000, gravity: 80, numIter: 1000,
    }
  });

  cy.on('tap', 'node', e => { closeEdgePopup(); showNodeModal(e.target.data()); });
  cy.on('tap', 'edge', e => { closeNodeModal(); showEdgePopup(e.target.data(), e.renderedPosition); });
  cy.on('tap', e => { if (e.target === cy) { closeNodeModal(); closeEdgePopup(); } });
}

// ── Node modal ───────────────────────────────────────────────────────
function showNodeModal(d) {
  const flaps = (appData.flap_events || []).filter(f => f.hostid === d.id);

  document.getElementById('modal-title').textContent = d.label;

  // Basic info rows
  let html = [
    ['IP',       d.ip],
    ['OS',       d.os],
    ['Hardware', d.hardware || '—'],
    ['Flaps',    flaps.length],
  ].map(([k, v]) => `
    <div class="info-row">
      <span>${k}</span>
      <span class="info-val ${k === 'Flaps' && v > 0 ? 'warn' : ''}">${v}</span>
    </div>`
  ).join('');

  // All flap entries — no limit
  if (flaps.length > 0) {
    html += `<div class="flap-section-title">
      Flap events (${flaps.length})
      <button class="btn-clear-flaps" onclick="clearFlaps('${d.id}')">Clear This Switch</button>
    </div>`;
    flaps.forEach(f => {
      html += `<div class="flap-entry">
        <span class="flap-mac">${f.mac}</span>
        <span class="flap-vlan">VLAN ${f.vlan_id}</span>
        <div class="flap-ports">${f.from_port} → ${f.to_port}</div>
      </div>`;
    });
  }

  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('node-modal').classList.add('visible');

  // Reset position to centre each time
  const modal = document.getElementById('node-modal');
  modal.style.left      = '50%';
  modal.style.top       = '50%';
  modal.style.transform = 'translate(-50%, -50%)';

  // Highlight in sidebar
  document.querySelectorAll('.switch-item').forEach(el => el.classList.remove('selected'));
  const item = document.querySelector(`.switch-item[data-id="${d.id}"]`);
  if (item) { item.classList.add('selected'); item.scrollIntoView({ block: 'nearest' }); }
}

function closeNodeModal() {
  document.getElementById('node-modal').classList.remove('visible');
}

// ── Edge popup ────────────────────────────────────────────────────────
function showEdgePopup(d, pos) {
  const src  = appData.hosts.find(h => h.hostid === d.source);
  const tgt  = appData.hosts.find(h => h.hostid === d.target);
  if (!src || !tgt) return;

  const lport = d.lport || '—';
  const rport = d.rport || '—';

  const popup = document.getElementById('edge-popup');
  popup.innerHTML = `
    <div class="edge-popup-row">
      <span class="edge-popup-host">${src.hostname}</span>
      <span class="edge-popup-ip">${src.ip_address}</span>
      <span class="edge-popup-port">${lport}</span>
    </div>
    <div class="edge-popup-divider">⟷</div>
    <div class="edge-popup-row">
      <span class="edge-popup-host">${tgt.hostname}</span>
      <span class="edge-popup-ip">${tgt.ip_address}</span>
      <span class="edge-popup-port">${rport}</span>
    </div>`;

  // Position near the tap point, clamped inside the graph container
  const wrap  = document.getElementById('graph-wrap');
  const wRect = wrap.getBoundingClientRect();
  const popupW = 260, popupH = 90;
  let left = pos.x + 12;
  let top  = pos.y + 12;
  if (left + popupW > wRect.width)  left = pos.x - popupW - 8;
  if (top  + popupH > wRect.height) top  = pos.y - popupH - 8;

  popup.style.left    = left + 'px';
  popup.style.top     = top  + 'px';
  popup.style.display = 'block';
}

function closeEdgePopup() {
  document.getElementById('edge-popup').style.display = 'none';
}

function selectHost(id) {
  if (!cy) return;
  const n = cy.getElementById(id);
  if (n.length) {
    cy.animate({ fit: { eles: n, padding: 80 }, duration: 400 });
    showNodeModal(n.data());
  }
}

// ── Draggable modal ───────────────────────────────────────────────────
(function () {
  let dragging = false;
  let startX, startY, origLeft, origTop;

  const header = document.getElementById('modal-header');
  const modal  = document.getElementById('node-modal');

  header.addEventListener('mousedown', e => {
    dragging = true;
    const rect = modal.getBoundingClientRect();
    modal.style.transform = 'none';
    modal.style.left      = rect.left + 'px';
    modal.style.top       = rect.top  + 'px';
    startX   = e.clientX;
    startY   = e.clientY;
    origLeft = rect.left;
    origTop  = rect.top;
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    modal.style.left = (origLeft + e.clientX - startX) + 'px';
    modal.style.top  = (origTop  + e.clientY - startY) + 'px';
  });

  document.addEventListener('mouseup', () => { dragging = false; });
})();

// ── Controls ─────────────────────────────────────────────────────────
function resetLayout() {
  if (!cy) return;
  cy.layout({ name: 'cose', nodeRepulsion: 400000, idealEdgeLength: 100, padding: 40 }).run();
}

async function clearFlaps(hostid) {
  const isAll  = !hostid;
  const label  = isAll ? 'ALL switches' : (appData.hosts.find(h => h.hostid === hostid)?.hostname || hostid);
  const prompt = isAll
    ? 'Clear flap history for ALL switches?'
    : `Clear flap history for ${label}?`;

  if (!confirm(prompt)) return;

  const res  = await fetch('/api/flaps', {
    method:  'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(hostid ? { hostid } : {}),
  });
  const data = await res.json();

  if (res.ok) {
    addLog(`Cleared ${data.deleted} flap event(s) for ${data.scope}.`, 'info');
    if (!isAll) closeNodeModal();
    await loadData();
  } else {
    addLog(`Clear flaps failed: ${data.error || res.status}`, 'error');
  }
}

loadData();
loadConfig();