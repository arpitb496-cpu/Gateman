/* ============================================================
   AI Gateman — app.js
   Camera capture, API calls, canvas overlay, parallax & glow
   ============================================================ */

const API = "http://127.0.0.1:8000";

// DOM refs
const video      = document.getElementById('video-feed');
const canvas     = document.getElementById('overlay-canvas');
const ctx        = canvas.getContext('2d');
const camSelect  = document.getElementById('camera-select');
const startBtn   = document.getElementById('start-btn');
const stopBtn    = document.getElementById('stop-btn');
const feedEmpty  = document.getElementById('feed-empty');
const serverDot  = document.getElementById('server-dot');
const serverLbl  = document.getElementById('server-label');
const vehCount   = document.getElementById('vehicle-count');
const logList    = document.getElementById('log-list');
const cursorGlow = document.getElementById('cursor-glow');
const pCanvas    = document.getElementById('particle-canvas');
const pCtx       = pCanvas.getContext('2d');
const metricTotal = document.getElementById('metric-total');
const metricScanned = document.getElementById('metric-scanned');
const metricRecognized = document.getElementById('metric-recognized');

let stream = null;
let loop   = null;
let busy   = false;
let totalVehicles = 0;
let totalPlates = 0;
let activityHistory = new Array(12).fill(0);

// ─── Mouse Tracking ───────────────────────────────────────
let mouse = { x: null, y: null, radius: 150 };

document.addEventListener('mousemove', e => {
    cursorGlow.style.left = e.clientX + 'px';
    cursorGlow.style.top  = e.clientY + 'px';
    mouse.x = e.clientX;
    mouse.y = e.clientY;
});

document.addEventListener('mouseleave', () => {
    mouse.x = null;
    mouse.y = null;
});

// ─── Particle Constellation System ────────────────────────
let particles = [];
const colors = ['#4285F4', '#EA4335', '#FBBC05', '#34A853', '#8a2be2', '#ff69b4', '#00bcd4'];

