/* ============================================================
   AI Gateman — app.js (Apple Silicon & Multi-device Optimized)
   Camera capture, API calls, canvas overlay, auto-scan, particle bg
   ============================================================ */

// API endpoint: Always points to port 8000 on current hostname (works on localhost, LAN IPs, and remote)
const API = `${window.location.protocol}//${window.location.hostname || 'localhost'}:8000`;

// ─── Authentication Token Management ─────────────────────────
const AUTH_STORAGE_KEY = 'gateman_admin_token';
let isVerifyingToken = false;
let isDashboardLocked = false;
let resumeScanAfterUnlock = false;

function getAuthToken() {
    return sessionStorage.getItem(AUTH_STORAGE_KEY) || '';
}

function setAuthToken(token) {
    if (token) {
        sessionStorage.setItem(AUTH_STORAGE_KEY, token);
    } else {
        sessionStorage.removeItem(AUTH_STORAGE_KEY);
    }
}

function lockDashboard(reason = 'LOCKED - enter token') {
    isDashboardLocked = true;
    if (autoScanOn) {
        resumeScanAfterUnlock = true;
    }
    setAutoScan(false);
    if (autoScanTimer) {
        clearTimeout(autoScanTimer);
        autoScanTimer = null;
    }
    busy = false;

    if (statusOverlay) {
        statusOverlay.className = 'status-overlay unknown';
        statusOverlay.innerHTML = '<span class="flex items-center gap-2"><i data-lucide="lock" class="w-5 h-5"></i> LOCKED - enter token</span>';
        if (window.lucide) lucide.createIcons();
    }
    if (statusText) {
        statusText.textContent = 'LOCKED - enter token';
    }

    showTokenModal();
}

function showTokenModal(errorMessage = '') {
    const modal = document.getElementById('auth-token-modal');
    const input = document.getElementById('auth-token-input');
    const errBox = document.getElementById('auth-token-error');
    const errTxt = document.getElementById('auth-token-error-text');

    if (modal) {
        modal.classList.remove('hidden');
    }
    if (errBox && errTxt) {
        if (errorMessage) {
            errTxt.textContent = errorMessage;
            errBox.classList.remove('hidden');
        } else {
            errBox.classList.add('hidden');
        }
    }
    if (input) {
        input.value = '';
        setTimeout(() => input.focus(), 80);
    }
    if (window.lucide) lucide.createIcons();
}

function hideTokenModal() {
    const modal = document.getElementById('auth-token-modal');
    const errBox = document.getElementById('auth-token-error');
    if (modal) {
        modal.classList.add('hidden');
    }
    if (errBox) {
        errBox.classList.add('hidden');
    }
}

function handleUnauthorized() {
    setAuthToken('');
    lockDashboard('LOCKED - enter token');
}

async function submitAuthToken() {
    if (isVerifyingToken) return;
    const input = document.getElementById('auth-token-input');
    const submitBtn = document.getElementById('auth-token-submit-btn');
    const submitTxt = document.getElementById('auth-token-submit-text');
    const errBox = document.getElementById('auth-token-error');
    const errTxt = document.getElementById('auth-token-error-text');

    const candidate = input ? input.value.trim() : '';
    if (!candidate) {
        if (errBox && errTxt) {
            errTxt.textContent = 'Token cannot be empty';
            errBox.classList.remove('hidden');
            if (window.lucide) lucide.createIcons();
        }
        return;
    }

    isVerifyingToken = true;
    if (submitBtn) submitBtn.disabled = true;
    if (submitTxt) submitTxt.textContent = 'Verifying...';

    try {
        const res = await fetch(`${API}/whitelist`, {
            headers: { 'Authorization': `Bearer ${candidate}` }
        });

        if (res.ok) {
            setAuthToken(candidate);
            isDashboardLocked = false;
            hideTokenModal();
            hideStatusOverlay();

            if (statusText) {
                statusText.textContent = stream ? 'Optical Feed Active' : 'Ready';
            }

            loadWhitelist().then(loadHistoricalLogsAndStats).then(loadSystemInfo);

            if (stream && (resumeScanAfterUnlock || autoScanOn)) {
                resumeScanAfterUnlock = false;
                setAutoScan(true);
            }
        } else {
            const errData = await res.json().catch(() => ({}));
            const msg = errData.detail || (res.status === 401 ? 'Invalid authentication token' : `Server error: HTTP ${res.status}`);
            if (errBox && errTxt) {
                errTxt.textContent = msg;
                errBox.classList.remove('hidden');
                if (window.lucide) lucide.createIcons();
            }
            lockDashboard('LOCKED - enter token');
        }
    } catch (err) {
        if (errBox && errTxt) {
            errTxt.textContent = 'Connection error: ' + err.message;
            errBox.classList.remove('hidden');
            if (window.lucide) lucide.createIcons();
        }
    } finally {
        isVerifyingToken = false;
        if (submitBtn) submitBtn.disabled = false;
        if (submitTxt) submitTxt.textContent = 'Unlock Dashboard';
    }
}

async function fetchWithAuth(url, options = {}) {
    const token = getAuthToken();
    if (!token) {
        handleUnauthorized();
        return new Response(JSON.stringify({ detail: 'Token required' }), {
            status: 401,
            statusText: 'Unauthorized',
            headers: { 'Content-Type': 'application/json' }
        });
    }
    const opts = { ...options };
    opts.headers = new Headers(opts.headers || {});
    opts.headers.set('Authorization', `Bearer ${token}`);
    try {
        const res = await fetch(url, opts);
        if (res.status === 401) {
            handleUnauthorized();
        }
        return res;
    } catch (e) {
        throw e;
    }
}

