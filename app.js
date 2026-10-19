/**
 * Device 153 Energy Monitoring Dashboard
 * Full replacement for app.js.
 * Prioritizes the local PostgreSQL API on localhost, then falls back to the
 * existing GitHub data sources. Database credentials never enter browser code.
 */
'use strict';

const CONFIG = {
  urls: {
    localApi: '/api/dashboard-data',
    liveDataApi: 'https://api.github.com/repos/manumrityunjay07-ai/Energy_Monitor_HTTP/contents/results/dashboard_data.json?ref=live-data',
    dashboardData: 'https://raw.githubusercontent.com/manumrityunjay07-ai/Energy_Monitor_HTTP/live-data/results/dashboard_data.json',
    aiResults: 'https://raw.githubusercontent.com/manumrityunjay07-ai/Energy_Monitor_HTTP/live-data/results/ai_results.csv',
    dailyTotals: 'https://raw.githubusercontent.com/manumrityunjay07-ai/Energy_Monitor_HTTP/live-data/results/daily_totals.csv',
    hourlyPredictions: 'https://raw.githubusercontent.com/manumrityunjay07-ai/Energy_Monitor_HTTP/live-data/results/hourly_predictions.csv',
    state: 'https://raw.githubusercontent.com/manumrityunjay07-ai/Energy_Monitor_HTTP/live-data/data/state.json',
  },
  refreshInterval: 5 * 60 * 1000,
  timezone: 'Asia/Kolkata',
  maxRetries: 3,
  requestTimeoutMs: 10000,
  cacheMaxAgeMs: 30 * 60 * 1000,
};

const $ = id => document.getElementById(id);
const UI = {
  loadingOverlay: $('loading-overlay'), errorOverlay: $('error-overlay'), errorMessage: $('error-message'),
  mainContent: $('main-content'), lastUpdated: $('last-updated'), updateDot: $('update-dot'),
  btnRefresh: $('btn-refresh'), btnRetry: $('btn-retry'), refreshIcon: $('refresh-icon'),
  kpiPredicted: $('kpi-predicted'), kpiPredictedLabel: $('kpi-predicted-label'), predictionRange: $('prediction-range'),
  kpiPredictedDate: $('kpi-predicted-date'), kpiActual: $('kpi-actual'), kpiActualLabel: $('kpi-actual-label'),
  kpiActualUnit: $('kpi-actual-unit'), kpiActualHint: $('kpi-actual-hint'), kpiActualDate: $('kpi-actual-date'),
  kpiError: $('kpi-error'), kpiErrorLabel: $('kpi-error-label'), kpiErrorUnit: $('kpi-error-unit'),
  kpiErrorHint: $('kpi-error-hint'), kpiErrorDate: $('kpi-error-date'), hourlyTableBody: $('hourly-table-body'),
  noActualNote: $('no-actual-note'), anomalyInvestigationList: $('anomaly-investigation-list'),
  collectorHealth: $('collector-health'), collectorLastRun: $('collector-last-run'), dataFreshness: $('data-freshness'),
  currentDataStatus: $('current-data-status'), hourlyDataDate: $('hourly-data-date'), hourlyChartSummary: $('hourly-chart-summary'),
  historyCaption: $('history-caption'), dailyChartSummary: $('daily-chart-summary'),
  historyTableBody: $('history-table-body'), historyDateSelect: $('history-date-select'),
  aiLearningStatus: $('ai-learning-status'), aiLearningSamples: $('ai-learning-samples'), aiBaselineMae: $('ai-baseline-mae'),
  aiAdaptedMae: $('ai-adapted-mae'), aiDifferenceKwh: $('ai-difference-kwh'), aiImprovementPercent: $('ai-improvement-percent'),
  aiRateComparison: $('ai-rate-comparison'), aiImprovementMessage: $('ai-improvement-message'),
};

let hourlyChart = null;
let dailyChart = null;
let historyDays = 7;
let selectedHistoryDate = '';
let activePayload = null;
let refreshTimer = null;

