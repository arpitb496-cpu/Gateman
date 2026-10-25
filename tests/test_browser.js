const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

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

// Strip external script tags (Tailwind CDN, Lucide CDN) to avoid unnecessary network fetches during test
// and strip the inline <script src="app.js"></script> tag because we evaluate it in JSDOM directly
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

// Track console.error
const originalConsoleError = window.console.error;
window.console.error = (...args) => {
    consoleErrors.push(args.join(' '));
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

window.alert = (msg) => {
    // console.log('Mocked window.alert called:', msg);
};

// Ensure sessionStorage has no token
window.sessionStorage.clear();
assert.strictEqual(window.sessionStorage.getItem('gateman_admin_token'), null);

// 3. Execute frontend/app.js in the window context
try {
    window.eval(frontendAppJs);
} catch (evalErr) {
    uncaughtErrors.push(evalErr);
}

// 4. Assertions

// Assertion A: No uncaught console or runtime errors during page load / script execution
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

// Assertion B: The token modal is visible (i.e., does not contain the 'hidden' class)
const modal = window.document.getElementById('auth-token-modal');
assert.ok(modal, 'auth-token-modal element must exist in the DOM');
assert.strictEqual(
    modal.classList.contains('hidden'),
    false,
    'Expected #auth-token-modal to be visible (not containing "hidden" class)'
);

const statusText = window.document.getElementById('status-text');
assert.strictEqual(
    statusText.textContent,
    'LOCKED - enter token',
    'Expected status-text to display "LOCKED - enter token"'
);
console.log('✓ Verified: Token modal is visible and dashboard is locked with "LOCKED - enter token"');

// Assertion C: Clicking Start does not throw
const startBtn = window.document.getElementById('start-btn');
assert.ok(startBtn, 'start-btn element must exist in the DOM');

assert.doesNotThrow(() => {
    startBtn.click();
}, 'Clicking Start button should not throw an exception');
console.log('✓ Verified: Clicking Start button executed safely without throwing');

console.log('\nAll browser-level checks passed successfully!');

// Clean up JSDOM window and exit cleanly
dom.window.close();
process.exit(0);