// ─── DOM References ──────────────────────────────────────────
const video          = document.getElementById('video-feed');
const canvas         = document.getElementById('overlay-canvas');
const ctx            = canvas.getContext('2d');
const camSelect      = document.getElementById('camera-select');
const startBtn       = document.getElementById('start-btn');
const stopBtn        = document.getElementById('stop-btn');
const feedEmpty      = document.getElementById('feed-empty');
const serverDot      = document.getElementById('server-dot');
const serverLbl      = document.getElementById('server-label');
const statusText     = document.getElementById('status-text');
const vehCount       = document.getElementById('vehicle-count');
const logList        = document.getElementById('log-list');
const cursorGlow     = document.getElementById('cursor-glow');
const pCanvas        = document.getElementById('particle-canvas');
const pCtx           = pCanvas ? pCanvas.getContext('2d') : null;
const metricTotal    = document.getElementById('metric-total');
const metricScanned  = document.getElementById('metric-scanned');
const metricRealtime = document.getElementById('metric-realtime');
const statusOverlay  = document.getElementById('status-overlay');
const systemOfflineBanner = document.getElementById('system-offline-banner');
const manualScanBtn  = document.getElementById('manual-scan-btn');
const autoscanToggle = document.getElementById('autoscan-toggle');
const autoscanKnob   = document.getElementById('autoscan-knob');
const logCountBadge  = document.getElementById('log-count-badge');
const liveFps        = document.getElementById('live-fps');

// ─── State ───────────────────────────────────────────────────
let stream           = null;
let autoScanLoop     = null;
let busy             = false;
let autoScanOn       = false;
let isSystemOffline  = false;
let statusTimer      = null;
let totalVehicles    = 0;
let totalPlates      = 0;
let totalLogs        = 0;
let activityHistory  = new Array(12).fill(0);
let plateBuffer      = [];
let cachedWhitelist  = new Set();
let cachedSystemInfo = null;

async function loadSystemInfo() {
    try {
        const r = await fetchWithAuth(`${API}/system-info`);
        if (r.ok) {
            cachedSystemInfo = await r.json();
            if (cachedSystemInfo.status === 'degraded') {
                setSystemOffline(true);
            }
        }
    } catch (e) {
        console.warn('System info notice:', e);
    }
}

function setSystemOffline(offline) {
    isSystemOffline = Boolean(offline);
    const banner = document.getElementById('system-offline-banner');
    if (isSystemOffline) {
        if (banner) {
            banner.classList.remove('hidden');
        }
        if (autoScanOn) {
            setAutoScan(false);
        }
        if (typeof autoScanTimer !== 'undefined' && autoScanTimer) {
            clearTimeout(autoScanTimer);
            autoScanTimer = null;
        }
        if (statusOverlay) {
            if (statusTimer) clearTimeout(statusTimer);
            statusOverlay.innerHTML = '<span class="flex items-center gap-2"><i data-lucide="alert-triangle" class="w-5 h-5"></i> SYSTEM OFFLINE - MANUAL CONTROL</span>';
            statusOverlay.className = 'status-overlay unknown';
        }
        if (statusText) {
            statusText.textContent = 'SYSTEM OFFLINE - MANUAL CONTROL';
        }
        if (serverDot) {
            serverDot.style.background = '#ef4444';
            serverDot.style.boxShadow = '0 0 10px rgba(239,68,68,0.7)';
        }
        if (serverLbl) {
            serverLbl.style.color = '#f87171';
        }
        if (window.lucide) {
            lucide.createIcons();
        }
    } else {
        if (banner) {
            banner.classList.add('hidden');
        }
        if (statusOverlay && statusOverlay.innerHTML.includes('SYSTEM OFFLINE')) {
            hideStatusOverlay();
        }
    }
}