function escapeAttr(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function nowIST() {
  return new Date().toLocaleString('en-IN', { timeZone: CONFIG.timezone, day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });
}
function formatISOtoIST(value) {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? escapeAttr(value) : d.toLocaleString('en-IN', { timeZone: CONFIG.timezone, day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true });
}
function hourLabel(hour) {
  const n = Number.parseInt(hour, 10);
  if (!Number.isFinite(n)) return String(hour);
  return `${String(n % 12 || 12).padStart(2, '0')}:00 ${n >= 12 ? 'PM' : 'AM'}`;
}
function formatDisplayDate(dateStr) {
  if (!dateStr || dateStr === 'latest completed day') return 'Latest day';
  const m = String(dateStr).trim().match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return String(dateStr);
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isNaN(d.getTime()) ? String(dateStr) : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}
function getNextDay(dateStr) {
  const d = new Date(`${dateStr}T12:00:00`);
  if (!dateStr || Number.isNaN(d.getTime())) return dateStr;
  d.setDate(d.getDate() + 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function parseCSV(text) {
  const input = String(text || '').replace(/^\uFEFF/, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const lines = input.split('\n').filter((line, i, arr) => line.length || i < arr.length - 1);
  if (lines.length < 2) return [];
  const parseLine = line => {
    const out = []; let value = ''; let quoted = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i], next = line[i + 1];
      if (quoted && c === '"' && next === '"') { value += '"'; i++; }
      else if (c === '"') quoted = !quoted;
      else if (c === ',' && !quoted) { out.push(value); value = ''; }
      else value += c;
    }
    out.push(value); return out;
  };
  const headers = parseLine(lines[0]).map(x => x.trim());
  return lines.slice(1).filter(line => line.trim()).map(line => {
    const values = parseLine(line); const row = {};
    headers.forEach((h, i) => row[h] = (values[i] ?? '').trim());
    return row;
  });
}
function convertToCSV(rows) {
  if (!rows?.length) return '';
  const headers = Object.keys(rows[0]);
  const q = value => {
    const s = String(value ?? '');
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [headers.map(q).join(','), ...rows.map(row => headers.map(h => q(row[h])).join(','))].join('\r\n');
}
function toFloat(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function fmt(value, decimals = 2) {
  const n = toFloat(value);
  return n === null ? null : n.toFixed(decimals);
}
function rowPredictionError(row) {
  const stored = toFloat(row?.prediction_error_kwh);
  if (stored !== null) return stored;
  const actual = toFloat(row?.actual_kwh), predicted = toFloat(row?.previous_prediction_kwh);
  return actual !== null && predicted !== null ? actual - predicted : null;
}
function saveCache(data) {
  try { localStorage.setItem('energy-dashboard-cache', JSON.stringify({ cachedAt: Date.now(), data })); }
  catch (err) { console.warn('[EnergyDash] Cache write skipped:', err); }
}
async function fetchWithRetry(url, asJson = false) {
  const match = url.match(/^https:\/\/raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/([^/]+)\/(.+)$/);
  const cdn = match ? `https://cdn.jsdelivr.net/gh/${match[1]}/${match[2]}@${match[3]}/${match[4]}` : url;
  let lastError;
  for (const candidate of [...new Set([url, cdn])]) {
    for (let attempt = 1; attempt <= CONFIG.maxRetries; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), CONFIG.requestTimeoutMs);
      try {
        const separator = candidate.includes('?') ? '&' : '?';
        const response = await fetch(`${candidate}${separator}t=${Date.now()}`, { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return asJson ? await response.json() : await response.text();
      } catch (err) {
        lastError = err;
        if (attempt < CONFIG.maxRetries) await new Promise(resolve => setTimeout(resolve, 400 * attempt));
      } finally { clearTimeout(timer); }
    }
  }
  const reason = lastError?.name === 'AbortError' ? `request timed out after ${CONFIG.requestTimeoutMs / 1000}s` : (lastError?.message || 'request failed');
  throw new Error(`${reason} (${url})`);
}
function normalizePayload(payload, source) {
  return {
    aiRows: Array.isArray(payload?.ai_results) ? payload.ai_results : [],
    hourlyRows: Array.isArray(payload?.hourly_predictions) ? payload.hourly_predictions : [],
    dailyTotalRows: Array.isArray(payload?.daily_totals) ? payload.daily_totals : [],
    stateData: payload?.state && typeof payload.state === 'object' ? payload.state : {},
    health: { ...(payload?.health || {}), source, generated_at: payload?.generated_at || payload?.health?.generated_at },
  };
}
async function loadAllData() {
  // Only query the local backend on localhost. GitHub Pages and file:// builds
  // continue to use the existing public-data fallback.
  const isLocalHost = ['localhost', '127.0.0.1', '::1'].includes(location.hostname);
  if (isLocalHost) {
    try {
      const payload = await fetchWithRetry(CONFIG.urls.localApi, true);
      const normalized = normalizePayload(payload, 'PostgreSQL local API');
      saveCache(normalized);
      return normalized;
    } catch (err) {
      console.warn('[EnergyDash] Local PostgreSQL API unavailable; trying GitHub data.', err);
    }
  }
  try {
    const envelope = await fetchWithRetry(CONFIG.urls.liveDataApi, true);
    const payload = envelope?.content ? JSON.parse(atob(envelope.content.replace(/\s/g, ''))) : envelope;
    const normalized = normalizePayload(payload, 'live consolidated payload');
    saveCache(normalized); return normalized;
  } catch (apiError) {
    try {
      const payload = await fetchWithRetry(CONFIG.urls.dashboardData, true);
      const normalized = normalizePayload(payload, 'live consolidated payload');
      saveCache(normalized); return normalized;
    } catch (combinedError) {
      const [aiText, hourlyText, dailyText, stateData] = await Promise.all([
        fetchWithRetry(CONFIG.urls.aiResults), fetchWithRetry(CONFIG.urls.hourlyPredictions),
        fetchWithRetry(CONFIG.urls.dailyTotals), fetchWithRetry(CONFIG.urls.state, true),
      ]);
      const normalized = { aiRows: parseCSV(aiText), hourlyRows: parseCSV(hourlyText), dailyTotalRows: parseCSV(dailyText), stateData, health: { collector_status: 'legacy payload', source: 'legacy files fallback', error: `${apiError.message}; ${combinedError.message}` } };
      saveCache(normalized); return normalized;
    }
  }
}
function getLatestAiRow(rows) {
  if (!rows?.length) return null;
  return rows.reduce((best, row) => !best || String(row.date || '') > String(best.date || '') ? row : best, null);
}
function getHourlyForDate(rows, targetDate) {
  return (!rows?.length || !targetDate) ? [] : rows.filter(row => String(row.date) === String(targetDate));
}
function currentISTParts() {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: CONFIG.timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', hour12: false }).formatToParts(new Date());
  const value = key => parts.find(part => part.type === key)?.value;
  const hour = Number(value('hour'));
  return { date: `${value('year')}-${value('month')}-${value('day')}`, hour: hour === 24 ? 0 : hour };
}
function getHourlyDisplayContext(aiRows, hourlyRows) {
  const latest = getLatestAiRow(aiRows);
  const selected = selectedHistoryDate ? aiRows.find(row => row.date === selectedHistoryDate) : null;
  const display = selected || latest;
  const today = currentISTParts().date;
  const todaysRows = getHourlyForDate(hourlyRows, today);
  if (!selectedHistoryDate && todaysRows.length && latest?.date !== today) return { date: today, pendingDailyDate: latest?.date || null };
  return { date: display?.date || null, pendingDailyDate: null };
}
function mergeLiveActuals(hourlyRows, stateData) {
  const rows = hourlyRows.map(row => ({ ...row }));
  const current = currentISTParts();
  const profile = stateData?.profiles?.[current.date];
  if (!profile) return rows;
  const byHour = new Map(rows.map(row => [Number(row.hour), row]));
  for (let h = 0; h < current.hour; h++) {
    const actual = toFloat(profile[String(h)]);
    const row = byHour.get(h);
    if (!row || actual === null) continue;
    row.actual_kwh = String(actual);
    const prediction = toFloat(row.predicted_kwh);
    if (prediction !== null) row.error_kwh = String(actual - prediction);
  }
  return rows;
}
function renderKPI(ai) {
  if (!ai) return;
  const actualDate = ai.actual_date || ai.date || 'latest completed day';
  const forecastDate = ai.forecast_date || (ai.date ? getNextDay(ai.date) : actualDate);
  if (UI.kpiPredictedLabel) UI.kpiPredictedLabel.textContent = 'Predicted Energy';
  if (UI.kpiActualLabel) UI.kpiActualLabel.textContent = 'Actual Energy';
  if (UI.kpiErrorLabel) UI.kpiErrorLabel.textContent = 'Saved / Over used';
  if (UI.kpiPredictedDate) UI.kpiPredictedDate.textContent = formatDisplayDate(forecastDate);
  if (UI.kpiActualDate) UI.kpiActualDate.textContent = formatDisplayDate(actualDate);
  if (UI.kpiErrorDate) UI.kpiErrorDate.textContent = formatDisplayDate(actualDate);
  if (UI.kpiPredicted) UI.kpiPredicted.textContent = fmt(ai.prediction_kwh) ?? '—';
  const lower = fmt(ai.prediction_lower_kwh), upper = fmt(ai.prediction_upper_kwh);
  if (UI.predictionRange) UI.predictionRange.textContent = lower !== null && upper !== null ? `Expected range: ${lower}–${upper} kWh` : 'Expected range: —';
  const actual = fmt(ai.actual_kwh);
  if (UI.kpiActual) UI.kpiActual.textContent = actual ?? 'Pending';
  if (UI.kpiActualUnit) UI.kpiActualUnit.textContent = actual !== null ? 'kWh' : '';
  if (UI.kpiActualHint) UI.kpiActualHint.textContent = actual !== null ? '24h recorded total' : 'Awaiting daily summary';
  const err = rowPredictionError(ai);
  const icon = document.querySelector('#card-error .kpi-card__icon');
  if (err === null) {
    if (UI.kpiError) UI.kpiError.textContent = 'N/A';
    if (UI.kpiErrorUnit) UI.kpiErrorUnit.textContent = '';
    if (UI.kpiErrorHint) UI.kpiErrorHint.textContent = 'Awaiting forecast calculation';
    if (icon) icon.className = 'kpi-card__icon';
  } else if (err < -0.01) {
    if (UI.kpiErrorLabel) UI.kpiErrorLabel.textContent = 'Saved';
    if (UI.kpiError) UI.kpiError.textContent = fmt(Math.abs(err));
    if (UI.kpiErrorUnit) UI.kpiErrorUnit.textContent = 'kWh saved';
    if (UI.kpiErrorHint) UI.kpiErrorHint.textContent = `${fmt(Math.abs(err))} kWh below forecast`;
    if (icon) icon.className = 'kpi-card__icon kpi-card__icon--green';
  } else if (err > 0.01) {
    if (UI.kpiErrorLabel) UI.kpiErrorLabel.textContent = 'Over used';
    if (UI.kpiError) UI.kpiError.textContent = fmt(Math.abs(err));
    if (UI.kpiErrorUnit) UI.kpiErrorUnit.textContent = 'kWh over';
    if (UI.kpiErrorHint) UI.kpiErrorHint.textContent = `${fmt(Math.abs(err))} kWh above forecast`;
    if (icon) icon.className = 'kpi-card__icon kpi-card__icon--amber';
  } else {
    if (UI.kpiErrorLabel) UI.kpiErrorLabel.textContent = 'Saved / Over used';
    if (UI.kpiError) UI.kpiError.textContent = '0.00';
    if (UI.kpiErrorUnit) UI.kpiErrorUnit.textContent = 'kWh';
    if (UI.kpiErrorHint) UI.kpiErrorHint.textContent = 'Exact match with forecast';
    if (icon) icon.className = 'kpi-card__icon kpi-card__icon--green';
  }
}
function renderChart(hourlyRows, context = {}) {
  const canvas = $('hourly-chart');
  if (!canvas || typeof Chart === 'undefined') return;
  const map = Array.from({ length: 24 }, () => ({ predicted: null, actual: null, error: null }));
  hourlyRows.forEach(row => {
    const h = Number.parseInt(row.hour, 10); if (!Number.isInteger(h) || h < 0 || h > 23) return;
    const pred = toFloat(row.predicted_kwh), actual = toFloat(row.actual_kwh), rawError = toFloat(row.error_kwh);
    map[h] = { predicted: pred, actual, error: pred !== null && actual !== null ? Number((pred - actual).toFixed(2)) : rawError !== null ? -rawError : null };
  });
  const hasActual = map.some(row => row.actual !== null), hasError = map.some(row => row.error !== null);
  const datasets = [{ label: 'Predicted kWh', data: map.map(row => row.predicted), borderColor: 'rgba(59,130,246,1)', backgroundColor: 'rgba(59,130,246,0.12)', borderWidth: 2.5, pointRadius: 3, tension: 0.4, fill: true, spanGaps: true }];
  if (hasActual) datasets.push({ label: 'Actual kWh', data: map.map(row => row.actual), borderColor: 'rgba(34,197,94,1)', backgroundColor: 'rgba(34,197,94,0.08)', borderWidth: 2.5, pointRadius: 3, tension: 0.4, fill: false, spanGaps: true });
  if (hasError) datasets.push({ label: 'Saved / Over used (kWh)', data: map.map(row => row.error), borderColor: 'rgba(245,158,11,1)', borderDash: [5, 4], borderWidth: 1.5, pointRadius: 2, tension: 0.3, fill: false, spanGaps: true });
  const labels = Array.from({ length: 24 }, (_, h) => hourLabel(h));
  const chartDate = hourlyRows[0]?.date || context.date || 'no matching date';
  if (UI.hourlyDataDate) UI.hourlyDataDate.textContent = context.pendingDailyDate ? `Forecast date: ${chartDate} · Daily result pending (latest: ${context.pendingDailyDate})` : `Data date: ${chartDate}`;
  if (UI.hourlyChartSummary) UI.hourlyChartSummary.textContent = hourlyRows.length ? `Hourly chart for ${chartDate}. ${hasActual ? 'Actual values are available for completed hours.' : 'Actual values are not yet available.'}` : 'No hourly records are available for the selected date.';
  const options = { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => ctx.parsed.y == null ? `${ctx.dataset.label}: N/A` : `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)} kWh` } } }, scales: { x: { ticks: { color: '#64748b', maxRotation: 45, autoSkip: true, maxTicksLimit: 12 }, grid: { color: 'rgba(255,255,255,.05)' } }, y: { ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,.05)' }, title: { display: true, text: 'Energy (kWh)', color: '#64748b' } } } };
  if (hourlyChart) { hourlyChart.data = { labels, datasets }; hourlyChart.options = options; hourlyChart.update('active'); }
  else hourlyChart = new Chart(canvas.getContext('2d'), { type: 'line', data: { labels, datasets }, options });
}
function renderTable(hourlyRows, context = {}) {
  if (!UI.hourlyTableBody) return;
  const map = Array.from({ length: 24 }, () => ({ predicted: null, actual: null, error: null }));
  hourlyRows.forEach(row => { const h = Number.parseInt(row.hour, 10); if (Number.isInteger(h) && h >= 0 && h <= 23) map[h] = { predicted: toFloat(row.predicted_kwh), actual: toFloat(row.actual_kwh), error: toFloat(row.error_kwh) }; });
  const hasActual = map.some(row => row.actual !== null);
  if (UI.noActualNote) {
    UI.noActualNote.classList.remove('hidden');
    UI.noActualNote.lastChild.textContent = !hourlyRows.length ? 'No hourly records are available for the selected date' : hasActual ? 'Actual values are shown for completed hours; final daily results are confirmed after the 24-hour cycle.' : 'Actual values are not yet available for this period';
  }
  UI.hourlyTableBody.textContent = '';
  for (let h = 0; h < 24; h++) {
    const item = map[h], tr = document.createElement('tr');
    const td = (value, cls = '') => { const cell = document.createElement('td'); cell.textContent = value; if (cls) cell.className = cls; tr.appendChild(cell); return cell; };
    td(hourLabel(h)); td(item.predicted === null ? 'N/A' : item.predicted.toFixed(2), item.predicted === null ? 'td-na' : 'td-predicted'); td(item.actual === null ? '—' : item.actual.toFixed(2), item.actual === null ? 'td-na' : 'td-actual');
    const err = item.actual !== null && item.predicted !== null ? item.actual - item.predicted : item.error !== null ? -item.error : null;
    if (err === null) { td('—', 'td-na'); td('—', 'td-na'); }
    else if (err < -0.005) { td(Math.abs(err).toFixed(2), 'td-actual'); td('—', 'td-na'); }
    else if (err > 0.005) { td('—', 'td-na'); td(err.toFixed(2), 'td-error'); }
    else { td('0.00', 'td-actual'); td('—', 'td-na'); }
    UI.hourlyTableBody.appendChild(tr);
  }
}
function historyDataStatus(row) { return row.data_status || (row.status === 'daily_total_only' ? 'daily_total_only' : row.status === 'data_incomplete' ? 'data_incomplete' : 'complete'); }
function historyEnergyStatus(row) { return row.status === 'normal' || row.status === 'daily_total_only' ? 'Normal' : row.status === 'anomaly' ? 'Anomaly' : 'data_incomplete'; }
function renderHistory(aiRows, dailyTotalRows = []) {
  if (!UI.historyTableBody) return;
  const byDate = new Map((aiRows || []).filter(row => row.date).map(row => [row.date, { ...row }]));
  dailyTotalRows.forEach(total => { if (!total.date) return; const row = byDate.get(total.date) || { date: total.date }; if (toFloat(row.actual_kwh) === null && toFloat(total.total_kwh) !== null) row.actual_kwh = total.total_kwh; row.data_status ||= 'daily_total_only'; row.status ||= 'daily_total_only'; byDate.set(total.date, row); });
  const all = [...byDate.values()].sort((a, b) => String(b.date).localeCompare(String(a.date)));
  const dates = all.map(row => row.date).filter(Boolean);
  if (UI.historyDateSelect) {
    const old = selectedHistoryDate; UI.historyDateSelect.textContent = '';
    const allOpt = document.createElement('option'); allOpt.value = ''; allOpt.textContent = 'All days'; UI.historyDateSelect.appendChild(allOpt);
    dates.forEach(d => { const opt = document.createElement('option'); opt.value = d; opt.textContent = d; UI.historyDateSelect.appendChild(opt); });
    UI.historyDateSelect.value = dates.includes(old) ? old : '';
  }
  const shown = selectedHistoryDate ? all.filter(row => row.date === selectedHistoryDate) : all;
  UI.historyTableBody.textContent = '';
  shown.forEach(row => {
    const tr = document.createElement('tr');
    [row.date || '—', historyDataStatus(row), historyEnergyStatus(row), fmt(row.actual_kwh) ?? 'Pending', fmt(row.previous_prediction_kwh) ?? '—', fmt(rowPredictionError(row)) ?? '—'].forEach((value, index) => { const cell = document.createElement('td'); cell.textContent = value; if (index === 2) cell.className = String(value).toLowerCase() === 'anomaly' ? 'td-error' : 'td-actual'; tr.appendChild(cell); });
    UI.historyTableBody.appendChild(tr);
  });
  if (!shown.length) { const tr = document.createElement('tr'), td = document.createElement('td'); td.colSpan = 6; td.textContent = 'No completed daily records yet.'; tr.appendChild(td); UI.historyTableBody.appendChild(tr); }
}
function renderAnomalyInvestigation(hourlyRows) {
  if (!UI.anomalyInvestigationList) return;
  const rows = hourlyRows.map(row => ({ hour: row.hour, actual: toFloat(row.actual_kwh), expected: toFloat(row.predicted_kwh) })).filter(row => row.actual !== null && row.expected !== null).map(row => ({ ...row, deviation: row.actual - row.expected, percent: row.expected ? ((row.actual - row.expected) / row.expected) * 100 : null })).sort((a, b) => Math.abs(b.deviation) - Math.abs(a.deviation)).slice(0, 5);
  UI.anomalyInvestigationList.textContent = '';
  if (!rows.length) { const li = document.createElement('li'); li.textContent = 'No completed actual-versus-expected hourly comparisons available.'; UI.anomalyInvestigationList.appendChild(li); return; }
  rows.forEach(row => { const li = document.createElement('li'); li.textContent = `${hourLabel(row.hour)}: actual ${row.actual.toFixed(2)} kWh, expected ${row.expected.toFixed(2)} kWh — ${Math.abs(row.deviation).toFixed(2)} kWh (${Math.abs(row.percent || 0).toFixed(1)}%) ${row.deviation >= 0 ? 'above' : 'below'} expected.`; UI.anomalyInvestigationList.appendChild(li); });
}
function renderHealth(stateData, health = {}) {
  const lastRun = stateData?.last_run;
  if (UI.collectorLastRun) UI.collectorLastRun.textContent = formatISOtoIST(lastRun);
  const age = lastRun ? Math.max(0, Math.round((Date.now() - new Date(lastRun).getTime()) / 60000)) : null;
  const healthy = health.collector_status === 'healthy' && lastRun && age < 45;
  if (UI.collectorHealth) { UI.collectorHealth.textContent = healthy ? 'Healthy' : 'Needs attention'; UI.collectorHealth.className = healthy ? 'health-good' : 'health-warn'; }
  if (UI.dataFreshness) UI.dataFreshness.textContent = age === null ? 'Unknown' : `${age} min ago`;
  const status = health.data_quality?.data_status || health.data_quality?.status || health.data_status || 'unknown';
  if (UI.currentDataStatus) { UI.currentDataStatus.textContent = health.data_quality?.completed_hours != null ? `${status} · ${health.data_quality.completed_hours} completed hours` : status; UI.currentDataStatus.className = status === 'complete' ? 'health-good' : 'health-warn'; }
}
function renderDailyChart(aiRows, dailyTotalRows = []) {
  const canvas = $('daily-history-chart'); if (!canvas || typeof Chart === 'undefined') return;
  const byDate = new Map((aiRows || []).filter(row => row.date).map(row => [row.date, { ...row }]));
  dailyTotalRows.forEach(total => { if (!total.date) return; const row = byDate.get(total.date) || { date: total.date }; if (toFloat(row.actual_kwh) === null) row.actual_kwh = total.total_kwh; byDate.set(total.date, row); });
  const rows = [...byDate.values()].filter(row => toFloat(row.actual_kwh) !== null || toFloat(row.previous_prediction_kwh) !== null).sort((a, b) => String(a.date).localeCompare(String(b.date))).slice(-historyDays);
  if (UI.historyCaption) UI.historyCaption.textContent = `Last ${historyDays} days (${rows.length} records)`;
  if (UI.dailyChartSummary) UI.dailyChartSummary.textContent = rows.length ? `Daily energy history for ${rows[0].date} through ${rows[rows.length - 1].date}, showing actual and forecast values.` : 'No daily historical records are available.';
  const data = { labels: rows.map(row => formatDisplayDate(row.date)), datasets: [
    { label: 'Actual kWh', data: rows.map(row => toFloat(row.actual_kwh)), borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,.12)', tension: .35, spanGaps: true },
    { label: 'Forecast kWh', data: rows.map(row => toFloat(row.previous_prediction_kwh)), borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,.10)', tension: .35, spanGaps: true },
  ] };
  const options = { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#64748b', maxRotation: 45 }, grid: { color: 'rgba(255,255,255,.05)' } }, y: { ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,.05)' }, title: { display: true, text: 'Energy (kWh)', color: '#64748b' } } } };
  if (dailyChart) { dailyChart.data = data; dailyChart.options = options; dailyChart.update('active'); }
  else dailyChart = new Chart(canvas.getContext('2d'), { type: 'line', data, options });
}
function renderAIImprovement(aiRows, stateData = {}) {
  if (!UI.aiLearningStatus) return;
  const completed = aiRows.filter(row => toFloat(row.actual_kwh) !== null).sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const previous = completed.length ? completed[completed.length - 1] : null;
  const current = currentISTParts(), profile = stateData?.profiles?.[current.date] || {};
  const todayEntries = Object.entries(profile).filter(([hour, value]) => Number(hour) < current.hour && toFloat(value) !== null);
  const previousProfile = previous ? stateData?.profiles?.[previous.date] || {} : {};
  const pairs = todayEntries.filter(([hour]) => toFloat(previousProfile[hour]) !== null).map(([hour, value]) => [Number(value), Number(previousProfile[hour])]);
  const todayTotal = pairs.reduce((sum, p) => sum + p[0], 0), previousTotal = pairs.reduce((sum, p) => sum + p[1], 0);
  const diff = pairs.length ? todayTotal - previousTotal : null;
  const pct = pairs.length ? diff / Math.max(Math.abs(previousTotal), 1) * 100 : null;
  UI.aiLearningStatus.textContent = 'day comparison';
  if (UI.aiLearningSamples) UI.aiLearningSamples.textContent = String(pairs.length);
  if (UI.aiBaselineMae) UI.aiBaselineMae.textContent = pairs.length ? previousTotal.toFixed(2) : '—';
  if (UI.aiAdaptedMae) UI.aiAdaptedMae.textContent = pairs.length ? todayTotal.toFixed(2) : '—';
  if (UI.aiDifferenceKwh) UI.aiDifferenceKwh.textContent = diff === null ? '—' : `${diff >= 0 ? '+' : ''}${diff.toFixed(2)}`;
  if (UI.aiImprovementPercent) UI.aiImprovementPercent.textContent = pct === null ? '—' : `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
  if (UI.aiRateComparison) UI.aiRateComparison.textContent = pairs.length ? `${(todayTotal / pairs.length).toFixed(1)} / ${(previousTotal / pairs.length).toFixed(1)}` : '—';
  if (UI.aiImprovementMessage) UI.aiImprovementMessage.textContent = pairs.length ? `Like-for-like comparison across ${pairs.length} matching hours: today's usage is ${Math.abs(diff).toFixed(2)} kWh ${diff >= 0 ? 'higher' : 'lower'} than ${previous?.date || 'the previous day'}.` : 'Waiting for matching completed hours from today and the previous completed day.';
}
function showLoading() {
  UI.loadingOverlay?.classList.remove('hidden'); UI.errorOverlay?.classList.add('hidden'); UI.mainContent?.classList.add('hidden');
  if (UI.updateDot) UI.updateDot.className = 'update-dot update-dot--loading';
  UI.refreshIcon?.classList.add('btn-refresh__icon--spinning'); if (UI.lastUpdated) UI.lastUpdated.textContent = 'Loading…';
}
function showError(message) {
  UI.loadingOverlay?.classList.add('hidden'); UI.errorOverlay?.classList.remove('hidden'); UI.mainContent?.classList.add('hidden');
  if (UI.updateDot) UI.updateDot.className = 'update-dot update-dot--error'; UI.refreshIcon?.classList.remove('btn-refresh__icon--spinning');
  if (UI.errorMessage) UI.errorMessage.textContent = `Failed to load data: ${message}. Check the backend or internet connection, then try again.`;
  if (UI.lastUpdated) UI.lastUpdated.textContent = 'Error';
}
function showContent() {
  UI.loadingOverlay?.classList.add('hidden'); UI.errorOverlay?.classList.add('hidden'); UI.mainContent?.classList.remove('hidden');
  if (UI.updateDot) UI.updateDot.className = 'update-dot'; UI.refreshIcon?.classList.remove('btn-refresh__icon--spinning');
  if (UI.lastUpdated) UI.lastUpdated.textContent = `Updated ${nowIST()} IST`;
}
async function refresh() {
  showLoading();
  try {
    const { aiRows, hourlyRows, dailyTotalRows, stateData, health } = await loadAllData();
    activePayload = { aiRows, hourlyRows, dailyTotalRows, stateData, health };
    const latest = getLatestAiRow(aiRows);
    const selected = selectedHistoryDate ? aiRows.find(row => row.date === selectedHistoryDate) : null;
    const display = selected || latest;
    const context = getHourlyDisplayContext(aiRows, hourlyRows);
    const displayHourly = context.date === currentISTParts().date ? mergeLiveActuals(getHourlyForDate(hourlyRows, context.date), stateData) : getHourlyForDate(hourlyRows, context.date);
    renderKPI(display); renderChart(displayHourly, context); renderTable(displayHourly, context); renderAnomalyInvestigation(displayHourly);
    renderHistory(aiRows, dailyTotalRows); renderHealth(stateData, health, dailyTotalRows); renderDailyChart(aiRows, dailyTotalRows); renderAIImprovement(aiRows, stateData);
    showContent();
  } catch (err) {
    console.error('[EnergyDash] Fetch error:', err);
    try {
      const stored = JSON.parse(localStorage.getItem('energy-dashboard-cache') || 'null');
      const cached = stored?.data || stored, at = Number(stored?.cachedAt || 0);
      if (at && Date.now() - at <= CONFIG.cacheMaxAgeMs && cached?.aiRows?.length) {
        activePayload = cached;
        const latest = getLatestAiRow(cached.aiRows), selected = selectedHistoryDate ? cached.aiRows.find(row => row.date === selectedHistoryDate) : null;
        const context = getHourlyDisplayContext(cached.aiRows, cached.hourlyRows || []);
        const displayHourly = context.date === currentISTParts().date ? mergeLiveActuals(getHourlyForDate(cached.hourlyRows || [], context.date), cached.stateData) : getHourlyForDate(cached.hourlyRows || [], context.date);
        renderKPI(selected || latest); renderChart(displayHourly, context); renderTable(displayHourly, context); renderAnomalyInvestigation(displayHourly);
        renderHistory(cached.aiRows, cached.dailyTotalRows || []); renderHealth(cached.stateData, { ...(cached.health || {}), collector_status: 'degraded' }); renderDailyChart(cached.aiRows, cached.dailyTotalRows || []); renderAIImprovement(cached.aiRows, cached.stateData);
        showContent(); if (UI.lastUpdated) UI.lastUpdated.textContent = `Showing cached data · ${nowIST()} IST`; if (UI.updateDot) UI.updateDot.className = 'update-dot update-dot--error'; return;
      }
    } catch (cacheError) { console.error('[EnergyDash] Cache error:', cacheError); }
    showError(err.message || 'Unknown error');
  }
}
function scheduleAutoRefresh() { clearInterval(refreshTimer); refreshTimer = setInterval(refresh, CONFIG.refreshInterval); }
function handleRefreshClick() { clearInterval(refreshTimer); refresh().finally(scheduleAutoRefresh); }
async function handleDataDownload(event) {
  const link = event.currentTarget, url = link.href, filename = link.getAttribute('download') || url.split('/').pop() || 'energy-data';
  event.preventDefault(); link.classList.add('is-downloading');
  try {
    let response;
    try { response = await fetch(`${url}${url.includes('?') ? '&' : '?'}download=${Date.now()}`, { cache: 'no-store' }); if (!response.ok) throw new Error(`HTTP ${response.status}`); }
    catch {
      let blob;
      if (filename.includes('daily') && activePayload?.dailyTotalRows?.length) blob = new Blob([convertToCSV(activePayload.dailyTotalRows)], { type: 'text/csv' });
      else if (filename.includes('hourly') && activePayload?.hourlyRows?.length) blob = new Blob([convertToCSV(activePayload.hourlyRows)], { type: 'text/csv' });
      else if (filename.includes('health') && activePayload?.health) blob = new Blob([JSON.stringify(activePayload.health, null, 2)], { type: 'application/json' });
      else if (activePayload) blob = new Blob([JSON.stringify(activePayload, null, 2)], { type: 'application/json' });
      if (blob) response = new Response(blob);
    }
    if (!response) throw new Error('No data available for export');
    const blobUrl = URL.createObjectURL(await response.blob()), a = document.createElement('a'); a.href = blobUrl; a.download = filename; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
  } catch (err) { console.warn('[EnergyDash] Download fallback:', err); window.open(url, '_blank', 'noopener'); }
  finally { link.classList.remove('is-downloading'); }
}

UI.btnRefresh?.addEventListener('click', handleRefreshClick);
UI.btnRetry?.addEventListener('click', handleRefreshClick);
UI.historyDateSelect?.addEventListener('change', event => { selectedHistoryDate = event.target.value || ''; refresh(); });
document.querySelectorAll('.range-btn').forEach(button => button.addEventListener('click', () => {
  historyDays = Number(button.dataset.days) || 7;
  document.querySelectorAll('.range-btn').forEach(item => item.classList.toggle('is-active', item === button));
  if (activePayload?.aiRows?.length) renderDailyChart(activePayload.aiRows, activePayload.dailyTotalRows || []); else refresh();
}));
document.querySelectorAll('.download-controls a[download]').forEach(link => link.addEventListener('click', handleDataDownload));
(function init() { refresh().finally(scheduleAutoRefresh); })();