function initParticles() {
    particles = [];
    const width = pCanvas.width = window.innerWidth;
    const height = pCanvas.height = window.innerHeight;
    
    // Concentrate particles mostly on the right side in a curved/sprinkled wave to match the ref image
    const count = Math.min(220, Math.floor((width * height) / 7000));
    for (let i = 0; i < count; i++) {
        const isClustered = Math.random() > 0.25;
        let x, y;
        if (isClustered) {
            // Clustered on the right / top-right quadrant
            x = width * 0.45 + Math.random() * (width * 0.55);
            y = Math.random() * (height * 0.95);
        } else {
            x = Math.random() * width;
            y = Math.random() * height;
        }
        
        particles.push({
            x: x,
            y: y,
            baseX: x,
            baseY: y,
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
    
    for (let i = 0; i < particles.length; i++) {
        let p = particles[i];
        
        // Idle drift
        p.x += p.vx;
        p.y += p.vy;
        p.baseX += p.vx;
        p.baseY += p.vy;
        
        // Wrap around margins
        if (p.x < -20) p.x = p.baseX = pCanvas.width + 20;
        if (p.x > pCanvas.width + 20) p.x = p.baseX = -20;
        if (p.y < -20) p.y = p.baseY = pCanvas.height + 20;
        if (p.y > pCanvas.height + 20) p.y = p.baseY = -20;
        
        // Interactive mouse hover push
        if (mouse.x !== null && mouse.y !== null) {
            let dx = mouse.x - p.x;
            let dy = mouse.y - p.y;
            let distance = Math.sqrt(dx * dx + dy * dy);
            if (distance < mouse.radius) {
                let forceDirectionX = dx / distance;
                let forceDirectionY = dy / distance;
                let maxDistance = mouse.radius;
                let force = (maxDistance - distance) / maxDistance;
                let directionX = forceDirectionX * force * p.density * 0.45;
                let directionY = forceDirectionY * force * p.density * 0.45;
                p.x -= directionX;
                p.y -= directionY;
            } else {
                if (p.x !== p.baseX) {
                    let dxBase = p.x - p.baseX;
                    p.x -= dxBase * 0.08;
                }
                if (p.y !== p.baseY) {
                    let dyBase = p.y - p.baseY;
                    p.y -= dyBase * 0.08;
                }
            }
        }
        
        // Draw a 4-pointed star (sparkle)
        pCtx.beginPath();
        const points = 4;
        const outer = p.size * 1.5;
        const inner = p.size * 0.5;
        for (let j = 0; j < points * 2; j++) {
            let r = (j % 2 === 0) ? outer : inner;
            let angle = (j * Math.PI) / points;
            pCtx.lineTo(p.x + r * Math.sin(angle), p.y + r * Math.cos(angle));
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

// ─── Server Health ────────────────────────────────────────
async function ping() {
    try {
        const r = await fetch(`${API}/health`);
        if (r.ok) {
            document.getElementById('server-ping').className = "absolute inline-flex w-full h-full rounded-full opacity-75 animate-ping bg-emerald-500";
            serverDot.className = "relative inline-flex w-2.5 h-2.5 rounded-full bg-emerald-500";
            serverLbl.className = "text-sm font-semibold text-emerald-400 tracking-wide uppercase";
            serverLbl.textContent = 'LIVE SYSTEM ACTIVE';
        } else throw 0;
    } catch {
        document.getElementById('server-ping').className = "absolute inline-flex w-full h-full rounded-full opacity-75 animate-ping bg-rose-500";
        serverDot.className = "relative inline-flex w-2.5 h-2.5 rounded-full bg-rose-500";
        serverLbl.className = "text-sm font-semibold text-rose-400 tracking-wide uppercase";
        serverLbl.textContent = 'SYSTEM OFFLINE';
    }
}
ping();
setInterval(ping, 5000);

// ─── Export to Excel ─────────────────────────────────────
function exportExcel() {
    window.location.href = `${API}/export-excel`;
}

// ─── Camera Enumeration ──────────────────────────────────
(async function listCams() {
    try {
        await navigator.mediaDevices.getUserMedia({ video: true });
        const devs = await navigator.mediaDevices.enumerateDevices();
        const cams = devs.filter(d => d.kind === 'videoinput');
        camSelect.innerHTML = '';
        if (!cams.length) {
            camSelect.innerHTML = '<option value="">No cameras</option>';
            return;
        }
        cams.forEach(d => {
            const o = document.createElement('option');
            o.value = d.deviceId;
            o.text  = d.label || `Camera ${camSelect.length + 1}`;
            camSelect.appendChild(o);
        });
    } catch {
        camSelect.innerHTML = '<option value="">Permission denied</option>';
    }
})();

// ─── Start / Stop ────────────────────────────────────────
async function start() {
    const id = camSelect.value;
    if (!id) return;
    try {
        stream = await navigator.mediaDevices.getUserMedia({
            video: { deviceId: { exact: id } }
        });
        video.srcObject = stream;
        feedEmpty.style.display = 'none';
        startBtn.disabled = true;
        stopBtn.disabled  = false;

        video.onloadedmetadata = () => {
            canvas.width  = video.videoWidth;
            canvas.height = video.videoHeight;
            loop = setInterval(detect, 1000);
        };
    } catch (e) {
        alert('Camera error: ' + e.message);
    }
}

function stop() {
    if (stream) stream.getTracks().forEach(t => t.stop());
    video.srcObject = null;
    if (loop) clearInterval(loop);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    feedEmpty.style.display = 'flex';
    startBtn.disabled = false;
    stopBtn.disabled  = true;
    vehCount.textContent = '0 Vehicles';
}

startBtn.addEventListener('click', start);
stopBtn.addEventListener('click', stop);

// ─── Frame Capture → API ─────────────────────────────────
async function detect() {
    if (busy) return;
    busy = true;

    const tc  = document.createElement('canvas');
    tc.width  = video.videoWidth;
    tc.height = video.videoHeight;
    tc.getContext('2d').drawImage(video, 0, 0);

    tc.toBlob(async blob => {
        if (!blob) { busy = false; return; }
        const fd = new FormData();
        fd.append('file', blob, 'f.jpg');
        try {
            const r = await fetch(`${API}/detect`, { method: 'POST', body: fd });
            if (r.ok) {
                const d = await r.json();
                draw(d);
                logPlates(d);
            }
        } catch { /* network error — silent */ }
        busy = false;
    }, 'image/jpeg', 0.80);
}

// ─── Canvas Rendering ────────────────────────────────────
// ─── Canvas Rendering & Status Display ────────────────────
const statusOverlay = document.getElementById('status-overlay');

function draw(d) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Show ALLOWED banner only — no UNKNOWN alert
    if (d.status === "ALLOWED") {
        statusOverlay.textContent = "✔ ALLOWED";
        statusOverlay.className = "status-overlay allowed";
    } else {
        statusOverlay.className = "status-overlay";
    }

    d.cars.forEach(c => {
        const [x1,y1,x2,y2] = c.box;
        const w = x2-x1, h = y2-y1;

        ctx.fillStyle = 'rgba(255, 255, 255, 0.02)';
        ctx.fillRect(x1, y1, w, h);

        ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
        ctx.lineWidth = 1.8;
        ctx.strokeRect(x1, y1, w, h);

        ctx.font = '700 11px "Outfit"';
        ctx.fillStyle = 'rgba(255, 255, 255, 0.8)';
        ctx.fillText('VEHICLE', x1+6, y1+15);
    });

    d.plates.forEach(p => {
        const [x1,y1,x2,y2] = p.box;
        const w = x2-x1, h = y2-y1;
        const plateText = p.text === '...' ? 'SCANNING…' : p.text;

        // Live timestamp next to plate
        const now = new Date();
        const dateStr = now.toLocaleDateString('en-GB');   // DD/MM/YYYY
        const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const timestamp = `${dateStr}  ${timeStr}`;

        const plateColor = d.status === "ALLOWED" ? '#4ade80' : '#3b82f6';

        ctx.fillStyle = 'rgba(59, 130, 246, 0.05)';
        ctx.fillRect(x1, y1, w, h);

        ctx.strokeStyle = plateColor;
        ctx.lineWidth = 2.5;
        ctx.strokeRect(x1, y1, w, h);

        // Plate number — bold and prominent
        ctx.font = '800 15px "Outfit", monospace';
        ctx.fillStyle = plateColor;
        ctx.fillText(plateText, x1, y1 - 8);

        // Date & time — smaller, muted, right next to the plate text
        const plateWidth = ctx.measureText(plateText).width;
        ctx.font = '500 11px "Outfit", sans-serif';
        ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
        ctx.fillText(timestamp, x1 + plateWidth + 10, y1 - 8);
    });

    vehCount.textContent =
        `${d.cars.length} Vehicle${d.cars.length !== 1 ? 's' : ''}`;
}

// ─── Log Panel ───────────────────────────────────────────
function logPlates(d) {
    d.plates.forEach(p => {
        if (p.text === '...' || p.text.length < 4) return;

        // remove empty state row
        const empty = document.getElementById('log-empty-row');
        if (empty) empty.remove();

        // deduplicate by recent text (last 10 plates)
        const existing = Array.from(logList.querySelectorAll('.plate-text-cell')).slice(0, 10);
        for (const el of existing) {
            if (el.textContent.trim() === p.text.trim()) return;
        }

        totalPlates++;
        totalVehicles++;
        metricScanned.textContent = totalPlates;
        metricTotal.textContent = totalVehicles;
        metricRecognized.textContent = totalPlates;
        activityHistory[11]++;

        const now = new Date();
        const dateStr = now.toLocaleDateString('en-GB'); // DD/MM/YYYY
        const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        
        const isAllowed = d.status === "ALLOWED";
        const statusClass = isAllowed ? 'text-emerald-400' : 'text-rose-400';
        const statusText = isAllowed ? 'Access Granted' : 'Access Denied';
        
        const confPercent = Math.round((p.confidence || 0) * 100);
        const vType = p.vehicle_type || 'Unknown';

        const row = document.createElement('tr');
        row.className = "hover:bg-slate-800/50 transition-colors text-slate-300";
        row.innerHTML = `
            <td class="px-4 py-3 border-none font-mono text-[11px] whitespace-nowrap">
                ${timeStr}
            </td>
            <td class="px-4 py-3 border-none font-medium text-xs">
                ${vType}
            </td>
            <td class="px-4 py-3 border-none font-bold text-white tracking-wider font-mono text-xs plate-text-cell">
                ${p.text}
            </td>
        `;
        logList.prepend(row);

        if (logList.children.length > 50) {
            logList.removeChild(logList.lastChild);
        }
    });
}

// ─── Activity Graph Animation ────────────────────────────
function updateActivityGraph() {
    // shift history left (oldest falls off)
    activityHistory.shift();
    // add new bucket for the next segment
    activityHistory.push(0);

    const graph = document.getElementById('activity-graph');
    if (!graph) return;
    const bars = graph.children;
    
    // find max to scale the bars (minimum max of 2 to avoid huge bars for 1 car)
    const maxVal = Math.max(2, ...activityHistory);
    
    for (let i = 0; i < 12; i++) {
        if (bars[i]) {
            const heightPct = (activityHistory[i] / maxVal) * 100;
            // minimum height of 4% just so the bar is slightly visible even if 0
            bars[i].style.height = Math.max(4, heightPct) + '%';
        }
    }
}

// Update the graph every 5 seconds (60 seconds total for 12 bars)
setInterval(updateActivityGraph, 5000);