// ─── Levenshtein Edit Distance (for Plate Deduplication) ──────
const getEditDist = (a, b) => {
    if (!a) return (b || '').length;
    if (!b) return a.length;
    const matrix = [];
    for (let i = 0; i <= b.length; i++) matrix[i] = [i];
    for (let j = 0; j <= a.length; j++) matrix[0][j] = j;
    for (let i = 1; i <= b.length; i++) {
        for (let j = 1; j <= a.length; j++) {
            matrix[i][j] = b[i-1] === a[j-1]
                ? matrix[i-1][j-1]
                : Math.min(matrix[i-1][j-1] + 1, matrix[i][j-1] + 1, matrix[i-1][j] + 1);
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

// ─── Particle Constellation ──────────────────────────────────
let particles = [];
const colors = ['#3b82f6', '#10b981', '#6366f1', '#ec4899', '#06b6d4'];

function initParticles() {
    if (!pCanvas || !pCtx) return;
    particles = [];
    const width  = pCanvas.width  = window.innerWidth;
    const height = pCanvas.height = window.innerHeight;
    const count  = Math.min(120, Math.floor((width * height) / 10000));
    for (let i = 0; i < count; i++) {
        particles.push({
            x: Math.random() * width,
            y: Math.random() * height,
            baseX: Math.random() * width,
            baseY: Math.random() * height,
            size: Math.random() * 2 + 0.8,
            color: colors[Math.floor(Math.random() * colors.length)],
            vx: (Math.random() - 0.5) * 0.25,
            vy: (Math.random() - 0.5) * 0.25,
            density: (Math.random() * 20) + 10
        });
    }
}

function animateParticles() {
    if (!pCanvas || !pCtx) return;
    pCtx.clearRect(0, 0, pCanvas.width, pCanvas.height);
    for (let p of particles) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < -10) p.x = pCanvas.width + 10;
        if (p.x > pCanvas.width + 10) p.x = -10;
        if (p.y < -10) p.y = pCanvas.height + 10;
        if (p.y > pCanvas.height + 10) p.y = -10;

        pCtx.beginPath();
        pCtx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        pCtx.fillStyle = p.color;
        pCtx.fill();
    }
    requestAnimationFrame(animateParticles);
}

if (pCanvas) {
    window.addEventListener('resize', initParticles);
    initParticles();
    animateParticles();
}

// ─── Server Health Ping ───────────────────────────────────────
async function ping() {
    try {
        const r = await fetch(`${API}/health`, { cache: 'no-store' });
        if (r.ok) {
            const data = await r.json();
            if (data.status === 'degraded') {
                setSystemOffline(true);
            } else {
                setSystemOffline(false);
                serverDot.style.background = '#10b981';
                serverDot.style.boxShadow = '0 0 10px rgba(16,185,129,0.7)';
                if (statusText && (statusText.textContent.includes('OFFLINE') || statusText.textContent.includes('MANUAL CONTROL'))) {
                    statusText.textContent = 'ANPR Engine Active';
                }
                if (serverLbl) serverLbl.style.color = '#34d399';
            }
        } else throw 0;
    } catch {
        serverDot.style.background = '#ef4444';
        serverDot.style.boxShadow = '0 0 10px rgba(239,68,68,0.7)';
        if (statusText) statusText.textContent = 'BACKEND OFFLINE';
        if (serverLbl) serverLbl.style.color = '#f87171';
    }
}
ping();
setInterval(ping, 6000);

// ─── Whitelist Management ────────────────────────────────────
async function loadWhitelist() {
    try {
        const r = await fetchWithAuth(`${API}/whitelist`);
        if (r.ok) {
            const data = await r.json();
            cachedWhitelist = new Set(data.whitelist || []);
            renderWhitelistChips(data.whitelist || []);
        }
    } catch (e) {
        console.warn('Whitelist fetch notice:', e);
    }
}

function renderWhitelistChips(list) {
    const container = document.getElementById('whitelist-chips');
    if (!container) return;
    if (!list.length) {
        container.innerHTML = '<span class="text-xs text-slate-500">No plates whitelisted yet.</span>';
        return;
    }
    container.innerHTML = list.map(p => `
        <span class="inline-flex items-center gap-1.5 px-3 py-1 bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 rounded-lg text-xs font-mono font-bold group">
            <span>${p}</span>
            <button onclick="removePlate('${p}')" class="text-emerald-500/60 hover:text-rose-400 hover:bg-rose-500/20 rounded px-1 transition-colors ml-0.5" title="Remove from whitelist">×</button>
        </span>
    `).join('');
}

async function removePlate(plate) {
    if (!confirm(`Remove plate ${plate} from whitelist?`)) return;
    try {
        const r = await fetchWithAuth(`${API}/whitelist/remove`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ plate: plate })
        });
        if (r.ok) {
            loadWhitelist();
        } else if (r.status !== 401) {
            const err = await r.json().catch(() => ({}));
            alert('Failed to remove plate: ' + (err.detail || r.statusText));
        }
    } catch (e) {
        alert('Failed to remove plate: ' + e.message);
    }
}

async function addModalPlate() {
    const input = document.getElementById('modal-plate-input');
    const val = input.value.trim().toUpperCase();
    if (!val) return;
    try {
        const r = await fetchWithAuth(`${API}/whitelist/add`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ plate: val })
        });
        if (r.ok) {
            input.value = '';
            loadWhitelist();
        } else if (r.status !== 401) {
            const err = await r.json().catch(() => ({}));
            alert('Failed to add plate: ' + (err.detail || r.statusText));
        }
    } catch (e) {
        alert('Failed to add plate: ' + e.message);
    }
}

async function addQuickPlate() {
    const input = document.getElementById('quick-plate-input');
    const val = input.value.trim().toUpperCase();
    if (!val) return;
    try {
        const r = await fetchWithAuth(`${API}/whitelist/add`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ plate: val })
        });
        if (r.ok) {
            input.value = '';
            loadWhitelist();
        } else if (r.status !== 401) {
            const err = await r.json().catch(() => ({}));
            alert('Failed to add plate: ' + (err.detail || r.statusText));
        }
    } catch (e) {
        alert('Failed to add plate: ' + e.message);
    }
}

function toggleWhitelistModal() {
    const modal = document.getElementById('whitelist-modal');
    if (!modal) return;
    modal.classList.toggle('hidden');
    if (!modal.classList.contains('hidden')) {
        loadWhitelist();
    }
}

async function loadHistoricalLogsAndStats() {
    try {
        loadSystemInfo();
        const [statsRes, logsRes] = await Promise.all([
            fetchWithAuth(`${API}/stats`).catch(() => null),
            fetchWithAuth(`${API}/logs?limit=30`).catch(() => null)
        ]);

        if (statsRes && statsRes.ok) {
            const stats = await statsRes.json();
            totalVehicles = stats.total_scans || 0;
            totalPlates = stats.unique_plates || 0;
            totalLogs = stats.total_scans || 0;
            if (metricTotal) metricTotal.textContent = totalVehicles;
            if (metricScanned) metricScanned.textContent = totalPlates;
            if (logCountBadge) logCountBadge.textContent = `${totalLogs} detection${totalLogs !== 1 ? 's' : ''} logged`;
        }

        if (logsRes && logsRes.ok) {
            const data = await logsRes.json();
            if (data.logs && data.logs.length > 0) {
                const emptyRow = document.getElementById('log-empty-row');
                if (emptyRow) emptyRow.remove();

                data.logs.forEach(item => {
                    let statusBadge = '';
                    if (item.status === 'ALLOWED') {
                        statusBadge = '<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">✔ ALLOWED</span>';
                    } else if (item.status === 'NEEDS_CONFIRM') {
                        statusBadge = '<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/10 text-amber-300 border border-amber-500/40">⚠ CONFIRM</span>';
                    } else {
                        statusBadge = '<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/30">✘ UNKNOWN</span>';
                    }

                    const row = document.createElement('tr');
                    row.className = 'hover:bg-slate-800/40 transition-colors text-slate-300';
                    row.innerHTML = `
                        <td class="px-4 py-2.5 font-mono text-[11px] whitespace-nowrap text-slate-400">${item.time}</td>
                        <td class="px-4 py-2.5 font-medium text-xs text-slate-200">Vehicle</td>
                        <td class="px-4 py-2.5 font-bold text-white tracking-wider font-mono text-xs plate-text-cell">${item.plate}</td>
                        <td class="px-4 py-2.5 font-mono text-[11px] text-slate-400">95%</td>
                        <td class="px-4 py-2.5">${statusBadge}</td>
                    `;
                    logList.appendChild(row);
                });
            }
        }
    } catch (e) {
        console.warn('Initial stats load notice:', e);
    }
}

