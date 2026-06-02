/* ============================================================
   AI Gateman — app.js  (v2 — fixed & enhanced)
   Camera capture, API calls, canvas overlay, auto-scan, particle bg
   ============================================================ */

// API endpoint — auto-switch between local and deployed based on environment:
const API = (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
    ? "http://localhost:8000"
    : "https://arpitcodes-gateman-api.hf.space";

// ─── DOM refs ────────────────────────────────────────────────
const video       = document.getElementById('video-feed');
const canvas      = document.getElementById('overlay-canvas');
const ctx         = canvas.getContext('2d');
const camSelect   = document.getElementById('camera-select');
const startBtn    = document.getElementById('start-btn');
const stopBtn     = document.getElementById('stop-btn');
const feedEmpty   = document.getElementById('feed-empty');
const serverDot   = document.getElementById('server-dot');
const serverLbl   = document.getElementById('server-label');
const statusText  = document.getElementById('status-text');          // fixed: was missing
const vehCount    = document.getElementById('vehicle-count');
const logList     = document.getElementById('log-list');
const cursorGlow  = document.getElementById('cursor-glow');
const pCanvas     = document.getElementById('particle-canvas');
const pCtx        = pCanvas.getContext('2d');
const metricTotal      = document.getElementById('metric-total');
const metricScanned    = document.getElementById('metric-scanned');
const metricRealtime   = document.getElementById('metric-realtime');  // fixed: now exists in HTML
const statusOverlay    = document.getElementById('status-overlay');
const manualScanBtn    = document.getElementById('manual-scan-btn');
const autoscanToggle   = document.getElementById('autoscan-toggle');
const autoscanKnob     = document.getElementById('autoscan-knob');

// ─── State ───────────────────────────────────────────────────
let stream         = null;
let autoScanLoop   = null;
let busy           = false;
let autoScanOn     = false;
let statusTimer    = null;   // timer to auto-dismiss status overlay
let totalVehicles  = 0;
let totalPlates    = 0;
let activityHistory = new Array(12).fill(0);
let plateBuffer    = [];

// ─── Levenshtein edit distance (for dedup) ───────────────────
const getEditDist = (a, b) => {
    if (a.length === 0) return b.length;
    if (b.length === 0) return a.length;
    const matrix = [];
    for (let i = 0; i <= b.length; i++) matrix[i] = [i];
    for (let j = 0; j <= a.length; j++) matrix[0][j] = j;
    for (let i = 1; i <= b.length; i++) {
        for (let j = 1; j <= a.length; j++) {
            matrix[i][j] = b[i-1] === a[j-1]
                ? matrix[i-1][j-1]
                : Math.min(matrix[i-1][j-1]+1, matrix[i][j-1]+1, matrix[i-1][j]+1);
        }
    }
    return matrix[b.length][a.length];
};

// ─── Mouse / Cursor Glow ─────────────────────────────────────
let mouse = { x: null, y: null, radius: 150 };
document.addEventListener('mousemove', e => {
    if (cursorGlow) {
        cursorGlow.style.left = e.clientX + 'px';
        cursorGlow.style.top  = e.clientY + 'px';
    }
    mouse.x = e.clientX;
    mouse.y = e.clientY;
});
document.addEventListener('mouseleave', () => { mouse.x = null; mouse.y = null; });

// ─── Particle Constellation System ───────────────────────────
let particles = [];
const colors = ['#4285F4','#EA4335','#FBBC05','#34A853','#8a2be2','#ff69b4','#00bcd4'];

function initParticles() {
    particles = [];
    const width  = pCanvas.width  = window.innerWidth;
    const height = pCanvas.height = window.innerHeight;
    const count  = Math.min(220, Math.floor((width * height) / 7000));
    for (let i = 0; i < count; i++) {
        const isClustered = Math.random() > 0.25;
        const x = isClustered ? width * 0.45 + Math.random() * (width * 0.55) : Math.random() * width;
        const y = Math.random() * height;
        particles.push({
            x, y, baseX: x, baseY: y,
            size: Math.random() * 2.8 + 1,
            color: colors[Math.floor(Math.random() * colors.length)],
            vx: (Math.random() - 0.5) * 0.2,
            vy: (Math.random() - 0.5) * 0.2,
            density: (Math.random() * 20) + 12
        });
    }
}

function animateParticles() {
    pCtx.clearRect(0, 0, pCanvas.width, pCanvas.height);
    for (let p of particles) {
        p.x += p.vx; p.y += p.vy;
        p.baseX += p.vx; p.baseY += p.vy;
        if (p.x < -20) p.x = p.baseX = pCanvas.width + 20;
        if (p.x > pCanvas.width + 20)  p.x = p.baseX = -20;
        if (p.y < -20) p.y = p.baseY = pCanvas.height + 20;
        if (p.y > pCanvas.height + 20) p.y = p.baseY = -20;
        if (mouse.x !== null && mouse.y !== null) {
            const dx = mouse.x - p.x, dy = mouse.y - p.y;
            const dist = Math.sqrt(dx*dx + dy*dy);
            if (dist < mouse.radius) {
                const f = (mouse.radius - dist) / mouse.radius;
                p.x -= (dx/dist) * f * p.density * 0.45;
                p.y -= (dy/dist) * f * p.density * 0.45;
            } else {
                p.x -= (p.x - p.baseX) * 0.08;
                p.y -= (p.y - p.baseY) * 0.08;
            }
        }
        // 4-pointed sparkle star
        pCtx.beginPath();
        for (let j = 0; j < 8; j++) {
            const r = (j % 2 === 0) ? p.size * 1.5 : p.size * 0.5;
            pCtx.lineTo(p.x + r * Math.sin(j * Math.PI / 4), p.y + r * Math.cos(j * Math.PI / 4));
        }
        pCtx.closePath();
        pCtx.fillStyle = p.color;
        pCtx.fill();
    }
    requestAnimationFrame(animateParticles);
}

window.addEventListener('resize', initParticles);
initParticles();
animateParticles();

// ─── Server Health Ping ───────────────────────────────────────
async function ping() {
    try {
        const r = await fetch(`${API}/health`);
        if (r.ok) {
            serverDot.style.background = '#22c55e';
            serverDot.style.boxShadow = '0 0 8px rgba(34,197,94,0.5)';
            if (statusText) statusText.textContent = 'LIVE SYSTEM ACTIVE';
            serverLbl.style.color = '#4ade80';
        } else throw 0;
    } catch {
        serverDot.style.background = '#f87171';
        serverDot.style.boxShadow = '0 0 8px rgba(248,113,113,0.5)';
        if (statusText) statusText.textContent = 'SYSTEM OFFLINE';
        serverLbl.style.color = '#f87171';
    }
}
ping();
setInterval(ping, 5000);

// ─── Export to Excel ─────────────────────────────────────────
function exportExcel() {
    window.open(`${API}/export-excel`, '_blank');
}

// ─── Camera Enumeration ──────────────────────────────────────
(async function listCams() {
    try {
        await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
        const devs = await navigator.mediaDevices.enumerateDevices();
        const cams = devs.filter(d => d.kind === 'videoinput');
        camSelect.innerHTML = '';
        if (!cams.length) { camSelect.innerHTML = '<option value="">No cameras</option>'; return; }
        let backCamIndex = -1;
        cams.forEach((d, i) => {
            const o = document.createElement('option');
            o.value = d.deviceId;
            o.text  = d.label || `Camera ${camSelect.length + 1}`;
            camSelect.appendChild(o);
            if (o.text.toLowerCase().match(/back|environment|rear/)) backCamIndex = i;
        });
        if (backCamIndex !== -1) camSelect.selectedIndex = backCamIndex;
        else if (cams.length > 1) camSelect.selectedIndex = cams.length - 1;
    } catch {
        camSelect.innerHTML = '<option value="">Permission denied</option>';
    }
})();

// ─── Auto-Scan Toggle ────────────────────────────────────────
function setAutoScan(on) {
    autoScanOn = on;
    autoscanToggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    // Animate the knob and track color
    if (on) {
        autoscanToggle.style.background = '#22c55e';
        autoscanKnob.style.left = 'calc(100% - 1.125rem)';
        autoscanKnob.style.background = '#fff';
    } else {
        autoscanToggle.style.background = '#334155';
        autoscanKnob.style.left = '0.125rem';
        autoscanKnob.style.background = '#94a3b8';
    }
    if (on) {
        // Start scanning immediately, then every 2 seconds
        if (!busy) detect();
        autoScanLoop = setInterval(() => { if (!busy) detect(); }, 2000);
    } else {
        if (autoScanLoop) { clearInterval(autoScanLoop); autoScanLoop = null; }
    }
}

autoscanToggle.addEventListener('click', () => {
    if (!stream) { alert('Start the camera first!'); return; }
    setAutoScan(!autoScanOn);
});

// ─── Start / Stop ────────────────────────────────────────────
async function start() {
    const id = camSelect.value;
    if (!id) return;
    try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: { exact: id } } });
        video.srcObject = stream;
        feedEmpty.style.display = 'none';
        startBtn.disabled = true;
        stopBtn.disabled  = false;
        video.onloadedmetadata = () => {
            canvas.width  = video.videoWidth;
            canvas.height = video.videoHeight;
        };
        manualScanBtn.style.display = 'flex';
        lucide.createIcons();
    } catch (e) {
        alert('Camera error: ' + e.message);
    }
}

