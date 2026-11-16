import {FUNDING_WINDOW_MS, healthLabel, isDay, isNullableNumber, isRecord, isSnapshot, isString, isTimestamp} from './view-model.js';

// Validate fields used by the views before handing off a successful response.
async function read(path, validate) {
  let response;
  try {
    response = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(17000)});
  } catch (error) {
    throw Error(error.name === 'TimeoutError' ? 'The request timed out.' : 'Unable to reach the server.');
  }

  let data;
  try {
    data = await response.json();
  } catch {
    throw Error(`Invalid response from the server (HTTP ${response.status}).`);
  }

  if (!response.ok) {
    const issues = Array.isArray(data?.issues) ? data.issues.filter(isString).join('; ') : '';
    throw Error(issues || `HTTP ${response.status}`);
  }
  if (!isRecord(data) || !validate(data)) throw Error('Unexpected data from the server.');
  return data;
}

function isFundingWindows(data) {
  return ['complete', 'partial', 'unavailable'].includes(data.status)
    && Array.isArray(data.windows) && data.windows.every((window, index) => {
      if (!isRecord(window) || !isTimestamp(window.start_at) || !isTimestamp(window.end_at)) return false;
      const start = Date.parse(window.start_at);
      const end = Date.parse(window.end_at);
      return end - start === FUNDING_WINDOW_MS && end % FUNDING_WINDOW_MS === 0
        && (index === 0 || start === Date.parse(data.windows[index - 1].end_at))
        && isNullableNumber(window.net_funding_usdt);
    });
}

function isHistoryStatus(data) {
  return isRecord(data) && isString(data.state) && (data.covered_through === null || isDay(data.covered_through));
}

function isBaseline(data) {
  return isRecord(data) && isTimestamp(data.as_of) && Number.isFinite(data.equity);
}

function isDailySeries(data) {
  return Array.isArray(data) && data.length > 0 && data.every((row) => isBaseline(row)
    && isDay(row.date) && Number.isFinite(row.daily_funding));
}

function isHistory(data) {
  return data.status === 'available' && isRecord(data.window)
    && isDay(data.window.start_date) && isDay(data.window.end_date)
    && isHistoryStatus(data.ingestion) && isBaseline(data.baseline) && isDailySeries(data.series)
    && Number.isFinite(data.initial_cash)
    && [data.baseline, ...data.series].every((row) => Number.isFinite(row.cumulative_funding));
}

function isFundingHistory(data) {
  return isString(data.symbol) && Array.isArray(data.days) && data.days.length > 0
    && data.days.every((row) => isRecord(row) && isDay(row.date) && Number.isFinite(row.rate)
      && Number.isInteger(row.settlements))
    && Array.isArray(data.events) && data.events.length > 0
    && data.events.every((event) => isRecord(event) && isTimestamp(event.as_of) && Number.isFinite(event.rate));
}

function isBacktest(data) {
  return data.status === 'available' && isRecord(data.window)
    && isDay(data.window.start_date) && isDay(data.window.end_date) && Number.isInteger(data.window.complete_days)
    && isRecord(data.inputs) && Number.isFinite(data.inputs.slippage_bps)
    && isRecord(data.metrics) && isBaseline(data.baseline) && isDailySeries(data.series)
    && [data.metrics.entry_slippage, data.metrics.exit_slippage, data.metrics.total_slippage,
      data.metrics.turnover_notional].every(Number.isFinite)
    && isNullableNumber(data.metrics.break_even_slippage_bps)
    && Number.isFinite(data.series.at(-1).mark_to_market_pnl)
    && Array.isArray(data.issues) && data.issues.every(isString)
    && Array.isArray(data.entries) && data.entries.every((row) => isRecord(row) && isString(row.asset));
}

function isDiagnostics(data) {
  return Array.isArray(data.observations) && data.observations.every((row) => isRecord(row)
    && isString(row.request_id) && isTimestamp(row.observed_at) && isRecord(row.snapshot));
}

export const api = {
  snapshot: () => read('/api/snapshot', isSnapshot),
  fundingWindows: () => read('/api/funding-windows', isFundingWindows),
  history: () => read('/api/history', isHistory),
  historyStatus: () => read('/api/history/status', isHistoryStatus),
  fundingHistory: (symbol) => read(`/api/funding-history?${new URLSearchParams({symbol})}`, isFundingHistory),
  backtestBounds: () => read('/api/backtest/bounds', (bounds) => isDay(bounds.start) && isDay(bounds.end)
    && bounds.start <= bounds.end),
  backtest: (params) => read(`/api/backtest?${params}`, isBacktest),
  diagnostics: (filters) => read(`/api/diagnostics?${filters}`, isDiagnostics),
  health: () => read('/api/health', (data) => Object.hasOwn(healthLabel, data.state)),
};