// Initial authorization check
if (!getAuthToken()) {
    lockDashboard('LOCKED - enter token');
} else {
    loadWhitelist().then(loadHistoricalLogsAndStats);
}

// ─── Reset Counters and Clear Detection Logs ───────────────────
async function clearDetectionLogs() {
    if (!confirm('Are you sure you want to reset all vehicle counters and clear the detection log history to 0?')) {
        return;
    }
    try {
        const res = await fetchWithAuth(`${API}/clear-logs`, { method: 'POST' });
        if (res.ok) {
            // Reset state counters
            totalVehicles = 0;
            totalPlates = 0;
            totalLogs = 0;
            activityHistory = new Array(12).fill(0);
            const graph = document.getElementById('activity-graph');
            if (graph) {
                Array.from(graph.children).forEach(bar => { bar.style.height = '6%'; });
            }

            // Update UI elements
            if (metricTotal) metricTotal.textContent = '0';
            if (metricScanned) metricScanned.textContent = '0';
            if (logCountBadge) logCountBadge.textContent = '0 detections logged';

            // Reset table to empty state
            if (logList) {
                logList.innerHTML = `
                    <tr id="log-empty-row">
                        <td colspan="5" class="py-8 text-center text-slate-500 text-xs font-sans">
                            Awaiting camera detection...
                        </td>
                    </tr>
                `;
            }
            if (window.lucide) lucide.createIcons();
        } else if (res.status !== 401) {
            alert('Failed to reset logs: Server returned error');
        }
    } catch (e) {
        alert('Failed to reset logs: ' + e.message);
    }
}

// ─── Export to Excel ─────────────────────────────────────────
async function exportExcel() {
    try {
        const r = await fetchWithAuth(`${API}/export-excel`);
        if (!r.ok) {
            throw new Error(`Server returned HTTP ${r.status}`);
        }
        const blob = await r.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const nowStr = new Date().toISOString().slice(0, 10).replace(/-/g, '');
        a.download = `Gateman_Report_${nowStr}.xlsx`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
    } catch (e) {
        alert('Failed to export report: ' + e.message);
    }
}

// ─── Camera Enumeration (Releases test stream immediately) ───
(async function listCams() {
    try {
        // Request temporary permission to read hardware labels
        const tempStream = await navigator.mediaDevices.getUserMedia({ video: true });
        tempStream.getTracks().forEach(t => t.stop()); // Free camera immediately!

        const devs = await navigator.mediaDevices.enumerateDevices();
        const cams = devs.filter(d => d.kind === 'videoinput');
        camSelect.innerHTML = '';
        if (!cams.length) {
            camSelect.innerHTML = '<option value="">No camera found</option>';
            return;
        }
        let preferredIndex = 0;
        cams.forEach((d, i) => {
            const o = document.createElement('option');
            o.value = d.deviceId;
            o.text  = d.label || `Camera ${i + 1}`;
            camSelect.appendChild(o);
            // Default to external or back/environment camera if present
            if (o.text.toLowerCase().match(/back|rear|environment|external|usb/)) {
                preferredIndex = i;
            }
        });
        camSelect.selectedIndex = preferredIndex;
    } catch (err) {
        console.warn('Camera enumeration note:', err);
        camSelect.innerHTML = '<option value="">Default Webcam</option>';
    }
})();

// ─── Continuous High-Speed Auto-Scan ─────────────────────────
let autoScanTimer = null;

function triggerNextScan() {
    if (!autoScanOn || !stream || isDashboardLocked || isSystemOffline || !getAuthToken()) return;
    if (autoScanTimer) clearTimeout(autoScanTimer);
    autoScanTimer = setTimeout(() => {
        if (autoScanOn && !busy && stream && !isDashboardLocked && !isSystemOffline && getAuthToken()) {
            detect();
        }
    }, 30); // 30ms ultra-low throttle keeps inference continuous at full GPU speed
}

function setAutoScan(on) {
    if (on && isSystemOffline) {
        console.warn('Cannot enable auto-scan while system is offline.');
        return;
    }
    if (on && (!getAuthToken() || isDashboardLocked)) {
        lockDashboard('LOCKED - enter token');
        return;
    }
    autoScanOn = on;
    autoscanToggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (on) {
        autoscanToggle.style.background = '#10b981';
        autoscanKnob.style.left = 'calc(100% - 1.35rem)';
        autoscanKnob.style.background = '#fff';
        if (!busy && stream) detect();
    } else {
        autoscanToggle.style.background = '#334155';
        autoscanKnob.style.left = '0.125rem';
        autoscanKnob.style.background = '#94a3b8';
        if (autoScanTimer) {
            clearTimeout(autoScanTimer);
            autoScanTimer = null;
        }
    }
}

autoscanToggle.addEventListener('click', () => {
    if (!stream) {
        alert('Please click "Start" to activate the camera first!');
        return;
    }
    setAutoScan(!autoScanOn);
});