function stop() {
    // Stop auto-scan first
    setAutoScan(false);
    if (stream) stream.getTracks().forEach(t => t.stop());
    video.srcObject = null;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    feedEmpty.style.display = 'flex';
    startBtn.disabled = false;
    stopBtn.disabled  = true;
    manualScanBtn.style.display = 'none';
    hideStatusOverlay();
}

manualScanBtn.addEventListener('click', detect);
startBtn.addEventListener('click', start);
stopBtn.addEventListener('click', stop);

// ─── Status Overlay Control ──────────────────────────────────
function showStatusOverlay(status) {
    if (statusTimer) clearTimeout(statusTimer);

    statusOverlay.textContent = status === 'ALLOWED' ? '✔ ALLOWED' : '✘ UNKNOWN';
    statusOverlay.className   = status === 'ALLOWED'
        ? 'status-overlay allowed'
        : 'status-overlay unknown';

    // Auto-dismiss after 3 seconds
    statusTimer = setTimeout(hideStatusOverlay, 3000);
}

function hideStatusOverlay() {
    statusOverlay.className = 'status-overlay';  // hides via CSS (translateY 100%)
    statusTimer = null;
}

// ─── Frame Capture → API ─────────────────────────────────────
async function detect() {
    if (busy || !stream) return;
    busy = true;

    const ogHtml = manualScanBtn.innerHTML;
    manualScanBtn.innerHTML = '<i data-lucide="loader-2" class="w-5 h-5 animate-spin"></i><span>Scanning...</span>';
    manualScanBtn.disabled  = true;
    lucide.createIcons();

    const tc = document.createElement('canvas');
    tc.width  = video.videoWidth;
    tc.height = video.videoHeight;
    tc.getContext('2d').drawImage(video, 0, 0);

    tc.toBlob(async blob => {
        if (!blob) { busy = false; return; }
        const fd = new FormData();
        fd.append('file', blob, 'frame.jpg');

        const t0 = performance.now();
        try {
            const r = await fetch(`${API}/detect`, { method: 'POST', body: fd });
            const latency = Math.round(performance.now() - t0);
            if (metricRealtime) metricRealtime.textContent = `${latency} ms`;

            if (r.ok) {
                const data = await r.json();
                draw(data);
                logPlates(data);
                if (data.plates && data.plates.length === 0 && statusText) {
                    statusText.textContent = 'No Plate Detected';
                    setTimeout(() => { if (statusText) statusText.textContent = 'Vehicle Detection Active'; }, 2000);
                }
            } else {
                throw new Error(`API returned ${r.status}`);
            }
        } catch (e) {
            console.error('API Error:', e);
            if (statusText) statusText.textContent = 'Connection Error';
            setTimeout(() => { if (statusText) statusText.textContent = 'Vehicle Detection Active'; }, 3000);
        }

        busy = false;
        manualScanBtn.innerHTML = ogHtml;
        manualScanBtn.disabled  = false;
        lucide.createIcons();
    }, 'image/jpeg', 0.85);
}

