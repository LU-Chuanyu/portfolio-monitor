export const MINUTE_MS = 60_000;
export const FUNDING_WINDOW_MS = 8 * 60 * MINUTE_MS;

export function observationTime(value, end = false) {
  const match = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(Z|[+-]\d{2}:\d{2})?)?$/.exec(value ?? '');
  if (!match || !isDay(match[1])) return null;
  const [, day, time = end ? '23:59:59' : '00:00:00', zone = 'Z'] = match;
  if (time.startsWith('24:')) return null;
  const stamp = new Date(`${day}T${time}${zone}`);
  if (!Number.isFinite(stamp.valueOf())) return null;
  const utc = stamp.toISOString();
  return [utc.slice(0, 10), ...utc.slice(11, 19).split(':')];
}

export function observationBound([day, hour, minute, second], end = false) {
  return day ? `${day}T${hour}:${minute}:${second}${end ? '.999' : ''}Z` : '';
}

export const isRecord = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
export const isTimestamp = (value) => typeof value === 'string'
  && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.test(value)
  && Number.isFinite(Date.parse(value));
export function isDay(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === value;
}
export const isString = (value) => typeof value === 'string';
export const isNullableNumber = (value) => value === null || Number.isFinite(value);

const quoteStatuses = ['ready', 'partial', 'unavailable'];

function isSettlement(settlement, asOf) {
  if (!isRecord(settlement) || settlement.as_of !== asOf) return false;
  const {timestamp_ms: stamp, amount_usdt: amount, included_contracts: included, total_contracts: total} = settlement;
  if (!Number.isSafeInteger(total) || total < 0 || !Number.isSafeInteger(included) || included < 0 || included > total) return false;
  if (stamp === null) return included === 0 && amount === null;
  return Number.isSafeInteger(stamp) && stamp > Date.parse(asOf) && included > 0 && isNullableNumber(amount);
}

export function isQuote(value) {
  return isRecord(value) && quoteStatuses.includes(value.status) && isTimestamp(value.as_of)
    && isNullableNumber(value.equity)
    && Array.isArray(value.positions) && value.positions.every((row) => isRecord(row)
      && [row.asset, row.symbol].every(isString) && ['SPOT', 'PERP'].includes(row.instrument)
      && ['LONG', 'SHORT'].includes(row.side)
      && [row.quantity, row.entry_price_usdt, row.current_price, row.value, row.pnl, row.funding_rate,
          row.next_funding_time, row.expected_funding].every(isNullableNumber))
    && Array.isArray(value.asset_deltas) && value.asset_deltas.every((row) => isRecord(row)
      && isString(row.asset) && isNullableNumber(row.delta))
    && isRecord(value.sources) && Object.values(value.sources).every((source) => isRecord(source)
      && isString(source.status) && (source.reason == null || isString(source.reason)))
    && Array.isArray(value.issues) && value.issues.every(isString)
    && (value.stream == null || (isRecord(value.stream) && Object.values(value.stream).every(
      (source) => isRecord(source) && (source.reason == null || isString(source.reason)))))
    && (value.status !== 'ready' || (value.positions.length > 0 && hasCurrentEquity(value)
      && value.positions.every((row) => Number.isFinite(row.current_price))))
    && (value.next_settlement === undefined || isSettlement(value.next_settlement, value.as_of));
}

export function isSamples(value) {
  return Array.isArray(value) && value.every((sample) => isRecord(sample)
    && Number.isFinite(sample.t) && isNullableNumber(sample.value));
}

export function isSnapshot(value) {
  return isQuote(value) && isSamples(value.samples)
    && (value.last_complete == null || (isQuote(value.last_complete) && value.last_complete.status === 'ready'));
}

export function hasCurrentEquity(quote) {
  return Number.isFinite(quote?.equity) && ['spot', 'linear'].every(
    (name) => quote.sources?.[name]?.status === 'available');
}

export function displayedQuote(observed, lastComplete, current) {
  if (observed.status === 'ready' || hasCurrentEquity(observed)) return observed;
  if (lastComplete && (!hasCurrentEquity(current) || Date.parse(lastComplete.as_of) >= Date.parse(current.as_of))) {
    return lastComplete;
  }
  return hasCurrentEquity(current) ? current : observed;
}

export function sameQuote(first, second) {
  return Boolean(first && second && (first.request_id ?? first.as_of) === (second.request_id ?? second.as_of));
}

export function nextMinuteReadAt(now, latestSampleTime, retryInterval) {
  const readOffset = 1500;
  const nextBoundaryRead = (Math.floor((now - readOffset) / MINUTE_MS) + 1) * MINUTE_MS + readOffset;
  const minuteStart = Math.floor(now / MINUTE_MS) * MINUTE_MS;
  const retryUntil = minuteStart + 3 * retryInterval;
  // A REST request started at the UTC boundary can take longer than the read offset.
  if (latestSampleTime < minuteStart && now < retryUntil) return Math.min(now + retryInterval, retryUntil);
  return nextBoundaryRead;
}

// Use the server-selected settlement group and expire it at the browser clock.
export function nextSettlement(quote, now) {
  const settlement = quote.next_settlement;
  if (!isRecord(settlement) || !Number.isFinite(settlement.timestamp_ms)) {
    return {state: 'unavailable', amount: null};
  }
  const {timestamp_ms: stamp, amount_usdt: amount, included_contracts: included, total_contracts: total} = settlement;
  if (stamp <= now) return {state: 'expired', amount: null};
  return {state: 'scheduled', stamp, amount: Number.isFinite(amount) ? amount : null, included, total};
}

export function latestRequest() {
  let revision = 0;
  return () => {
    const issued = ++revision;
    return () => issued === revision;
  };
}

export function querySignature(params) {
  const copy = new URLSearchParams(params);
  for (const key of ['start', 'end']) {
    if (copy.has(key)) copy.set(key, copy.get(key).trim());
  }
  copy.sort();
  return copy.toString();
}

export const healthLabel = {
  unknown: 'Waiting for quotes',
  healthy: 'Healthy',
  degraded: 'Incomplete quotes',
  alerting: 'Data alert',
  stale: 'Diagnostics stale',
  unavailable: 'Diagnostics unavailable',
};