// ─── Start / Stop Camera ─────────────────────────────────────
async function start() {
    const id = camSelect.value;
    const constraints = id
        ? { video: { deviceId: { exact: id }, width: { ideal: 1280 }, height: { ideal: 720 } } }
        : { video: { width: { ideal: 1280 }, height: { ideal: 720 } } };

    try {
        stream = await navigator.mediaDevices.getUserMedia(constraints);
        video.srcObject = stream;
        feedEmpty.style.display = 'none';
        startBtn.disabled = true;
        stopBtn.disabled  = false;

        const updateDimensions = () => {
            if (video.videoWidth && video.videoHeight) {
                canvas.width  = video.videoWidth;
                canvas.height = video.videoHeight;
            }
        };

        if (video.readyState >= 1) {
            updateDimensions();
        } else {
            video.onloadedmetadata = updateDimensions;
        }

        manualScanBtn.style.display = 'flex';
        lucide.createIcons();

        // Start scanning automatically 500ms after optical feed stabilizes
        setTimeout(() => {
            setAutoScan(true);
        }, 500);
    } catch (e) {
        alert('Camera start error: ' + e.message);
    }
}

function stop() {
    setAutoScan(false);
    if (autoScanTimer) {
        clearTimeout(autoScanTimer);
        autoScanTimer = null;
    }
    if (stream) {
        stream.getTracks().forEach(t => t.stop());
        stream = null;
    }
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

// ─── Audio Chimes & AI Voice Synthesizer ──────────────────────
const AudioContextClass = window.AudioContext || window.webkitAudioContext;
let audioCtx = null;
let soundEnabled = true;
const lastSpokenPlates = new Map();

function initAudio() {
    if (!audioCtx && AudioContextClass) {
        audioCtx = new AudioContextClass();
    }
    if (audioCtx && audioCtx.state === 'suspended') {
        audioCtx.resume();
    }
}
document.addEventListener('click', initAudio, { once: true });

function toggleSound() {
    soundEnabled = !soundEnabled;
    const btn = document.getElementById('sound-toggle-btn');
    const label = document.getElementById('sound-label');
    const icon = document.getElementById('sound-icon');
    if (soundEnabled) {
        initAudio();
        if (label) label.textContent = 'Audio: ON';
        if (btn) btn.className = 'p-2.5 px-4 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-xl transition-all flex items-center gap-2 text-xs font-semibold shadow-sm';
        if (icon) {
            icon.setAttribute('data-lucide', 'volume-2');
            icon.className = 'w-4 h-4 text-emerald-400';
        }
        playTone(520, 'sine', 0.15, 0, 0.15);
    } else {
        if (label) label.textContent = 'Audio: MUTED';
        if (btn) btn.className = 'p-2.5 px-4 bg-slate-800/60 hover:bg-slate-800 text-slate-400 border border-slate-800 rounded-xl transition-all flex items-center gap-2 text-xs font-semibold';
        if (icon) {
            icon.setAttribute('data-lucide', 'volume-x');
            icon.className = 'w-4 h-4 text-slate-500';
        }
        if (window.speechSynthesis) window.speechSynthesis.cancel();
    }
    lucide.createIcons();
}

function playTone(freq, type, duration, delay = 0, gainLevel = 0.2) {
    if (!soundEnabled) return;
    initAudio();
    if (!audioCtx) return;
    
    setTimeout(() => {
        try {
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.type = type;
            osc.frequency.setValueAtTime(freq, audioCtx.currentTime);
            gain.gain.setValueAtTime(gainLevel, audioCtx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + duration);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + duration);
        } catch (e) {
            console.warn('Audio play notice:', e);
        }
    }, delay);
}

function playAccessGrantedChime() {
    playTone(523.25, 'sine', 0.18, 0, 0.20);
    playTone(659.25, 'sine', 0.18, 90, 0.20);
    playTone(783.99, 'sine', 0.35, 180, 0.25);
}

function playAccessDeniedBuzzer() {
    playTone(220, 'sawtooth', 0.14, 0, 0.16);
    playTone(196, 'sawtooth', 0.22, 110, 0.18);
}

function formatPlateForSpeech(plate) {
    return plate.split('').join(' ');
}

function speakVoice(text) {
    if (!soundEnabled || !('speechSynthesis' in window)) return;
    try {
        window.speechSynthesis.cancel();
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.rate = 1.05;
        utterance.pitch = 1.0;
        utterance.volume = 0.95;
        window.speechSynthesis.speak(utterance);
    } catch (e) {
        console.warn('Speech synthesis notice:', e);
    }
}

// ─── Automated Boom Barrier Controller ────────────────────────
let barrierCountdown = 0;
let barrierInterval = null;
let gateIsOpen = false;

function openBoomBarrier(plate) {
    gateIsOpen = true;
    if (barrierInterval) clearInterval(barrierInterval);

    const arm = document.getElementById('barrier-arm');
    const beacon = document.getElementById('gate-beacon');
    const badge = document.getElementById('gate-status-badge');
    const tipLed = document.getElementById('barrier-tip-led');
    const actionTxt = document.getElementById('gate-action-text');
    const timerTxt = document.getElementById('gate-timer-text');

    if (arm) arm.style.transform = 'rotate(-75deg)';
    if (beacon) {
        beacon.style.background = '#10b981';
        beacon.style.boxShadow = '0 0 16px rgba(16,185,129,0.95)';
    }
    if (tipLed) tipLed.style.background = '#10b981';
    if (badge) {
        badge.className = 'px-2 py-0.5 rounded-full text-[10px] font-bold font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
        badge.textContent = '✔ GATE OPEN';
    }
    if (actionTxt) {
        actionTxt.textContent = 'OPEN • PROCEED';
        actionTxt.className = 'text-[11px] font-bold text-emerald-400 font-mono';
    }

    // 5-second countdown timer before automatic closure
    barrierCountdown = 5;
    if (timerTxt) timerTxt.textContent = `Auto-Closing: ${barrierCountdown}s`;

    barrierInterval = setInterval(() => {
        barrierCountdown--;
        if (barrierCountdown > 0) {
            if (timerTxt) timerTxt.textContent = `Auto-Closing: ${barrierCountdown}s`;
        } else {
            clearInterval(barrierInterval);
            barrierInterval = null;
            closeBoomBarrier();
        }
    }, 1000);
}