// ─── Canvas Overlay Rendering ────────────────────────────────
function draw(d) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Removed status banner logic

    // Draw car bounding boxes
    d.cars.forEach(c => {
        const [x1,y1,x2,y2] = c.box;
        ctx.fillStyle   = 'rgba(255,255,255,0.03)';
        ctx.fillRect(x1, y1, x2-x1, y2-y1);
        ctx.strokeStyle = 'rgba(255,255,255,0.75)';
        ctx.lineWidth   = 1.8;
        ctx.strokeRect(x1, y1, x2-x1, y2-y1);
        ctx.font        = '700 11px "Outfit",monospace';
        ctx.fillStyle   = 'rgba(255,255,255,0.8)';
        ctx.fillText('VEHICLE', x1 + 6, y1 + 15);
    });

    // Draw plate bounding boxes
    d.plates.forEach(p => {
        const [x1,y1,x2,y2] = p.box;
        const plateText  = p.text === '...' ? 'SCANNING…' : p.text;
        const plateColor = '#3b82f6';

        const now = new Date();
        const ts  = `${now.toLocaleDateString('en-GB')}  ${now.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit', second:'2-digit' })}`;

        ctx.fillStyle   = 'rgba(59,130,246,0.05)';
        ctx.fillRect(x1, y1, x2-x1, y2-y1);
        ctx.strokeStyle = plateColor;
        ctx.lineWidth   = 2.5;
        ctx.strokeRect(x1, y1, x2-x1, y2-y1);

        ctx.font      = '800 15px "Outfit",monospace';
        ctx.fillStyle = plateColor;
        ctx.fillText(plateText, x1, y1 - 8);

        const pw = ctx.measureText(plateText).width;
        ctx.font      = '500 11px "Outfit",sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.75)';
        ctx.fillText(ts, x1 + pw + 10, y1 - 8);
    });

    if (vehCount) vehCount.textContent = `${d.cars.length} Vehicle${d.cars.length !== 1 ? 's' : ''}`;
}

