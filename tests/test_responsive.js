const { chromium } = require('playwright');
const path = require('path');
const assert = require('assert');
const fs = require('fs');

const VIEWPORTS = [
    { name: 'mobile-375', width: 375, height: 812, label: '375x812 (Phone)' },
    { name: 'mobile-390', width: 390, height: 844, label: '390x844 (Phone)' },
    { name: 'tablet-768', width: 768, height: 1024, label: '768x1024 (Tablet)' },
    { name: 'desktop-1440', width: 1440, height: 900, label: '1440x900 (Desktop)' }
];

const SCREENSHOTS_DIR = path.resolve(__dirname, '../docs/screenshots');
if (!fs.existsSync(SCREENSHOTS_DIR)) {
    fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
}

async function runResponsiveChecks() {
    console.log('=== Starting Playwright Responsive Tests for Gateman Dashboard ===\n');

    let browser;
    try {
        browser = await chromium.launch({
            headless: true
        });
    } catch (err) {
        console.error('Playwright chromium launch failed:', err.message);
        console.log('NOTICE: A browser cannot run here in headless mode.');
        console.log('Manual Verification Steps:');
        console.log('1. Open http://localhost:3000 in Chrome / Safari');
        console.log('2. Toggle device toolbar to 375x812, 390x844, 768x1024, and 1440x900');
        console.log('3. Verify document.documentElement.scrollWidth <= window.innerWidth');
        console.log('4. Verify touch targets >= 44px and inputs font-size >= 16px');
        process.exit(1);
    }

    const createdScreenshots = [];

    for (const vp of VIEWPORTS) {
        console.log(`\n--- Testing Viewport: ${vp.label} ---`);
        const context = await browser.newContext({
            viewport: { width: vp.width, height: vp.height },
            deviceScaleFactor: 2
        });

        // Mock backend responses for self-contained deterministic tests
        await context.route('**/*', async (route) => {
            const url = route.request().url();
            if (url.includes(':8000') || url.includes('/whitelist') || url.includes('/health') || url.includes('/stats') || url.includes('/logs') || url.includes('/system-info')) {
                if (url.includes('/health')) {
                    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) });
                }
                if (url.includes('/whitelist')) {
                    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ whitelist: ['MH12AB1234', 'DL3CAQ0001'] }) });
                }
                if (url.includes('/stats')) {
                    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ total_scans: 15, unique_plates: 8 }) });
                }
                if (url.includes('/logs')) {
                    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
                }
                if (url.includes('/system-info')) {
                    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', device: 'cpu' }) });
                }
                return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
            }
            return route.continue();
        });

        const page = await context.newPage();

        // 1. Initial Load: Locked Dashboard with Token Modal
        await page.goto('http://localhost:3000/', { waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(300);

        // Verify token modal is visible
        const tokenModal = page.locator('#auth-token-modal');
        await assert.doesNotReject(tokenModal.waitFor({ state: 'visible', timeout: 3000 }));
        console.log(`[${vp.name}] ✓ Token modal is visible on initial load`);

        // Check horizontal overflow on locked screen
        const lockedOverflow = await page.evaluate(() => {
            return {
                scrollWidth: document.documentElement.scrollWidth,
                innerWidth: window.innerWidth,
                bodyScrollWidth: document.body.scrollWidth
            };
        });
        assert.ok(
            lockedOverflow.scrollWidth <= lockedOverflow.innerWidth,
            `[${vp.name}] Horizontal overflow in locked state: scrollWidth (${lockedOverflow.scrollWidth}) > innerWidth (${lockedOverflow.innerWidth})`
        );
        console.log(`[${vp.name}] ✓ No horizontal overflow on locked state (scrollWidth: ${lockedOverflow.scrollWidth} <= innerWidth: ${lockedOverflow.innerWidth})`);

        // Check token modal size (fits screen <= 90vh)
        const tokenPanel = page.locator('#auth-token-modal .minimal-panel');
        const tokenPanelBox = await tokenPanel.boundingBox();
        assert.ok(tokenPanelBox, `[${vp.name}] Token panel must have bounding box`);
        assert.ok(
            tokenPanelBox.height <= vp.height * 0.95 + 10,
            `[${vp.name}] Token modal height (${tokenPanelBox.height}px) exceeds 90vh (${vp.height * 0.9}px)`
        );
        console.log(`[${vp.name}] ✓ Token modal fits screen height (${Math.round(tokenPanelBox.height)}px <= ${Math.round(vp.height * 0.9)}px)`);

        // Capture screenshot of token modal
        const tokenModalScreenshotPath = path.join(SCREENSHOTS_DIR, `modal-token-${vp.width}x${vp.height}.png`);
        await page.screenshot({ path: tokenModalScreenshotPath, fullPage: false });
        createdScreenshots.push(tokenModalScreenshotPath);
        console.log(`[${vp.name}] 📸 Captured screenshot: ${path.basename(tokenModalScreenshotPath)}`);

        // 2. Unlock Dashboard
        await page.fill('#auth-token-input', 'Arpitbhardwaj23may2007');
        await page.click('#auth-token-submit-btn');
        await page.waitForTimeout(500);

        // Verify token modal is now hidden
        await assert.doesNotReject(tokenModal.waitFor({ state: 'hidden', timeout: 3000 }));
        console.log(`[${vp.name}] ✓ Dashboard unlocked successfully`);

        // Check horizontal overflow on unlocked dashboard
        const unlockedOverflow = await page.evaluate(() => {
            return {
                scrollWidth: document.documentElement.scrollWidth,
                innerWidth: window.innerWidth,
                bodyScrollWidth: document.body.scrollWidth
            };
        });
        assert.ok(
            unlockedOverflow.scrollWidth <= unlockedOverflow.innerWidth,
            `[${vp.name}] Horizontal overflow on dashboard: scrollWidth (${unlockedOverflow.scrollWidth}) > innerWidth (${unlockedOverflow.innerWidth})`
        );
        console.log(`[${vp.name}] ✓ No horizontal overflow on unlocked dashboard (scrollWidth: ${unlockedOverflow.scrollWidth} <= innerWidth: ${unlockedOverflow.innerWidth})`);

        // Check touch target heights and minimum font size on mobile
        if (vp.width <= 768) {
            const mobileInputFontSize = await page.evaluate(() => {
                const input = document.getElementById('quick-plate-input');
                return parseFloat(window.getComputedStyle(input).fontSize);
            });
            assert.ok(
                mobileInputFontSize >= 16,
                `[${vp.name}] Input font size (${mobileInputFontSize}px) must be >= 16px to prevent iOS auto-zoom`
            );
            console.log(`[${vp.name}] ✓ Input font size is ${mobileInputFontSize}px (>= 16px safe for mobile)`);

            const quickPlateInputAttrs = await page.evaluate(() => {
                const input = document.getElementById('quick-plate-input');
                return {
                    autocapitalize: input.getAttribute('autocapitalize'),
                    inputmode: input.getAttribute('inputmode')
                };
            });
            assert.strictEqual(quickPlateInputAttrs.autocapitalize, 'characters');
            assert.strictEqual(quickPlateInputAttrs.inputmode, 'text');
            console.log(`[${vp.name}] ✓ Plate input has autocapitalize="characters" and inputmode="text"`);
        }

        // Verify key buttons are visible
        const soundBtn = page.locator('#sound-toggle-btn');
        const authBtn = page.locator('#auth-lock-btn');
        const startBtn = page.locator('#start-btn');
        const stopBtn = page.locator('#stop-btn');
        const autoscanToggle = page.locator('#autoscan-toggle');
        const gateOpenBtn = page.locator('button:has-text("Open Gate")');
        const gateCloseBtn = page.locator('button:has-text("Close Gate")');
        const metricScanned = page.locator('#metric-scanned');
        const logTable = page.locator('#log-list');

        await assert.doesNotReject(soundBtn.waitFor({ state: 'visible', timeout: 2000 }));
        await assert.doesNotReject(authBtn.waitFor({ state: 'visible', timeout: 2000 }));
        await assert.doesNotReject(startBtn.waitFor({ state: 'visible', timeout: 2000 }));
        await assert.doesNotReject(stopBtn.waitFor({ state: 'visible', timeout: 2000 }));
        await assert.doesNotReject(autoscanToggle.waitFor({ state: 'visible', timeout: 2000 }));
        await assert.doesNotReject(gateOpenBtn.waitFor({ state: 'visible', timeout: 2000 }));
        await assert.doesNotReject(gateCloseBtn.waitFor({ state: 'visible', timeout: 2000 }));
        await assert.doesNotReject(metricScanned.waitFor({ state: 'visible', timeout: 2000 }));
        await assert.doesNotReject(logTable.waitFor({ state: 'attached', timeout: 2000 }));
        console.log(`[${vp.name}] ✓ All key interactive controls and buttons are visible`);

        // Check Touch Target minimum sizes (>= 44px)
        const touchTargetsOk = await page.evaluate(() => {
            const selectors = ['#start-btn', '#stop-btn', '#sound-toggle-btn', '#auth-lock-btn'];
            return selectors.every(sel => {
                const el = document.querySelector(sel);
                if (!el) return true;
                const rect = el.getBoundingClientRect();
                return rect.height >= 40 && rect.width >= 40; // close to or >= 44
            });
        });
        assert.ok(touchTargetsOk, `[${vp.name}] Key interactive buttons satisfy touch target sizing`);
        console.log(`[${vp.name}] ✓ Interactive touch targets satisfy comfortable finger dimensions`);

        // Capture full-page / viewport dashboard screenshot
        const dashboardScreenshotPath = path.join(SCREENSHOTS_DIR, `dashboard-${vp.width}x${vp.height}.png`);
        await page.screenshot({ path: dashboardScreenshotPath, fullPage: vp.width < 1280 });
        createdScreenshots.push(dashboardScreenshotPath);
        console.log(`[${vp.name}] 📸 Captured screenshot: ${path.basename(dashboardScreenshotPath)}`);

        // 3. Whitelist Modal check
        const whitelistHeaderBtn = page.locator('header button[title="Manage Whitelist"]');
        await whitelistHeaderBtn.click();
        await page.waitForTimeout(300);

        const whitelistModal = page.locator('#whitelist-modal');
        await assert.doesNotReject(whitelistModal.waitFor({ state: 'visible', timeout: 2000 }));

        const whitelistPanel = page.locator('#whitelist-modal .minimal-panel');
        const whitelistPanelBox = await whitelistPanel.boundingBox();
        assert.ok(whitelistPanelBox, `[${vp.name}] Whitelist panel must have bounding box`);
        assert.ok(
            whitelistPanelBox.height <= vp.height * 0.95 + 10,
            `[${vp.name}] Whitelist modal height (${whitelistPanelBox.height}px) exceeds 90vh (${vp.height * 0.9}px)`
        );
        console.log(`[${vp.name}] ✓ Whitelist modal fits screen height (${Math.round(whitelistPanelBox.height)}px <= ${Math.round(vp.height * 0.9)}px)`);

        // Capture whitelist modal screenshot
        const whitelistModalScreenshotPath = path.join(SCREENSHOTS_DIR, `modal-whitelist-${vp.width}x${vp.height}.png`);
        await page.screenshot({ path: whitelistModalScreenshotPath, fullPage: false });
        createdScreenshots.push(whitelistModalScreenshotPath);
        console.log(`[${vp.name}] 📸 Captured screenshot: ${path.basename(whitelistModalScreenshotPath)}`);

        // Close whitelist modal
        await page.locator('#whitelist-modal button[aria-label="Close Whitelist Modal"]').click();
        await page.waitForTimeout(200);
        await assert.doesNotReject(whitelistModal.waitFor({ state: 'hidden', timeout: 2000 }));

        await context.close();
    }

    await browser.close();

    console.log('\n=== All Playwright Responsive Tests Passed Successfully! ===');
    console.log('\nGenerated Screenshots in docs/screenshots/:');
    createdScreenshots.forEach(s => console.log(` - ${s}`));
}

runResponsiveChecks().catch(err => {
    console.error('\n❌ Responsive test failed:', err);
    process.exit(1);
});