function closeBoomBarrier() {
    gateIsOpen = false;
    if (barrierInterval) clearInterval(barrierInterval);

    const arm = document.getElementById('barrier-arm');
    const beacon = document.getElementById('gate-beacon');
    const badge = document.getElementById('gate-status-badge');
    const tipLed = document.getElementById('barrier-tip-led');
    const actionTxt = document.getElementById('gate-action-text');
    const timerTxt = document.getElementById('gate-timer-text');

    if (arm) arm.style.transform = 'rotate(0deg)';
    if (beacon) {
        beacon.style.background = '#ef4444';
        beacon.style.boxShadow = '0 0 12px rgba(239,68,68,0.9)';
    }
    if (tipLed) tipLed.style.background = '#ef4444';
    if (badge) {
        badge.className = 'px-2 py-0.5 rounded-full text-[10px] font-bold font-mono bg-rose-500/10 text-rose-400 border border-rose-500/30';
        badge.textContent = '● BARRIER CLOSED';
    }
    if (actionTxt) {
        actionTxt.textContent = 'STANDBY';
        actionTxt.className = 'text-[11px] font-bold text-slate-300 font-mono';
    }
    if (timerTxt) timerTxt.textContent = 'Gate Secured';
}

function flashRestrictedBarrier() {
    const beacon = document.getElementById('gate-beacon');
    const actionTxt = document.getElementById('gate-action-text');
    const timerTxt = document.getElementById('gate-timer-text');

    if (actionTxt) {
        actionTxt.textContent = 'ACCESS DENIED';
        actionTxt.className = 'text-[11px] font-bold text-rose-400 font-mono animate-pulse';
    }
    if (timerTxt) timerTxt.textContent = 'Unauthorized Plate';

    if (beacon) {
        beacon.style.boxShadow = '0 0 25px rgba(239,68,68,1)';
        setTimeout(() => {
            if (!gateIsOpen && beacon) beacon.style.boxShadow = '0 0 12px rgba(239,68,68,0.9)';
            if (!gateIsOpen && actionTxt) {
                actionTxt.textContent = 'STANDBY';
                actionTxt.className = 'text-[11px] font-bold text-slate-300 font-mono';
            }
            if (!gateIsOpen && timerTxt) timerTxt.textContent = 'Gate Secured';
        }, 3000);
    }
}

function manualOpenGate() {
    initAudio();
    playAccessGrantedChime();
    speakVoice("Manual gate override activated. Barrier opening.");
    openBoomBarrier("MANUAL");
}

function manualCloseGate() {
    initAudio();
    playTone(300, 'sine', 0.2);
    speakVoice("Manual barrier closed.");
    closeBoomBarrier();
}

function handleGateAccess(plateText, status) {
    if (isSystemOffline) {
        flashRestrictedBarrier();
        return;
    }
    const clean = plateText.trim();
    const now = Date.now();
    const lastSpoken = lastSpokenPlates.get(clean) || 0;

    if (status === 'ALLOWED') {
        openBoomBarrier(clean);
        // Play chime and speak if not spoken within 7 seconds
        if (now - lastSpoken > 7000) {
            lastSpokenPlates.set(clean, now);
            playAccessGrantedChime();
            speakVoice(`Access Granted. Vehicle ${formatPlateForSpeech(clean)} authorized.`);
        }
    } else if (status === 'NEEDS_CONFIRM') {
        // MUST NOT open the barrier or play the granted sound for NEEDS_CONFIRM!
        flashRestrictedBarrier();
        if (now - lastSpoken > 7000) {
            lastSpokenPlates.set(clean, now);
            playTone(480, 'triangle', 0.25); // Subtle alert for guard, NOT granted chime
            speakVoice(`Verification needed for vehicle ${formatPlateForSpeech(clean)}. Guard please confirm.`);
        }
    } else {
        flashRestrictedBarrier();
        if (now - lastSpoken > 7000) {
            lastSpokenPlates.set(clean, now);
            playAccessDeniedBuzzer();
            speakVoice(`Access Restricted. Unregistered vehicle.`);
        }
    }
}

// ─── Status Overlay Control ──────────────────────────────────
function showStatusOverlay(status) {
    if (!statusOverlay) return;
    if (statusTimer) clearTimeout(statusTimer);

    if (isSystemOffline) {
        if (status === 'ALLOWED') return; // NEVER show ALLOWED
        statusOverlay.innerHTML = '<span class="flex items-center gap-2"><i data-lucide="alert-triangle" class="w-5 h-5"></i> SYSTEM OFFLINE - MANUAL CONTROL</span>';
        statusOverlay.className = 'status-overlay unknown';
        if (window.lucide) lucide.createIcons();
        return;
    }

    if (status === 'ALLOWED') {
        statusOverlay.innerHTML = '<span class="flex items-center gap-2"><i data-lucide="shield-check" class="w-5 h-5"></i> ACCESS GRANTED • AUTHORIZED</span>';
        statusOverlay.className = 'status-overlay allowed';
    } else if (status === 'NEEDS_CONFIRM') {
        statusOverlay.innerHTML = '<span class="flex items-center gap-2 text-amber-300"><i data-lucide="alert-triangle" class="w-5 h-5 text-amber-400"></i> MANUAL CHECK • CONFIRM PLATE</span>';
        statusOverlay.className = 'status-overlay unknown';
    } else if (status === 'UNKNOWN') {
        statusOverlay.innerHTML = '<span class="flex items-center gap-2"><i data-lucide="shield-alert" class="w-5 h-5"></i> ACCESS RESTRICTED • UNKNOWN PLATE</span>';
        statusOverlay.className = 'status-overlay unknown';
    } else {
        hideStatusOverlay();
        return;
    }

    lucide.createIcons();
    statusTimer = setTimeout(hideStatusOverlay, 3500);
}