// ─── Log Panel ────────────────────────────────────────────────
function logPlates(d) {
    d.plates.forEach(p => {
        if (!p.text || p.text === '...' || p.text.length < 4) return;
        plateBuffer.push({
            text:      p.text.trim(),
            confidence: p.confidence || 0,
            type:      p.vehicle_type || 'Unknown',
            timestamp: Date.now()
        });
    });
}

function commitPlateToLog(p) {
    const empty = document.getElementById('log-empty-row');
    if (empty) empty.remove();

    totalPlates++;
    totalVehicles++;

    metricScanned.textContent    = totalPlates;
    metricTotal.textContent      = totalVehicles;

    activityHistory[11]++;

    const now    = new Date(p.timestamp);
    const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    const row = document.createElement('tr');
    row.className = 'hover:bg-slate-800/50 transition-colors text-slate-300';
    row.innerHTML = `
        <td class="px-4 py-3 font-mono text-[11px] whitespace-nowrap">${timeStr}</td>
        <td class="px-4 py-3 font-medium text-xs">${p.type}</td>
        <td class="px-4 py-3 font-bold text-white tracking-wider font-mono text-xs plate-text-cell">${p.text}</td>
    `;
    logList.prepend(row);
    if (logList.children.length > 50) logList.removeChild(logList.lastChild);
}

function processPlateBuffer() {
    if (plateBuffer.length === 0) return;

    // Group similar plates (edit distance ≤ 2) and pick the highest-confidence read
    const groups = [];
    plateBuffer.forEach(p => {
        let found = false;
        for (const g of groups) {
            if (getEditDist(g[0].text, p.text) <= 2) { g.push(p); found = true; break; }
        }
        if (!found) groups.push([p]);
    });

    groups.forEach(g => {
        g.sort((a, b) => b.confidence - a.confidence);
        const best = g[0];

        // Deduplicate against the last 10 log entries
        const existing = Array.from(logList.querySelectorAll('.plate-text-cell')).slice(0, 10);
        const isDup = existing.some(el => getEditDist(el.textContent.trim(), best.text) <= 2);
        if (!isDup) commitPlateToLog(best);
    });

    plateBuffer = [];
}

// ─── Activity Graph ───────────────────────────────────────────
function updateActivityGraph() {
    activityHistory.shift();
    activityHistory.push(0);
    const graph = document.getElementById('activity-graph');
    if (!graph) return;
    const bars  = graph.children;
    const maxVal = Math.max(2, ...activityHistory);
    for (let i = 0; i < 12 && bars[i]; i++) {
        bars[i].style.height = Math.max(4, (activityHistory[i] / maxVal) * 100) + '%';
    }
}

setInterval(updateActivityGraph,  5000);
setInterval(processPlateBuffer,   1000);
