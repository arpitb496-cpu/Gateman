const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

async function runTests() {
    console.log('--- Running Browser-Level JSDOM Test for Gateman Frontend ---');

    // 1. Verify app.js and frontend/app.js are identical
    const rootAppJsPath = path.resolve(__dirname, '../app.js');
    const frontendAppJsPath = path.resolve(__dirname, '../frontend/app.js');
    const rootAppJs = fs.readFileSync(rootAppJsPath, 'utf8');
    const frontendAppJs = fs.readFileSync(frontendAppJsPath, 'utf8');

    assert.strictEqual(
        rootAppJs,
        frontendAppJs,
        'Root app.js and frontend/app.js must be identical!'
    );
    console.log('✓ Verified: app.js and frontend/app.js are strictly identical');

    // 2. Load frontend/index.html
    const htmlPath = path.resolve(__dirname, '../frontend/index.html');
    const rawHtml = fs.readFileSync(htmlPath, 'utf8');

    // Strip external CDN scripts and inline script tag
    const cleanHtml = rawHtml
        .replace(/<script src="https:[^"]*"><\/script>/g, '')
        .replace('<script src="app.js"></script>', '');

    const uncaughtErrors = [];
    const consoleErrors = [];

    const dom = new JSDOM(cleanHtml, {
        runScripts: 'outside-only',
        url: 'http://localhost:3000/'
    });

    const { window } = dom;

    // Track uncaught errors on the window
    window.addEventListener('error', (event) => {
        uncaughtErrors.push(event.error || event.message || event);
    });

    // Track console.error (ignore intentional console.error from API Error logging in catch blocks)
    const originalConsoleError = window.console.error;
    window.console.error = (...args) => {
        const msg = args.join(' ');
        if (!msg.includes('API Error:')) {
            consoleErrors.push(msg);
        }
        if (originalConsoleError) originalConsoleError.apply(window.console, args);
    };

    // Mock necessary browser APIs for headless environment
    window.lucide = {
        createIcons: () => {}
    };

    window.requestAnimationFrame = (callback) => setTimeout(callback, 16);
    window.cancelAnimationFrame = (id) => clearTimeout(id);

    window.HTMLCanvasElement.prototype.getContext = function (type) {
        if (type === '2d') {
            return {
                clearRect: () => {},
                fillRect: () => {},
                strokeRect: () => {},
                beginPath: () => {},
                arc: () => {},
                fill: () => {},
                stroke: () => {},
                measureText: () => ({ width: 50 }),
                fillText: () => {},
                save: () => {},
                restore: () => {},
                drawImage: () => {}
            };
        }
        return null;
    };

    window.HTMLCanvasElement.prototype.toBlob = function (callback) {
        callback(new window.Blob());
    };

    window.navigator.mediaDevices = {
        getUserMedia: async () => ({
            getTracks: () => [{ stop: () => {} }]
        }),
        enumerateDevices: async () => [
            { kind: 'videoinput', deviceId: 'default', label: 'FaceTime HD Camera' }
        ]
    };

    window.alert = () => {};

    // Ensure sessionStorage starts with no token
    window.sessionStorage.clear();
    assert.strictEqual(window.sessionStorage.getItem('gateman_admin_token'), null);

    // Initial mock fetch: returns 200 for initial ping, 401 for whitelist without token
    window.fetch = async (url, options = {}) => {
        const headers = options.headers || {};
        const auth = headers.Authorization || (headers.get && headers.get('Authorization')) || '';
        if (url.includes('/health')) {
            return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
        }
        if (url.includes('/whitelist')) {
            if (auth.includes('valid-admin-token')) {
                return { ok: true, status: 200, json: async () => ({ whitelist: ['MH12AB1234'] }) };
            }
            return {
                ok: false,
                status: 401,
                json: async () => ({ detail: 'Invalid authentication token' })
            };
        }
        return { ok: true, status: 200, json: async () => ({}) };
    };

    // 3. Execute frontend/app.js in the window context
    try {
        window.eval(frontendAppJs);
    } catch (evalErr) {
        uncaughtErrors.push(evalErr);
    }

    // 4. Baseline Assertions (Page Load)
    assert.strictEqual(
        uncaughtErrors.length,
        0,
        `Expected 0 uncaught runtime errors, but found ${uncaughtErrors.length}: ${uncaughtErrors.map(e => e.stack || e).join('\n')}`
    );
    assert.strictEqual(
        consoleErrors.length,
        0,
        `Expected 0 console.error calls, but found: ${consoleErrors.join(', ')}`
    );
    console.log('✓ Verified: No uncaught console or runtime errors during page load');

    const modal = window.document.getElementById('auth-token-modal');
    assert.ok(modal, 'auth-token-modal element must exist');
    assert.strictEqual(modal.classList.contains('hidden'), false, 'Token modal must be visible on load');

    const statusText = window.document.getElementById('status-text');
    assert.strictEqual(statusText.textContent, 'LOCKED - enter token');
    console.log('✓ Verified: Token modal is visible and dashboard is locked on initial load');

    const startBtn = window.document.getElementById('start-btn');
    assert.doesNotThrow(() => {
        startBtn.click();
    }, 'Clicking Start button should not throw');
    console.log('✓ Verified: Clicking Start button executed safely without throwing');

    // 5. Bug 1 Test: Submit wrong token, mock 401, assert #auth-token-error is visible with text
    console.log('\n--- Testing Bug 1: Wrong token shows error message ---');
    const tokenInput = window.document.getElementById('auth-token-input');
    const errBox = window.document.getElementById('auth-token-error');
    const errTxt = window.document.getElementById('auth-token-error-text');

    tokenInput.value = 'wrong-token-12345';
    await window.eval('submitAuthToken()');

    assert.strictEqual(
        errBox.classList.contains('hidden'),
        false,
        'Expected #auth-token-error to be visible after submitting invalid token'
    );
    assert.strictEqual(
        errTxt.textContent,
        'Invalid authentication token',
        'Expected error text to be "Invalid authentication token"'
    );
    assert.strictEqual(
        modal.classList.contains('hidden'),
        false,
        'Token modal should remain visible on 401'
    );
    console.log('✓ Verified: #auth-token-error is visible with correct text on 401 failure');

    // 6. Bug 2 Test: Backend unreachable (fetch rejection) sets system offline & stops auto-scan
    console.log('\n--- Testing Bug 2: Backend unreachable / fetch rejection ---');
    const banner = window.document.getElementById('system-offline-banner');

    // First unlock dashboard with valid token
    tokenInput.value = 'valid-admin-token-123456';
    await window.eval('submitAuthToken()');
    assert.strictEqual(modal.classList.contains('hidden'), true, 'Token modal should hide on valid token');

    // Start video feed and enable auto-scan
    await window.start();
    const autoscanToggle = window.document.getElementById('autoscan-toggle');
    window.setAutoScan(true);
    assert.strictEqual(
        autoscanToggle.getAttribute('aria-pressed'),
        'true',
        'autoscan toggle should be pressed (true) before network failure'
    );

    // 6a: Test ping() with network error
    window.fetch = async () => {
        throw new TypeError('Failed to fetch: Connection refused');
    };

    await window.ping();

    assert.strictEqual(
        banner.classList.contains('hidden'),
        false,
        'Expected #system-offline-banner to be visible when ping() encounters network failure'
    );
    assert.strictEqual(
        autoscanToggle.getAttribute('aria-pressed'),
        'false',
        'Expected auto-scan to be stopped (aria-pressed = false) after network failure'
    );
    console.log('✓ Verified: ping() network failure displays offline banner and stops auto-scan');

    // 6b: Test subsequent successful ping clears system offline
    window.fetch = async (url) => {
        if (url.includes('/health')) {
            return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
        }
        return { ok: true, status: 200, json: async () => ({}) };
    };

    await window.ping();
    assert.strictEqual(
        banner.classList.contains('hidden'),
        true,
        'Expected #system-offline-banner to be hidden after successful ping'
    );
    console.log('✓ Verified: subsequent successful ping clears system offline banner');

    // 6c: Test detect() with network error
    window.setAutoScan(true);
    assert.strictEqual(
        autoscanToggle.getAttribute('aria-pressed'),
        'true',
        'autoscan toggle should be re-enabled before detect network test'
    );

    // Force network rejection on detect
    window.fetch = async () => {
        throw new TypeError('Network error during /detect');
    };

    // Ensure video dimensions are set so detect() attempts fetch
    const videoElem = window.document.getElementById('video-feed');
    Object.defineProperty(videoElem, 'videoWidth', { value: 640, configurable: true });
    Object.defineProperty(videoElem, 'videoHeight', { value: 480, configurable: true });

    await window.detect();
    // Allow async toBlob callback and microtasks to complete
    await new Promise(r => setTimeout(r, 50));

    assert.strictEqual(
        banner.classList.contains('hidden'),
        false,
        'Expected #system-offline-banner to be visible when detect() encounters network failure'
    );
    assert.strictEqual(
        autoscanToggle.getAttribute('aria-pressed'),
        'false',
        'Expected auto-scan to be stopped after detect() network failure'
    );
    console.log('✓ Verified: detect() network failure displays offline banner and stops auto-scan');

    // Restore system online for manual scan tests
    window.fetch = async (url) => {
        if (url.includes('/health')) {
            return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
        }
        return { ok: true, status: 200, json: async () => ({}) };
    };
    await window.ping();
    assert.strictEqual(banner.classList.contains('hidden'), true);

    // 7. Manual Scan Tests
    const manualScanBtn = window.document.getElementById('manual-scan-btn');
    const statusTextElem = window.document.getElementById('status-text');

    // 7a: Every frame returns '...' -> button restored and "No plate read" shown
    console.log('\n--- Testing Manual Scan 4(a): Every frame returns "..." ---');
    window.setAutoScan(false);

    window.fetch = async (url) => {
        if (url.includes('/detect')) {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    cars: [{ box: [10, 10, 200, 200], confidence: 0.9 }],
                    plates: [{ box: [50, 50, 150, 100], text: '...', confidence: 0.1, status: 'UNKNOWN' }]
                })
            };
        }
        return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    };

    await window.manualScan();

    assert.strictEqual(manualScanBtn.disabled, false, 'Manual scan button must be restored/enabled after scan completes');
    assert.strictEqual(manualScanBtn.innerHTML.includes('Scan Plate Now'), true, 'Manual scan button should show "Scan Plate Now"');
    assert.strictEqual(statusTextElem.textContent.includes('No plate read'), true, 'Status text should show "No plate read - try again"');
    console.log('✓ Verified 4(a): Every frame returns "..." -> button restored and "No plate read" shown');

    // 7b: Auto-Scan toggled ON during a manual scan -> button restored
    console.log('\n--- Testing Manual Scan 4(b): Auto-Scan toggled ON during manual scan ---');
    window.setAutoScan(false);

    window.fetch = async (url) => {
        if (url.includes('/detect')) {
            await new Promise(r => setTimeout(r, 60));
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    cars: [],
                    plates: [{ box: [50, 50, 150, 100], text: '...', status: 'UNKNOWN' }]
                })
            };
        }
        return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    };

    const manualScanPromise = window.manualScan();

    // Give it a moment to enter the burst loop and set button to spinning
    await new Promise(r => setTimeout(r, 40));
    assert.strictEqual(manualScanBtn.disabled, true, 'Button should be disabled during manual scan');
    assert.strictEqual(manualScanBtn.innerHTML.includes('Scanning...'), true, 'Button should show "Scanning..."');

    // Toggle Auto-Scan ON while manual scan is running
    window.setAutoScan(true);
    assert.strictEqual(autoscanToggle.getAttribute('aria-pressed'), 'true', 'Auto-Scan should now be ON');

    // Wait for manual scan to complete
    await manualScanPromise;

    assert.strictEqual(manualScanBtn.disabled, false, 'Button must be restored even if Auto-Scan is toggled ON');
    assert.strictEqual(manualScanBtn.innerHTML.includes('Scan Plate Now'), true, 'Button text must return to "Scan Plate Now"');
    console.log('✓ Verified 4(b): Auto-Scan toggled ON during manual scan -> button restored');

    // 7c: lucide undefined -> manual scan still completes
    console.log('\n--- Testing Manual Scan 4(c): lucide undefined -> manual scan still completes ---');
    const savedLucide = window.lucide;
    delete window.lucide;
    window.lucide = undefined;

    window.fetch = async (url) => {
        if (url.includes('/detect')) {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    cars: [{ box: [10, 10, 200, 200] }],
                    plates: [{ box: [50, 50, 150, 100], text: 'DL01AB1234', status: 'ALLOWED' }]
                })
            };
        }
        return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    };

    await assert.doesNotReject(async () => {
        await window.manualScan();
    }, 'manualScan() must not throw when lucide is undefined');

    assert.strictEqual(manualScanBtn.disabled, false, 'Button must be enabled after scan completes with lucide undefined');
    assert.strictEqual(manualScanBtn.innerHTML.includes('Scan Plate Now'), true, 'Button text restored with lucide undefined');
    assert.strictEqual(statusTextElem.textContent, 'Scanned: DL01AB1234', 'Status text updated with plate text');

    window.lucide = savedLucide;
    console.log('✓ Verified 4(c): lucide undefined -> manual scan still completes without throwing');

    console.log('\nAll browser-level checks passed successfully!');
    dom.window.close();
    process.exit(0);
}

runTests().catch((err) => {
    console.error('Test failed with error:', err);
    process.exit(1);
});