function hideStatusOverlay() {
    if (!statusOverlay) return;
    statusOverlay.className = 'status-overlay';
    statusTimer = null;
}

// ─── Frame Capture → API ─────────────────────────────────────
async function detect() {
    const token = getAuthToken();
    if (!token || isDashboardLocked) {
        lockDashboard('LOCKED - enter token');
        return;
    }
    if (busy || !stream || !video.videoWidth) return;
    busy = true;

    const originalBtn = manualScanBtn.innerHTML;
    if (!autoScanOn) {
        manualScanBtn.innerHTML = '<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i><span>Scanning...</span>';
        manualScanBtn.disabled  = true;
        lucide.createIcons();
    }

    const vw = video.videoWidth;
    const vh = video.videoHeight;

    // Ensure display canvas matches live video feed dimensions
    if (canvas.width !== vw || canvas.height !== vh) {
        canvas.width  = vw;
        canvas.height = vh;
    }

    // Capture at full native resolution for crystal-clear OCR character recognition
    const tempCanvas = document.createElement('canvas');
    tempCanvas.width  = vw;
    tempCanvas.height = vh;
    tempCanvas.getContext('2d').drawImage(video, 0, 0);

    tempCanvas.toBlob(async blob => {
        if (!blob) {
            busy = false;
            if (!autoScanOn) {
                manualScanBtn.innerHTML = originalBtn;
                manualScanBtn.disabled  = false;
                lucide.createIcons();
            }
            triggerNextScan();
            return;
        }

        const fd = new FormData();
        fd.append('file', blob, 'frame.jpg');

        const t0 = performance.now();
        try {
            const r = await fetchWithAuth(`${API}/detect`, { method: 'POST', body: fd });
            const latency = Math.round(performance.now() - t0);
            if (metricRealtime) metricRealtime.textContent = `${latency} ms`;

            if (r.ok) {
                const data = await r.json();

                draw(data);
                logPlates(data);

                if (data.plates && data.plates.length > 0 && data.plates[0].text !== '...') {
                    const topPlate = data.plates[0];
                    if (!isSystemOffline && topPlate.status) {
                        showStatusOverlay(topPlate.status);
                    }
                    if (statusText && !isSystemOffline) {
                        statusText.textContent = `Scanned: ${topPlate.text}`;
                    }
                    if (!isSystemOffline) {
                        handleGateAccess(topPlate.text, topPlate.status);
                    }
                } else {
                    if (!isSystemOffline && data.status) {
                        showStatusOverlay(data.status);
                    }
                    if (statusText && !isSystemOffline) {
                        statusText.textContent = 'Optical Feed Active';
                    }
                }
            } else {
                if (r.status === 503) {
                    setSystemOffline(true);
                } else if (r.status === 401) {
                    hideStatusOverlay();
                    if (statusOverlay) {
                        statusOverlay.className = 'status-overlay unknown';
                        statusOverlay.innerHTML = '<span class="flex items-center gap-2"><i data-lucide="shield-alert" class="w-5 h-5"></i> 401 UNAUTHORIZED</span>';
                        if (window.lucide) lucide.createIcons();
                    }
                }
                throw new Error(`API returned HTTP ${r.status}`);
            }
        } catch (e) {
            console.error('API Error:', e);
            if (!isSystemOffline && statusText && statusText.textContent !== 'AUTH ERROR (401)') {
                statusText.textContent = 'Connection Error';
            }
        } finally {
            busy = false;
            if (!autoScanOn) {
                manualScanBtn.innerHTML = originalBtn;
                manualScanBtn.disabled  = false;
                lucide.createIcons();
            }
            if (!isSystemOffline) {
                triggerNextScan();
            }
        }
    }, 'image/jpeg', 0.85);
}

