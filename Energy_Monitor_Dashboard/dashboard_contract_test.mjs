import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const app = fs.readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const html = fs.readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const css = fs.readFileSync(new URL('./style.css', import.meta.url), 'utf8');

assert.match(app, /function getHourlyForDate\(hourlyRows, targetDate\)[\s\S]*?return hourlyRows\.filter\(r => r\.date === targetDate\);/);
assert.doesNotMatch(app, /Fallback: use latest date in the file/);
assert.match(app, /function saveCache\(data\)/);
assert.match(app, /saveCache\(normalized\);[\s\S]*?return normalized;/);
assert.match(app, /function mergeLiveActuals\(hourlyRows, stateData\)/);
assert.ok(app.includes('stateData?.profiles?.[current.date]'));
assert.match(app, /function getHourlyDisplayContext\(aiRows, hourlyRows\)/);
assert.match(app, /pendingDailyDate/);
assert.match(app, /Current forecast for/);
assert.match(app, /async function handleDataDownload\(event\)/);
assert.match(app, /URL\.createObjectURL\(await response\.blob\(\)\)/);
assert.match(html, /id="hourly-data-date"/);
assert.match(html, /id="hourly-chart-summary"/);
assert.match(html, /aria-describedby="hourly-chart-summary"/);
assert.match(css, /\.sr-only\s*\{/);

const domIds = [...app.matchAll(/\$\('([^']+)'\)/g)].map(match => match[1]);
for (const id of new Set(domIds)) {
  assert.match(html, new RegExp(`(?:id|for)=["']${id}["']`), `Missing DOM element: ${id}`);
}

// Execute the pure date-selection behavior without booting the browser UI.
const start = app.indexOf('function getHourlyForDate');
const end = app.indexOf('function currentISTParts', start);
const sandbox = {};
vm.runInNewContext(`${app.slice(start, end)}; this.getHourlyForDate = getHourlyForDate;`, sandbox);
const rows = [{ date: '2026-09-14', hour: '0' }, { date: '2026-09-15', hour: '0' }];
assert.deepEqual(sandbox.getHourlyForDate(rows, '2026-09-14'), [rows[0]]);
assert.deepEqual(sandbox.getHourlyForDate(rows, '2026-09-13'), []);

console.log('dashboard contract checks passed');