// ─── Canvas Overlay Rendering ────────────────────────────────
function draw(d) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // 1. Draw vehicle bounding boxes
    d.cars.forEach(c => {
        const [x1, y1, x2, y2] = c.box;
        const w = x2 - x1;
        const h = y2 - y1;

        ctx.fillStyle   = 'rgba(255, 255, 255, 0.02)';
        ctx.fillRect(x1, y1, w, h);

        ctx.strokeStyle = 'rgba(255, 255, 255, 0.65)';
        ctx.lineWidth   = 1.5;
        ctx.strokeRect(x1, y1, w, h);

        // Vehicle badge
        const label = `${c.vehicle_type || 'VEHICLE'} (${Math.round((c.confidence || 0.9) * 100)}%)`;
        ctx.font = '700 11px "Outfit", sans-serif';
        const tw = ctx.measureText(label).width;

        ctx.fillStyle = 'rgba(15, 23, 42, 0.85)';
        ctx.fillRect(x1, Math.max(0, y1 - 20), tw + 12, 20);

        ctx.fillStyle = '#ffffff';
        ctx.fillText(label, x1 + 6, Math.max(14, y1 - 6));
    });

    // 2. Draw license plate bounding boxes
    d.plates.forEach(p => {
        const [x1, y1, x2, y2] = p.box;
        const w = x2 - x1;
        const h = y2 - y1;
        const isAllowed = !isSystemOffline && p.status === 'ALLOWED';
        const isConfirm = p.status === 'NEEDS_CONFIRM';
        const plateColor = p.text === '...' ? '#38bdf8' : (isAllowed ? '#10b981' : (isConfirm ? '#f59e0b' : '#ef4444'));

        // Glowing border
        ctx.save();
        ctx.shadowColor = plateColor;
        ctx.shadowBlur = 12;
        ctx.strokeStyle = plateColor;
        ctx.lineWidth = 3;
        ctx.strokeRect(x1, y1, w, h);
        ctx.restore();

        // Shaded plate crop area
        ctx.fillStyle = isAllowed ? 'rgba(16, 185, 129, 0.12)' : (isConfirm ? 'rgba(245, 158, 11, 0.15)' : 'rgba(239, 68, 68, 0.12)');
        ctx.fillRect(x1, y1, w, h);

        // Plate Header Badge
        const displayText = p.text === '...' ? 'SCANNING...' : p.text;
        ctx.font = '800 15px "Outfit", monospace';
        const ptw = ctx.measureText(displayText).width;

        const badgeY = Math.max(24, y1 - 8);
        ctx.fillStyle = 'rgba(11, 15, 25, 0.92)';
        ctx.fillRect(x1, badgeY - 18, ptw + 16, 22);

        ctx.fillStyle = plateColor;
        ctx.fillText(displayText, x1 + 8, badgeY - 2);

        // Subtle timestamp tag
        const now = new Date();
        const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        ctx.font = '600 10px "Inter", sans-serif';
        ctx.fillStyle = 'rgba(255, 255, 255, 0.7)';
        ctx.fillText(timeStr, x1 + ptw + 22, badgeY - 3);
    });

    if (vehCount) {
        vehCount.textContent = `${d.cars.length} Vehicle${d.cars.length !== 1 ? 's' : ''}`;
    }
}

// ─── Instant Plate Logging (Zero Buffer Lag) ────────────────
const lastSeenPlates = new Map();

function logPlates(d) {
    if (!d.plates) return;
    const now = Date.now();
    d.plates.forEach(p => {
        if (!p.text || p.text === '...' || p.text === 'SCANNING...' || p.text.length < 6) return;
        if (p.confidence && p.confidence < 0.40) return;

        const cleanText = p.text.trim();
        const lastSeen = lastSeenPlates.get(cleanText) || 0;

        // 4-second cooldown per plate so standing vehicles don't spam duplicate rows
        if (now - lastSeen > 4000) {
            lastSeenPlates.set(cleanText, now);
            let effectiveStatus = p.status || 'UNKNOWN';
            if (isSystemOffline && effectiveStatus === 'ALLOWED') {
                effectiveStatus = 'UNKNOWN';
            }
            commitPlateToLog({
                text: cleanText,
                confidence: p.confidence || 0.9,
                type: p.vehicle_type || 'Vehicle',
                status: effectiveStatus,
                timestamp: now
            });
        }
    });
}

function commitPlateToLog(p) {
    if (isSystemOffline && p.status === 'ALLOWED') {
        p.status = 'UNKNOWN';
    }
    const emptyRow = document.getElementById('log-empty-row');
    if (emptyRow) emptyRow.remove();

    totalPlates++;
    totalVehicles++;
    totalLogs++;

    if (metricScanned) metricScanned.textContent = totalPlates;
    if (metricTotal) metricTotal.textContent = totalVehicles;
    if (logCountBadge) logCountBadge.textContent = `${totalLogs} detection${totalLogs !== 1 ? 's' : ''} logged`;

    activityHistory[11]++;

    const now = new Date(p.timestamp);
    const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    let statusBadge = '';
    if (p.status === 'ALLOWED') {
        statusBadge = '<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">✔ ALLOWED</span>';
    } else if (p.status === 'NEEDS_CONFIRM') {
        statusBadge = '<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/10 text-amber-300 border border-amber-500/40">⚠ CONFIRM</span>';
    } else {
        statusBadge = '<span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/30">✘ UNKNOWN</span>';
    }

    const row = document.createElement('tr');
    row.className = 'hover:bg-slate-800/40 transition-colors text-slate-300';
    row.innerHTML = `
        <td class="px-4 py-2.5 font-mono text-[11px] whitespace-nowrap text-slate-400">${timeStr}</td>
        <td class="px-4 py-2.5 font-medium text-xs text-slate-200">${p.type}</td>
        <td class="px-4 py-2.5 font-bold text-white tracking-wider font-mono text-xs plate-text-cell">${p.text}</td>
        <td class="px-4 py-2.5 font-mono text-[11px] text-slate-400">${Math.round(p.confidence * 100)}%</td>
        <td class="px-4 py-2.5">${statusBadge}</td>
    `;
    logList.prepend(row);
    if (logList.children.length > 60) {
        logList.removeChild(logList.lastChild);
    }
}

// ─── Activity Graph ───────────────────────────────────────────
function updateActivityGraph() {
    activityHistory.shift();
    activityHistory.push(0);
    const graph = document.getElementById('activity-graph');
    if (!graph) return;
    const bars = graph.children;
    const maxVal = Math.max(3, ...activityHistory);
    for (let i = 0; i < 12 && bars[i]; i++) {
        bars[i].style.height = Math.max(6, (activityHistory[i] / maxVal) * 100) + '%';
    }
}

// ─── Real Hardware Video Rendering FPS Meter ─────────────────
let renderFrames = 0;
let lastFpsTime = performance.now();

function measureVideoFps() {
    renderFrames++;
    const now = performance.now();
    if (now - lastFpsTime >= 1000) {
        const actualFps = Math.round((renderFrames * 1000) / (now - lastFpsTime));
        renderFrames = 0;
        lastFpsTime = now;
        if (liveFps) {
            liveFps.textContent = `${actualFps} FPS`;
        }
    }
    requestAnimationFrame(measureVideoFps);
}
requestAnimationFrame(measureVideoFps);

setInterval(updateActivityGraph, 5000);
