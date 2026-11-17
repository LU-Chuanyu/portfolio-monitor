import {api} from './api.js';
import {drawChart} from './chart.js';
import {
  FUNDING_WINDOW_MS, MINUTE_MS, displayedQuote, healthLabel, isQuote, isSamples,
  latestRequest, nextMinuteReadAt, nextSettlement, querySignature, sameQuote,
} from './view-model.js';

const demo = document.body.dataset.demo === 'true';
const sourceName = demo ? 'Synthetic demo' : 'Bybit';
const pages = {
  '/': ['overview', 'Portfolio Overview', 'Live funding, hedge exposure and positions'],
  '/positions': ['positions', 'Positions', ''],
  '/history': ['history', 'Historical Performance', ''],
  '/data': ['data', 'Data Health', `${sourceName} feed status and local observation log`],
};
const [view, title, subtitle] = pages[location.pathname] ?? pages['/'];
const el = (id) => document.getElementById(id);
const text = (id, value) => { el(id).textContent = value; };
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function format(value, digits = 2, signed = false) {
  if (!Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: digits, maximumFractionDigits: digits, signDisplay: signed ? 'exceptZero' : 'auto',
  }).format(value).replace('-', '−');
}
const money = (value, signed = false, digits = 2) => Number.isFinite(value) ? `${format(value, digits, signed)} USDT` : '—';
const pct = (value, digits = 4, signed = false) => Number.isFinite(value) ? `${format(value * 100, digits, signed)}%` : '—';
const clock = (value, full = false) => {
  const date = new Date(value);
  if (value == null || !Number.isFinite(date.valueOf())) return '—';
  const iso = date.toISOString();
  return `${full ? iso.slice(0, 10) + ' ' : ''}${iso.slice(11, 19)} UTC`;
};
const windowTime = new Intl.DateTimeFormat('en-GB', {
  timeZone:'UTC', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23',
});
const quote = (row, field) => format(row[field], ['HYPE','SOL'].includes(row.asset) ? 3 : 2);
const assetLink = (asset) => `/positions?asset=${encodeURIComponent(asset)}`;
const quoteStorage = demo ? 'portfolio-demo-quotes' : 'portfolio-live-quotes';
const minute = MINUTE_MS;
const fundingWindow = FUNDING_WINDOW_MS;
const fastRefreshInterval = 5000;
const staleAfter = 75000;
const colors = {BTC:'#77bbed', ETH:'#9688ed', SOL:'#76d3b1', HYPE:'#f1bf73', USDT:'#e88da4'};
const assetOptions = ['BTC','ETH','SOL','HYPE','USDT'];

el('main').append(el(view).content.cloneNode(true));
el('main').tabIndex = -1;
document.body.dataset.view = view;
text('page-title', title);
text('page-subtitle', subtitle);
document.title = `${title} · Portfolio Monitor`;
if (demo) {
  document.querySelector('.venue').textContent = 'Synthetic demo · USDT';
  text('status','Loading synthetic quotes');
  if (view === 'overview') text('feeds-title','Synthetic data quality');
}
document.querySelector(`[data-page="${view}"]`)?.setAttribute('aria-current', 'page');

function status(message, warning = false) {
  text('status', message);
  el('status').dataset.tone = warning ? 'warning' : 'normal';
}
function markup(container, content) { el(container).innerHTML = content; }
const motion = matchMedia('(prefers-reduced-motion: no-preference)');
function reveal(node) {
  if (motion.matches) node.animate([{opacity:.55,transform:'translateY(5px)'},{opacity:1,transform:'none'}],
    {duration:260,easing:'cubic-bezier(.2,.6,.3,1)'});
}
function quoteValue(id, value, signed = false, digits = 2) {
  const node = el(id);
  const next = money(value, signed, digits);
  const previous = node.textContent;
  node.textContent = next;
  if (signed) node.dataset.amount = value > 0 ? 'inflow' : value < 0 ? 'outflow' : 'neutral';
  if (motion.matches && previous.includes('USDT') && next.includes('USDT') && previous !== next) {
    node.getAnimations().forEach(animation=>animation.cancel());
    const tint = getComputedStyle(node).getPropertyValue('--change').trim();
    node.animate([{backgroundColor:tint},{backgroundColor:'transparent'}], {duration:520, easing:'ease-out'});
  }
}
function metrics(rows) {
  return rows.map(([label, value, reason]) => `<div><dt>${escape(label)}</dt><dd${reason ? ' data-unavailable="true"' : ''}>${escape(value)}${reason ? ` <small>${escape(reason)}</small>` : ''}</dd></div>`).join('');
}
function plot(id, rows, label, {
  onSelect, highlightLatest, inspect, zeroBaseline, asRate = false,
  rateAxisDigits = 3, axisUnit = asRate ? 'Rate' : 'USDT',
} = {}) {
  return drawChart(el(id), rows, {
    label, onSelect, highlightLatest, inspect, zeroBaseline, axisUnit,
    showEndPoint: id === 'live-chart',
    axisValue: asRate ? (value) => pct(value, rateAxisDigits) : (value) => format(value),
    chartValue: asRate ? (value) => pct(value, 5, true) : money,
    minPadding: asRate ? 0.00001 : 1,
  });
}
let samples = [];
let activeSnapshot = null;
let latestSnapshot = null;
let fundingRows;
function renderFunding(amount, equity, stamp, contracts = []) {
  const share = Number.isFinite(amount) && equity > 0 ? amount / equity : null;
  text('funding', pct(share, 5, true));
  quoteValue('funding-amount', amount, true, 4);
  const time = stamp ? new Date(stamp).toISOString().slice(11, 19).replace(/:00$/, '') : '';
  const direction = amount > 0 ? 'Receive' : 'Pay';
  const cash = value => money(Math.abs(value), false, Math.abs(value) < 0.01 ? 4 : 2);
  const message = !Number.isFinite(amount) ? 'Estimate unavailable'
    : amount === 0 ? 'No expected cash flow' : `${direction} ${cash(amount)} at ${time} UTC`;
  text('funding-direction', message);

  const driver = Number.isFinite(amount) && amount !== 0 ? contracts.filter(row =>
    row.next_funding_time === stamp && Number.isFinite(row.expected_funding) && row.expected_funding * amount > 0
  ).sort((a, b) => Math.abs(b.expected_funding) - Math.abs(a.expected_funding))[0] : null;
  el('funding-driver').hidden = !driver;
  if (driver) {
    text('funding-driver', `Largest ${amount < 0 ? 'debit' : 'credit'} · ${driver.asset} ${cash(driver.expected_funding)}`);
  }
}
function updateCountdown() {
  if (!activeSnapshot) return;
  const now = Date.now();
  for (const node of document.querySelectorAll('[data-settlement-clock]')) {
    node.textContent = remainingTime(Number(node.dataset.settlementClock), now);
  }
  for (const node of document.querySelectorAll('[data-payment-until]')) {
    if (Number(node.dataset.paymentUntil) <= now && node.textContent !== '—') {
      node.textContent = '—';
      delete node.dataset.payment;
    }
  }
  if (view !== 'overview') return;
  const shared = el('overview-settlement');
  const sharedStamp = Number(shared.dataset.stamp);
  if (!shared.hidden && sharedStamp <= now && shared.textContent.startsWith('Next')) {
    shared.textContent = `Settlement passed · ${shared.dataset.label}`;
  }
  const data = activeSnapshot.positions.filter(row => row.instrument === 'PERP');
  const visibleFunding = data.map(row => row.next_funding_time > now ? row.expected_funding : null);
  const settlement = nextSettlement(activeSnapshot, now);
  const combined = el('funding-all-next');
  const staggered = settlement.state === 'scheduled' && settlement.included < settlement.total;
  combined.hidden = !staggered;
  if (staggered) {
    text('funding-all-next', `All ${settlement.total} contracts’ next payments · ${money(activeSnapshot.expected_funding,true,4)}`);
  }
  const signature = JSON.stringify([visibleFunding, data.map(row => row.next_funding_time),
    settlement.amount, settlement.stamp, activeSnapshot.equity]);
  if (signature !== fundingRows) {
    fundingRows = signature;
    for (const [index, cell] of [...document.querySelectorAll('[data-funding-payment]')].entries()) {
      const amount = visibleFunding[index];
      cell.textContent = format(amount, 4, true);
      cell.dataset.payment = amount > 0 ? 'receive' : amount < 0 ? 'pay' : '';
    }
    renderFunding(settlement.amount, activeSnapshot.equity, settlement.stamp, data);
  }
  if (settlement.state === 'unavailable') { text('funding-clock','Settlement time unavailable'); return; }
  if (settlement.state === 'expired') { text('funding-clock','Waiting for the new settlement window'); return; }
  const {stamp, included, total} = settlement;
  const count = included < total ? ` · ${included}/${total} contracts next` : '';
  text('funding-clock', `${clock(stamp,true)} · ${remainingTime(stamp, now)}${count}`);
}
function remainingTime(stamp, now = Date.now()) {
  const remaining = Math.ceil((stamp-now)/1000);
  const parts = [Math.floor(remaining/3600), Math.floor(remaining/60)%60, remaining%60];
  return stamp <= now ? 'Awaiting next settlement time' : parts.map(n=>String(n).padStart(2,'0')).join(':');
}
function renderOverview(snapshot) {
  text('exposure',pct(snapshot.delta_ratio,4,true));
  text('exposure-amount', money(snapshot.net_delta, true));
  quoteValue('equity',snapshot.equity);
  text('equity-baseline', `Starting capital ${money(snapshot.starting_equity)}`);
  quoteValue('pnl',snapshot.unrealised_pnl,true);
  const largest = snapshot.asset_deltas.filter(row=>row.delta!=null).sort((a,b)=>Math.abs(b.delta) - Math.abs(a.delta))[0];
  text('largest-exposure',largest ? `Largest exposure · ${largest.asset} ${money(largest.delta,true)}` : 'Asset exposures unavailable');
  const perpetual = snapshot.positions.filter(row=>row.instrument==='PERP');
  const deltas = new Map(snapshot.asset_deltas.map(row=>[row.asset,row.delta]));
  const firstSettlement = perpetual[0]?.next_funding_time;
  const sharedSettlement = clock(firstSettlement) !== '—' &&
    perpetual.every(row => row.next_funding_time === firstSettlement);
  const settlementLabel = el('overview-settlement');
  settlementLabel.hidden = !sharedSettlement;
  if (sharedSettlement) {
    const stamp = new Date(firstSettlement).toISOString();
    const tradingDay = new Date(snapshot.as_of).toISOString().slice(0,10);
    const date = stamp.slice(0,10) === tradingDay ? '' : stamp.slice(0,10) + ' ';
    const time = stamp.slice(11,stamp.slice(17,19) === '00' ? 16 : 19);
    settlementLabel.dataset.stamp = firstSettlement;
    settlementLabel.dataset.label = `${date}${time} UTC`;
    settlementLabel.textContent = `Next settlement · ${settlementLabel.dataset.label}`;
  }
  const rows = perpetual.map(row=>{
    const settlement = clock(row.next_funding_time, true);
    const stamp = settlement === '—' ? 'Unavailable' : `<span>${settlement.slice(0,10)}</span> <span>${settlement.slice(11)}</span>`;
    const settlementCell = sharedSettlement ? '' : `<td class="audit-settlement">${stamp}</td>`;
    return `<tr><th scope="row"><a href="${assetLink(row.asset)}">${escape(row.asset)}</a></th><td data-funding-payment></td><td class="audit-exposure">${format(deltas.get(row.asset),2,true)}</td><td>${pct(row.funding_rate,5,true)}</td>${settlementCell}</tr>`;
  });
  markup('funding-detail', `<div class="table-wrap"><table aria-label="Funding and net exposure by asset"${sharedSettlement ? ' aria-describedby="overview-settlement"' : ''}><thead><tr><th scope="col">Asset</th><th scope="col">Est. payment (USDT)</th><th scope="col">Net exposure (USDT)</th><th scope="col">Funding rate</th>${sharedSettlement ? '' : '<th scope="col">Settlement</th>'}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`);
  fundingRows = null;
}
let renderedSeries;
let renderedFundingWindows;
let trendMode = 'funding';
let fundingReport;
let fundingReadPending = false;
let fundingReadFailed = false;
let fundingReadTimer;
function renderSettledFunding() {
  const windows = fundingReport?.windows ?? [];
  const complete = windows.filter(window => Number.isFinite(window.net_funding_usdt));
  const count = complete.length;
  const newestEnd = Date.parse(windows.at(-1)?.end_at);
  const cutoff = Math.floor(Date.now() / fundingWindow) * fundingWindow;
  const delayed = fundingReadFailed || (!demo && Number.isFinite(newestEnd) && newestEnd < cutoff);
  const allSettled = fundingReport?.status === 'complete' && count === 21;
  const coverage = !fundingReport ? (fundingReadFailed ? ' · Refresh failed' : '')
    : allSettled ? '' : ` · ${count}/21 complete`;
  const note = el('live-chart-note');
  note.textContent = `Each point covers 8 hours (UTC)${coverage}${delayed && fundingReport ? ' · Refresh delayed' : ''}`;
  note.dataset.gaps = String(delayed || Boolean(fundingReport && !allSettled));

  const equity = activeSnapshot?.equity;
  const equityTime = Date.parse(activeSnapshot?.as_of);
  const hasEquity = Number.isFinite(equity) && equity > 0 && Number.isFinite(equityTime);
  const staleEquity = hasEquity && (lastFailure || !sameQuote(activeSnapshot, latestSnapshot)
    || Date.now() - equityTime > staleAfter);
  const basis = el('trend-basis');
  basis.hidden = !count;
  basis.dataset.stale = String(Boolean(staleEquity));
  basis.textContent = hasEquity
    ? `% of ${staleEquity ? 'last displayed' : 'current'} account equity · ${money(equity)} at ${clock(equityTime,true)}`
    : 'Account equity unavailable for percentage calculation';

  const summary = el('trend-value');
  summary.hidden = !allSettled;
  if (allSettled) {
    const total = complete.reduce((sum, window) => sum + window.net_funding_usdt, 0);
    const share = hasEquity ? total / equity : null;
    const tone = share > 0 ? 'inflow' : share < 0 ? 'outflow' : 'neutral';
    markup('trend-value', `<strong data-amount="${tone}">${pct(share,5,true)}</strong> <span class="trend-amount">${money(total,true)}</span> 7-day simulated net · fixed positions`);
  }
  const windowsKey = JSON.stringify(windows.map(window => [window.start_at, window.end_at, window.net_funding_usdt]));
  let chartMessage;
  if (!count) chartMessage = fundingReport || fundingReadFailed
    ? 'No complete funding window is available.' : 'Loading settled funding…';
  else if (!hasEquity) chartMessage = 'Waiting for a positive account equity snapshot.';
  const series = JSON.stringify([hasEquity ? equity : null, windowsKey, chartMessage]);
  if (series === renderedSeries) return;
  if (chartMessage) {
    markup('live-chart', `<p class="chart-text">${chartMessage}</p>`);
    renderedSeries = series;
    renderedFundingWindows = undefined;
    return;
  }
  const highlight = renderedSeries != null && renderedFundingWindows != null &&
    windowsKey !== renderedFundingWindows && Number.isFinite(windows.at(-1)?.net_funding_usdt);
  renderedSeries = series;
  renderedFundingWindows = windowsKey;
  const points = windows.map(window => {
    const t = Date.parse(window.end_at);
    const amount = window.net_funding_usdt;
    const value = Number.isFinite(amount) ? amount / equity : null;
    const end = windowTime.format(t);
    const start = windowTime.format(Date.parse(window.start_at));
    const payment = Number.isFinite(value) ? `${pct(value,5,true)} · ${money(amount,true,4)}` : 'Window incomplete';
    return {t, value, label:`${end} UTC`, tooltip:`${start} to ${end} UTC · ${payment}`};
  });
  plot('live-chart', points, 'Net settled funding in 8-hour UTC windows as a percentage of the displayed account equity.', {
    highlightLatest:highlight, inspect:true, zeroBaseline:true, asRate:true, rateAxisDigits:5, axisUnit:'Equity %',
  });
}
async function loadSettledFunding() {
  clearTimeout(fundingReadTimer);
  if (document.hidden || fundingReadPending) return;
  fundingReadPending = true;
  try {
    const report = await api.fundingWindows();
    fundingReport = report;
    fundingReadFailed = false;
  } catch {
    fundingReadFailed = true;
  } finally {
    fundingReadPending = false;
    if (trendMode === 'funding') renderSettledFunding();
    const now = Date.now();
    const cutoff = Math.floor(now / fundingWindow) * fundingWindow;
    const current = fundingReport?.status === 'complete' && Date.parse(fundingReport.windows.at(-1)?.end_at) === cutoff;
    if (!document.hidden && (!demo || fundingReadFailed)) {
      const delay = fundingReadFailed ? minute : current ? cutoff + fundingWindow + 3000 - now : 15_000;
      fundingReadTimer = setTimeout(loadSettledFunding, delay);
    }
  }
}
function selectTrend(mode) {
  if (mode === trendMode) return;
  trendMode = mode;
  renderedSeries = undefined;
  if (mode === 'equity') {
    text('trend-title', 'Live account equity');
    renderSessionChart();
  } else {
    text('trend-title', 'Settled funding · Last 7 days');
    renderSettledFunding();
  }
  const control = el('trend-mode');
  control.dataset.selection = mode === 'equity' ? '1' : '0';
  for (const button of control.querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.trend === mode));
  reveal(el('live-chart'));
}
function renderSessionChart() {
  if (trendMode !== 'equity') return;
  el('trend-basis').hidden = true;
  const observations = samples.filter(row=>Number.isFinite(row.value));
  const count = observations.length;
  const missing = samples.length - count;
  const coverage = count ? 'Server session · 1-minute samples' : 'Waiting for a complete minute sample';
  text('live-chart-note', missing ? `${count} of ${samples.length} min sampled · ${missing} missing` : coverage);
  el('live-chart-note').dataset.gaps = String(missing > 0);
  const change = el('trend-value');
  change.hidden = !count;
  if (count) {
    const first = observations[0];
    const last = observations.at(-1);
    const delta = last.value - first.value;
    markup('trend-value', count === 1
      ? `<strong>${money(last.value)}</strong> at ${clock(last.t)}`
      : `<strong>${money(delta,true)}</strong> since ${clock(first.t)} <span>·</span> Last sample ${clock(last.t)}`);
  }
  const series = JSON.stringify(samples);
  if (series === renderedSeries) return;
  const highlightLatest = renderedSeries != null && count >= 2 && samples.at(-1)?.value != null;
  renderedSeries = series;
  plot('live-chart', samples.map(row=>({...row,label:clock(row.t)})), 'Live account equity', {highlightLatest, inspect:true});
}
let picked = new URLSearchParams(location.search).get('asset');
if (!assetOptions.includes(picked)) picked = null;
function applyAsset(asset) {
  if (picked === asset) return;
  picked = asset;
  const link = new URL(location.href);
  link.pathname = '/positions';
  if (asset) link.searchParams.set('asset',asset);
  else link.searchParams.delete('asset');
  history.replaceState(null,'',link);
  if (activeSnapshot) {
    renderPositions(activeSnapshot, true);
    reveal(el('position-rows'));
    reveal(el('position-cards'));
  }
}
function positionDetails(row) {
  const cash = row.asset === 'USDT';
  const amount = row.next_funding_time > Date.now() ? row.expected_funding : null;
  const paymentDirection = amount > 0 ? 'receive' : amount < 0 ? 'pay' : '';
  return {
    leg: cash ? 'cash' : row.instrument.toLowerCase(),
    type: cash ? 'Cash' : row.instrument === 'SPOT' ? 'Spot' : 'Perp',
    side: cash ? 'Held' : row.side === 'LONG' ? 'Long' : 'Short',
    cells: [
      ['Quantity', format(row.quantity), 'secondary'],
      ['Entry price', quote(row,'entry_price_usdt'), 'secondary'],
      ['Current price', quote(row,'current_price'), 'primary'],
      ['Value', format(row.value), 'primary'],
      ['Unrealized P&L', format(row.pnl,2,true), row.pnl > 0 ? 'profit' : row.pnl < 0 ? 'loss' : 'secondary'],
      ['Funding rate', pct(row.funding_rate,5,true), 'rate'],
    ].map(([label,value,tone]) => ({label,value,tone:value === '—' ? 'secondary' : tone})),
    payment: `<span data-payment-until="${row.next_funding_time ?? ''}" data-payment="${paymentDirection}">${format(amount,4,true)}</span>`,
    settlement: Number.isFinite(row.next_funding_time)
      ? `${clock(row.next_funding_time)} <span class="table-note" data-settlement-clock="${row.next_funding_time}">${remainingTime(row.next_funding_time)}</span>` : '—',
  };
}
function renderAllocations(snapshot) {
  markup('allocations',[['SPOT','Spot Notional Including Cash'],['PERP','Absolute Perp Notional']].map(([kind,label])=>{
    const legs = assetOptions.map(asset=>snapshot.positions.find(row=>row.instrument===kind && row.asset===asset)).filter(Boolean);
    const sum = legs.reduce((amount,row)=>amount+row.value,0);
    if (legs.some(row=>row.value==null) || !(sum > 0)) return `<article class="surface"><h2>${label}</h2><p class="notice">Allocation unavailable.</p></article>`;
    let start=0;
    const slices = legs.map(row=>{
      const previous=start;
      start+=row.value/sum*100;
      const angle = (previous + start) / 200 * 2 * Math.PI;
      const liftX = (Math.sin(angle) * 5).toFixed(2);
      const liftY = (-Math.cos(angle) * 5).toFixed(2);
      return `<span class="donut-slice" data-slice="${row.asset}" style="--asset-color:${colors[row.asset]};--start:${previous}%;--end:${start}%;--lift-x:${liftX}px;--lift-y:${liftY}px"></span>`;
    });
    const legend = legs.map(row=>`<button type="button" data-asset="${row.asset}" aria-pressed="false" style="--asset-color:${colors[row.asset]}"><span><i class="swatch" aria-hidden="true"></i>${row.asset}</span><strong>${pct(row.value/sum,1)}</strong></button>`).join('');
    return `<article class="surface" data-allocation-kind="${kind}"><div class="section-head"><div><h2>${label}</h2><p>${money(sum)}</p></div></div><div class="allocation"><div class="donut" aria-hidden="true">${slices.join('')}<span class="donut-label"></span></div><div class="allocation-legend">${legend}</div></div></article>`;
  }).join(''));
}
function syncAllocationSelection() {
  for (const panel of el('allocations').querySelectorAll('[data-allocation-kind]')) {
    const ring = panel.querySelector('.donut');
    const slices = [...ring.querySelectorAll('[data-slice]')];
    const selected = slices.find(slice=>slice.dataset.slice===picked);
    ring.classList.toggle('has-selection', Boolean(selected));
    for (const slice of slices) slice.classList.toggle('is-selected', slice===selected);
    ring.querySelector('.donut-label').textContent = selected?.dataset.slice ?? '';
    for (const button of panel.querySelectorAll('button[data-asset]')) {
      button.setAttribute('aria-pressed', String(button.dataset.asset===picked));
    }
  }
}
function renderPositions(snapshot, keepCharts = false) {
  const focus = document.activeElement;
  const group = focus?.closest('[data-allocation-kind], #asset-filter');
  const filterArea = group?.id === 'asset-filter' ? '#asset-filter' : group?.dataset.allocationKind ? `[data-allocation-kind="${group.dataset.allocationKind}"]` : '';
  const focusedAsset = focus?.dataset.asset;
  const list = snapshot.positions.filter(row=>!picked || row.asset===picked);
  text('position-count', `${list.length} ${list.length === 1 ? 'position' : 'positions'}`);
  markup('asset-filter',[null,...assetOptions].map(asset=>`<button type="button" data-asset="${asset ?? ''}" aria-pressed="${asset===picked}">${asset ?? 'All'}</button>`).join(''));
  const entries = list.map(row => ({row, ...positionDetails(row)}));
  const ledger = el('position-rows');
  const mobile = el('position-cards');
  for (const node of [ledger, mobile]) {
    node.classList.toggle('is-filtered', Boolean(picked));
    node.style.setProperty('--asset-color', colors[picked] ?? '');
  }
  markup('position-rows', entries.map(({row,leg,type,side,cells,payment,settlement}) => `
    <tr data-leg="${leg}">
      <th scope="row">${escape(row.symbol)}</th>
      <td><span class="position-instrument">${type}</span></td>
      <td class="position-side" data-side="${side}">${side}</td>
      ${cells.map(({value,tone}) => `<td data-value-tone="${tone}">${escape(value)}</td>`).join('')}
      <td>${payment}</td><td>${settlement}</td>
    </tr>`).join(''));
  const cards = [...new Set(list.map(row=>row.asset))].map(asset => {
    const legs = entries.filter(entry=>entry.row.asset===asset);
    return `<section class="position-group" aria-label="${escape(asset)}"><h3>${escape(asset)}</h3><div class="position-legs">${legs.map(({row,leg,type,side,cells,payment,settlement}) => `
      <article class="position-leg" data-leg="${leg}">
        <h4><span class="position-instrument">${type}</span> <span class="position-side" data-side="${side}">${side}</span> <span class="position-symbol">${escape(row.symbol)}</span></h4>
        <dl>
          ${cells.map(({label,value,tone}) => `<div><dt>${escape(label)}</dt><dd data-value-tone="${tone}">${escape(value)}</dd></div>`).join('')}
          <div><dt>Est. payment</dt><dd>${payment}</dd></div>
          <div class="leg-settlement"><dt>Settlement / countdown</dt><dd>${settlement}</dd></div>
        </dl>
      </article>`).join('')}</div></section>`;
  });
  markup('position-cards', cards.join(''));
  if (!keepCharts) renderAllocations(snapshot);
  syncAllocationSelection();
  const delta = snapshot.asset_deltas.find(row=>row.asset===picked);
  const pair = list.find(row=>row.instrument==='PERP');
  el('asset-breakdown').hidden = !delta;
  if (delta) {
    markup('asset-breakdown', `<dl><div><dt>${escape(picked)} net exposure</dt><dd>${money(delta.delta,true)}</dd></div><div><dt>Estimated next payment</dt><dd><span id="asset-payment"></span></dd></div></dl>`);
    const payment = el('asset-payment');
    payment.dataset.paymentUntil = pair?.next_funding_time ?? '';
    const amount = pair?.next_funding_time > Date.now() ? pair.expected_funding : null;
    payment.dataset.payment = amount > 0 ? 'receive' : amount < 0 ? 'pay' : '';
    payment.textContent = money(amount,true);
  }
  if (filterArea && focusedAsset != null) document.querySelector(`${filterArea} button[data-asset="${focusedAsset}"]`)?.focus({preventScroll:true});
}
if (view === 'positions') {
  el('main').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-asset]');
    if (button) applyAsset(button.dataset.asset || null);
  });
}
function renderMarketData(snapshot = latestSnapshot) {
  const reasons = {
    timeout:'Request timed out', network_error:'Network error', http_error:'HTTP request failed',
    exchange_rejected:'Exchange rejected the request', invalid_response:'Invalid response format',
    missing_exchange_time:'Response time missing', invalid_exchange_time:'Response time invalid',
    stale_exchange_time:'Bybit response is too old', future_exchange_time:'Bybit response time is too far ahead',
    exchange_time_skew:'Quotes arrived too far apart',
    monitor_error:'Internal quote collection failed',
    stream_stale:'Stream quote is too old', stream_disconnected:'Stream disconnected',
    stream_snapshot_incomplete:'Stream snapshot incomplete', stream_funding_unavailable:'Funding fields unavailable',
  };
  const time = el('feed-updated');
  const shownAt = activeSnapshot?.as_of;
  const stale = shownAt && Date.now() - Date.parse(shownAt) > staleAfter;
  let label = 'Waiting for quotes';
  if (lastFailure) label = 'Refresh failed';
  else if (stale) label = 'Stale quotes';
  else if (snapshot?.status === 'ready') {
    label = demo ? 'Simulated' : 'Up to date';
  } else if (snapshot) label = snapshot.status === 'partial' ? 'Partial quotes' : 'Quotes unavailable';
  const state = el('feed-state');
  state.textContent = label;
  state.dataset.tone = lastFailure || stale || snapshot?.status !== 'ready' ||
    ['alerting', 'stale', 'unavailable'].includes(snapshot?.health?.state) ? 'warning' : 'normal';
  time.hidden = !shownAt;
  if (shownAt) {
    time.dateTime = shownAt;
    const lastUsable = lastFailure || stale || !sameQuote(activeSnapshot, snapshot);
    time.textContent = `${lastUsable ? 'Last usable' : 'Updated'} ${clock(shownAt)}`;
  }
  const affected = new Map();
  const details = [];
  for (const issue of snapshot?.issues ?? []) {
    const match = issue.match(/^([A-Z0-9]+) (SPOT|PERP): (.+)$/);
    if (!match) {
      details.push(issue);
      continue;
    }
    const [, symbol, category, reason] = match;
    const label = `${category === 'PERP' ? 'Perp' : 'Spot'} ${reason}`;
    if (!affected.has(label)) affected.set(label, []);
    affected.get(label).push(symbol);
  }
  for (const [label, symbols] of affected) details.push(`${label} (${symbols.join(', ')})`);
  if (snapshot?.transport === 'rest') for (const [name,stream] of Object.entries(snapshot.stream ?? {})) {
    if (stream.reason) details.push(`${name === 'spot' ? 'Spot' : 'Perp'} WebSocket: ${reasons[stream.reason] ?? stream.reason.replaceAll('_',' ')}`);
  }
  if (snapshot?.health?.state === 'alerting') details.unshift(`Data alert · incomplete quotes since ${clock(snapshot.health.since,true)}`);
  if (['stale', 'unavailable'].includes(snapshot?.health?.state)) details.push(healthLabel[snapshot.health.state]);
  if (lastFailure) details.unshift(lastFailure);
  markup('issues',details.length ? `<div class="notice"><h3>Market data issues</h3><ul>${details.map(issue=>`<li>${escape(issue)}</li>`).join('')}</ul></div>` : '');
}

let report;
let historyDays=30;
let drawHistory;
let selectedRows=[];
let selectedHistoryIndex=-1;
function selectHistory(index) {
  const day=selectedRows[index];
  if (!day || index === selectedHistoryIndex) return;
  selectedHistoryIndex=index;
  el('history-day').value=String(index);
  const dateLabel = index === 0 ? `${day.date} 00:00 UTC · Window start` : `${day.date} UTC close`;
  const cashLabel = index === 0 ? 'Simulated opening cash' : 'Simulated cash';
  const cash = Number.isFinite(report.initial_cash) && Number.isFinite(day.cumulative_funding)
    ? report.initial_cash + day.cumulative_funding : null;
  const funding = index === 0 ? '' : `<span>Daily funding <b>${money(day.daily_funding,true)}</b></span>`;
  markup('history-selection', `<strong>${escape(dateLabel)}</strong><span>Equity <b>${money(day.equity)}</b></span><span title="Starting USDT plus cumulative simulated funding since ${escape(report.window.start_date)} UTC">${cashLabel} <b>${money(cash)}</b></span>${funding}<span>Drawdown <b>${pct(day.periodDrawdown,4,true)}</b></span>`);
  drawHistory?.(index);
}
function strategyStats(openingEquity, rows, maxDrawdown) {
  const equities = [openingEquity, ...rows.map(day=>day.equity)];
  const count = rows.length;
  if (!equities.every(equity=>Number.isFinite(equity) && equity > 0) || !count) return {};
  const dailyReturns = rows.map((day,i)=>day.equity / equities[i] - 1);
  const mean = dailyReturns.reduce((sum,value)=>sum + value,0) / count;
  const volatility = count > 1
    ? Math.sqrt(dailyReturns.reduce((sum,value)=>sum + (value - mean) ** 2,0) / (count - 1))
    : null;
  const downside = Math.sqrt(dailyReturns.reduce((sum,value)=>sum + Math.min(0,value) ** 2,0) / count);
  const growth = equities.at(-1) / openingEquity;
  const annualized = Math.expm1(Math.log(growth) * 365 / count);
  return {
    periodReturn:growth - 1,
    annualizedReturn:annualized,
    volatility,
    sharpe:volatility > 0 ? mean * Math.sqrt(365) / volatility : null,
    sortino:downside > 0 ? mean * Math.sqrt(365) / downside : null,
    calmar:maxDrawdown > 0 ? annualized / maxDrawdown : null,
    hasDownside:downside > 0,
  };
}
function renderHistory() {
  const offset=historyDays === 'all' ? 0 : Math.max(0,report.series.length-historyDays);
  const first=offset ? report.series[offset-1] : report.baseline;
  let peak=first.equity;
  const rows=report.series.slice(offset).map(day=>{
    peak=Math.max(peak,day.equity);
    return {...day,periodDrawdown:peak>0 ? day.equity/peak-1 : null};
  });
  selectedRows=[{...first,date:rows[0].date,periodDrawdown:0}, ...rows];
  selectedHistoryIndex=-1;
  const end=rows.at(-1);
  const funding=end.cumulative_funding-first.cumulative_funding;
  const paidDays=rows.filter(day=>day.daily_funding<0).length;
  const worstFunding=Math.min(...rows.map(day=>day.daily_funding));
  const maxDrawdown = first.equity > 0 ? Math.max(0, ...rows.map(day=>-day.periodDrawdown)) : null;
  const stats = strategyStats(first.equity, rows, maxDrawdown);
  text('history-window',`${rows[0].date} to ${end.date} UTC · ${rows.length} ${rows.length === 1 ? 'day' : 'days'} · Before costs`);
  const ratios = [
    ['Sharpe', format(stats.sharpe), stats.volatility === 0 ? 'No return variation' : ''],
    ['Sortino', format(stats.sortino), stats.hasDownside === false ? 'No downside days' : ''],
    ['Window Calmar', format(stats.calmar), maxDrawdown === 0 ? 'No drawdown' : ''],
  ];
  markup('history-metrics',metrics([['Annualized return',pct(stats.annualizedReturn,2,true)],['Period return',pct(stats.periodReturn,4,true)],['Simulated funding',money(funding,true)],['Net funding paid days',`${paidDays} / ${rows.length}`],['Worst funding day',money(worstFunding,true)],['Maximum drawdown',pct(maxDrawdown)],['Daily volatility',pct(stats.volatility)], ...ratios]));
  const chartRows=selectedRows.map((day,index)=>({value:day.equity,t:Date.parse(day.as_of),label:index===0 ? `${day.date} open` : day.date}));
  drawHistory=plot('history-chart',chartRows,'Simulated equity for fixed positions. Select a point or choose a valuation date below the chart.', {onSelect:selectHistory});
  el('history-day').replaceChildren(...selectedRows.map((day,index)=>new Option(index===0 ? `${day.date} open` : day.date, String(index))));
  el('history-day').disabled=false;
  selectHistory(selectedRows.length-1);
  reveal(el('history-chart').querySelector('.chart-frame'));
}
function showHistoryStatus(ingestion) {
  if (ingestion?.state === 'unavailable') {
    status(report ? 'History unavailable · Showing the last report' : 'Historical archive unavailable', true);
    return;
  }
  const coverage = ingestion?.covered_through ? ` · Through ${ingestion.covered_through} UTC` : '';
  const suffix = [
    ingestion?.state === 'error' ? 'Backfill delayed' : ingestion?.state === 'updating' ? 'Backfilling' : '',
    ingestion?.warning ? 'Using the verified seed archive' : '',
  ].filter(Boolean);
  status(`Historical simulation${coverage}${suffix.length ? ` · ${suffix.join(' · ')}` : ''}`,
    ingestion?.state === 'error' || Boolean(ingestion?.warning));
}
const latestHistoryRequest = latestRequest();
async function getHistory() {
  const isCurrent = latestHistoryRequest();
  try {
    const next = await api.history();
    if (!isCurrent()) return;
    if (next.status!=='available') throw Error(next.issues?.join('; ')||'Historical report unavailable');
    const advanced = report && report.window.end_date !== next.window.end_date;
    report = next;
    renderHistory();
    showHistoryStatus(report.ingestion);
    if (advanced) {
      fundingArchives.clear();
      latestFundingArchiveRequest();
      if (researchMode === 'funding') loadFundingArchive();
      if (researchMode === 'backtest') prepareBacktest();
    }
  } catch (error) {
    if (!isCurrent()) return;
    if (report) status('History refresh failed · Showing the last report', true);
    else {
      status('Historical report unavailable',true);
      text('history-window',error.message);
      markup('history-chart','<p class="chart-text">Daily equity unavailable.</p>');
    }
  }
}

const latestCoverageRequest = latestRequest();
async function checkHistoryCoverage() {
  if (document.hidden) return;
  const isCurrent = latestCoverageRequest();
  try {
    const ingestion = await api.historyStatus();
    if (!isCurrent()) return;
    if (ingestion.state === 'unavailable') {
      showHistoryStatus(ingestion);
      return;
    }
    if (!report || ingestion.covered_through > report.window.end_date) await getHistory();
    else showHistoryStatus(ingestion);
    if (researchMode === 'backtest') prepareBacktest();
  } catch {
    if (isCurrent() && report) status('History status unavailable · Showing the last report', true);
  }
}

const fundingArchives = new Map();
const latestFundingArchiveRequest = latestRequest();
let renderedFundingSymbol;
async function loadFundingArchive() {
  const isCurrent = latestFundingArchiveRequest();
  const symbol = el('funding-symbol').value;
  text('funding-history-window','Loading funding rates');
  try {
    if (!fundingArchives.has(symbol)) {
      const source = await api.fundingHistory(symbol);
      if (!isCurrent() || symbol !== el('funding-symbol').value) return;
      fundingArchives.set(symbol, source);
    }
    if (symbol !== el('funding-symbol').value) return;
    const source = fundingArchives.get(symbol);
    const count = el('funding-days').value;
    const rows = count === 'all' ? source.days : source.days.slice(-Number(count));
    const bySettlement = el('funding-granularity').value==='settlement';
    const values = rows.map(day=>day.rate);
    const settlements = rows.reduce((total,day)=>total+day.settlements,0);
    text('funding-history-window',`${rows[0].date} to ${rows.at(-1).date} UTC · ${symbol}`);
    markup('funding-history-metrics',metrics([
      ['Observed days',format(rows.length,0)], ['Published settlements',format(settlements,0)],
      ['Negative days',`${values.filter(value=>value<0).length} / ${rows.length}`],
      ['Mean daily rate',pct(values.reduce((total,value)=>total+value,0)/rows.length,5,true)],
      ['Lowest daily rate',pct(Math.min(...values),5,true)], ['Highest daily rate',pct(Math.max(...values),5,true)],
    ]));
    const points = bySettlement
      ? source.events.filter(event=>event.as_of.slice(0,10)>=rows[0].date).map(event=>({value:event.rate,t:Date.parse(event.as_of),label:event.as_of.slice(0,16).replace('T',' ')}))
      : rows.map(day=>({value:day.rate,t:Date.parse(`${day.date}T00:00:00Z`),label:day.date,settlements:day.settlements}));
    const picker = el('funding-history-day');
    const select = (index) => {
      const point = points[index];
      picker.value = String(index);
      const count = bySettlement ? '' : `<span>Settlements <b>${point.settlements}</b></span>`;
      markup('funding-history-selection', `<strong>${escape(point.label)} UTC</strong><span>${bySettlement ? 'Settled rate' : 'Daily rate'} <b>${pct(point.value,5,true)}</b></span>${count}`);
      moveCursor(index);
    };
    const moveCursor = plot('funding-history-chart',points,`${symbol} ${bySettlement ? 'settled funding rates' : 'daily sum of settled funding rates'}`, {onSelect:select, asRate:true});
    picker.replaceChildren(...points.map((point,index)=>new Option(point.label,String(index))));
    picker.onchange = () => select(Number(picker.value));
    picker.disabled = false;
    select(points.length-1);
    renderedFundingSymbol = symbol;
  } catch (error) {
    if (!isCurrent() || symbol !== el('funding-symbol').value) return;
    if (renderedFundingSymbol === symbol) {
      text('funding-history-window', `${error.message} · Showing the last report for ${symbol}`);
      return;
    }
    text('funding-history-window',error.message);
    for (const id of ['funding-history-chart','funding-history-selection','funding-history-metrics']) markup(id,'');
    el('funding-history-day').disabled = true;
    renderedFundingSymbol = undefined;
  }
}

let backtestBounds;
let backtestBoundsPending = false;
let displayedBacktestParams;
const latestBacktestRequest = latestRequest();
async function prepareBacktest() {
  if (backtestBoundsPending || (backtestBounds && backtestBounds.end === report?.window.end_date)) return;
  backtestBoundsPending = true;
  const form = el('backtest-form');
  try {
    const bounds = await api.backtestBounds();
    const dayMs = 24 * 60 * minute;
    const first = Date.parse(`${bounds.start}T00:00:00Z`);
    const last = Date.parse(`${bounds.end}T00:00:00Z`);
    const dates = [];
    for (let t = last; t >= first; t -= dayMs) dates.push(new Date(t).toISOString().slice(0, 10));
    el('backtest-date-options').replaceChildren(...dates.map(date => new Option(date, date)));
    if (!backtestBounds) {
      form.elements.start.value = dates[29] ?? bounds.start;
      form.elements.end.value = bounds.end;
      el('run-backtest').disabled = false;
    }
    backtestBounds = bounds;
    text('backtest-window',`${bounds.start} to ${bounds.end} UTC · Dates include the last close`);
  } catch (error) {
    text('backtest-window',error.message);
  } finally {
    backtestBoundsPending = false;
  }
}

function validateBacktestDates(params, bounds) {
  for (const [name, label] of [['start', 'Start'], ['end', 'End']]) {
    const value = params.get(name).trim();
    params.set(name, value);
    const date = new Date(`${value}T00:00:00Z`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(date.valueOf()) || date.toISOString().slice(0, 10) !== value) {
      return [name, `${label} must be a valid UTC date in YYYY-MM-DD format.`];
    }
    if (value < bounds.start || value > bounds.end) {
      return [name, `${label} must be from ${bounds.start} to ${bounds.end} UTC.`];
    }
  }
  return params.get('start') > params.get('end') ? ['end', 'End must be on or after start.'] : null;
}

function renderBacktest(result, selectedIndex) {
  const summary = result.metrics;
  const netFundingLabel = summary.total_funding < 0 ? 'Net funding paid'
    : summary.total_funding > 0 ? 'Net funding received' : 'Net funding';
  const breakEven = summary.break_even_slippage_bps;
  const breakEvenMetric = Number.isFinite(breakEven)
    ? ['Break-even slippage', `${format(breakEven, 3)} bps/leg`]
    : ['Break-even slippage', '—', summary.turnover_notional ? 'Loss at 0 bps' : 'No trades'];
  markup('backtest-attribution',metrics([
    [netFundingLabel,money(summary.total_funding,true)],
    ['Trading fees',money(-summary.total_fees,true)],
    ['Modeled slippage',money(-summary.total_slippage,true)],
    ['Price P&L',money(result.series.at(-1).mark_to_market_pnl,true)],
  ]));
  text('backtest-fees', `Trading fees: ${money(summary.entry_fees)} at entry and ${money(summary.exit_fees)} at exit. Slippage: ${money(summary.entry_slippage)} at entry and ${money(summary.exit_slippage)} at exit.`);
  markup('backtest-metrics',metrics([
    ['Net return',pct(summary.total_return,4,true)], ['Net P&L',money(summary.net_pnl,true)],
    ['Ending equity',money(summary.ending_equity)], ['Maximum drawdown',pct(summary.max_drawdown)],
    ['Cash after entry',money(summary.post_entry_cash)], breakEvenMetric,
  ]));
  const rows = [result.baseline,...result.series];
  const picker = el('backtest-day');
  const select = (index) => {
    const item = rows[index];
    picker.value = String(index);
    const date = index===0 ? `${item.date} open` : `${item.date} close`;
    const cashLabel = index===rows.length-1 ? 'Cash after exit' : 'Cash';
    markup('backtest-selection', `<strong>${escape(date)} UTC</strong><span>Equity <b>${money(item.equity)}</b></span><span>${cashLabel} <b>${money(item.cash)}</b></span>${index===0 ? '' : `<span>Daily funding <b>${money(item.daily_funding,true)}</b></span>`}`);
    moveCursor(index);
  };
  el('backtest-result').hidden = false;
  const chartRows=rows.map((day,index)=>({value:day.equity,t:Date.parse(day.as_of),label:index===0 ? `${day.date} open` : day.date}));
  const moveCursor = plot('backtest-chart',chartRows,'Portfolio equity after costs', {onSelect:select});
  picker.replaceChildren(...chartRows.map((day,index)=>new Option(day.label,String(index))));
  picker.onchange = () => select(Number(picker.value));
  picker.disabled = false;
  select(Math.min(selectedIndex ?? rows.length - 1, rows.length - 1));
  const holdings = result.entries.map(row=>`<article><h4>${escape(row.asset)} <span>${pct(row.weight_pct/100,2)}</span></h4><p>${format(row.quantity,6)} per leg</p><p>Spot ${format(row.spot_price,3)} · Perp ${format(row.perp_mark,3)}</p></article>`);
  markup('backtest-entries', holdings.length ? holdings.join('') : '<p>All starting capital held in USDT.</p>');
}

function backtestInputSignature(params) {
  const inputs = new URLSearchParams(params);
  inputs.delete('slippage_bps');
  return querySignature(inputs);
}

function sameBacktestInputs(params) {
  return displayedBacktestParams && backtestInputSignature(displayedBacktestParams) === backtestInputSignature(params);
}

function displayedBacktestNote() {
  return displayedBacktestParams ? ` Showing the ${displayedBacktestParams.get('slippage_bps')} bps result.` : '';
}

async function runBacktest(form) {
  if (!backtestBounds) return;
  const run = el('run-backtest');
  const message = el('backtest-status');
  const params = new URLSearchParams(new FormData(form));
  const invalidDate = validateBacktestDates(params, backtestBounds);
  if (invalidDate) {
    const [name, error] = invalidDate;
    form.elements[name].setAttribute('aria-invalid', 'true');
    form.elements[name].focus();
    message.dataset.error = 'true';
    message.textContent = `${error}${displayedBacktestNote()}`;
    return;
  }
  const isCurrent = latestBacktestRequest();
  const submitted = querySignature(params);
  run.disabled = true;
  message.dataset.error = 'false';
  message.textContent = `Calculating at ${params.get('slippage_bps')} bps…${displayedBacktestNote()}`;
  const inputsChanged = () => submitted !== querySignature(new FormData(form));
  const rerunMessage = `Inputs changed. Run backtest to apply.${displayedBacktestNote()}`;
  try {
    const result = await api.backtest(params);
    if (!isCurrent()) return;
    if (inputsChanged()) {
      message.textContent = rerunMessage;
      return;
    }
    const selectedIndex = sameBacktestInputs(params) ? Number(el('backtest-day').value) : null;
    renderBacktest(result, selectedIndex);
    displayedBacktestParams = params;
    message.dataset.error = String(result.issues.length > 0);
    const summary = `${result.window.start_date} to ${result.window.end_date} UTC · ${result.window.complete_days} days · ${result.inputs.slippage_bps} bps slippage per leg`;
    message.textContent = [summary, ...result.issues].join(' · ');
  } catch (error) {
    if (!isCurrent()) return;
    if (inputsChanged()) {
      message.textContent = rerunMessage;
      return;
    }
    message.dataset.error = 'true';
    message.textContent = `${error.message}${displayedBacktestNote()}`;
  } finally {
    if (isCurrent()) run.disabled = false;
  }
}

let researchMode = 'portfolio';
function selectResearch(mode) {
  researchMode = mode;
  for (const button of el('research-nav').querySelectorAll('[data-research]')) {
    button.setAttribute('aria-pressed',String(button.dataset.research===mode));
  }
  for (const panel of document.querySelectorAll('[data-research-panel]')) panel.hidden = panel.dataset.researchPanel!==mode;
  const url = new URL(location.href);
  if (mode === 'portfolio') url.searchParams.delete('view');
  else url.searchParams.set('view',mode);
  history.replaceState(null,'',url);
  if (mode === 'funding') loadFundingArchive();
  if (mode === 'backtest') prepareBacktest();
}
let pending=null;
let manualInProgress=false;
let manualQueued=false;
let nextRefreshAt=0;
let refreshTimer;
let lastFailure=null;
let refreshFeedback;
let fastUpdates=false;
try { fastUpdates=sessionStorage.getItem('cmcc-fast-quotes')==='true'; } catch { /* The mode is local to this page. */ }
function renderUpdateMode() {
  const control = el('update-mode').querySelector('.segmented');
  control.dataset.selection = fastUpdates ? '1' : '0';
  for (const button of control.querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String((button.dataset.update === 'fast') === fastUpdates));
  }
}
function refresh(automatic = false) {
  if (pending) {
    if (!automatic && !manualInProgress) manualQueued=true;
    return pending;
  }
  manualInProgress=!automatic;
  const now = Date.now();
  nextRefreshAt = now + (fastUpdates ? fastRefreshInterval : minute);
  pending = fetchAndRender(automatic);
  return pending;
}
function loadQuotes() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(quoteStorage));
    if (!isQuote(saved?.snapshot) || !isQuote(saved.latest ?? saved.snapshot) || !isSamples(saved.samples)) return null;
    return saved;
  } catch { return null; }
}
function saveQuotes() {
  try { sessionStorage.setItem(quoteStorage, JSON.stringify({snapshot:activeSnapshot, latest:latestSnapshot, samples, lastFailure})); }
  catch { /* Quote updates still work if tab storage is unavailable. */ }
}
function displayQuotes(snapshot, observed = snapshot, fromCache = false) {
  const firstPaint = !activeSnapshot || activeSnapshot.status === 'unavailable';
  const sameQuotes = sameQuote(activeSnapshot, snapshot);
  activeSnapshot = snapshot;
  latestSnapshot = observed;
  if (view === 'overview') {
    if (!sameQuotes) renderOverview(snapshot);
    if (trendMode === 'funding') renderSettledFunding();
    else renderSessionChart();
    renderMarketData(observed);
  } else if (!sameQuotes) {
    renderPositions(snapshot);
    if (firstPaint) {
      for (const id of fromCache ? ['main'] : ['allocations','position-rows','position-cards']) reveal(el(id));
    }
  }
  updateCountdown();
  const feed = demo ? 'quotes' : observed.transport === 'websocket' ? 'WebSocket' : observed.stream ? 'REST fallback' : 'REST';
  if (observed.status === 'ready') {
    status(`${sourceName} ${feed} · Updated ${clock(snapshot.as_of)}`);
  } else if (!sameQuote(snapshot, observed)) {
    const missing = Object.entries(observed.sources).filter(([, source]) => source.status !== 'available')
      .map(([category]) => category === 'spot' ? 'Spot' : 'Perpetual');
    const issue = missing.length ? `${missing.join(' and ')} feed unavailable` : 'Incomplete Bybit inputs';
    status(`${observed.health?.state === 'alerting' ? 'Data alert · ' : ''}${issue} · Last equity ${clock(snapshot.as_of)}`, true);
  } else {
    const missing = [];
    if (snapshot.equity == null) missing.push('equity and net exposure');
    if (snapshot.expected_funding == null) missing.push('funding estimate');
    const impact = missing.length ? `Unavailable: ${missing.join(', ')}` : 'Some market inputs are unavailable';
    status(`${sourceName} · ${impact} · Checked ${clock(observed.as_of)}`, true);
  }
}
function markOldQuotes() {
  if (!activeSnapshot || (!lastFailure && !sameQuote(activeSnapshot, latestSnapshot))) return;
  if (!lastFailure && Date.now() - Date.parse(activeSnapshot.as_of) <= staleAfter) return;
  const label = lastFailure ? 'Refresh failed' : 'Stale quotes';
  status(`${label} · Showing quotes as of ${clock(activeSnapshot.as_of)}`, true);
  if (view === 'overview') {
    renderMarketData();
    if (trendMode === 'funding') renderSettledFunding();
  }
}
async function fetchAndRender(automatic) {
  const button = el('refresh');
  const manual = !automatic;
  let feedback = '';
  clearTimeout(refreshFeedback);
  button.disabled=manual;
  button.dataset.state='pending';
  button.setAttribute('aria-busy','true');
  if (manual || !activeSnapshot) text('refresh-label', 'Refreshing…');
  if (activeSnapshot) markOldQuotes();
  else status(`Waiting for ${sourceName} quotes…`);
  try {
    const {samples:minuteSamples, last_complete:lastComplete, ...snapshot} = await api.snapshot();
    const sameObservation = sameQuote(latestSnapshot, snapshot);
    samples = minuteSamples;
    lastFailure = null;
    displayQuotes(displayedQuote(snapshot, lastComplete, activeSnapshot), snapshot);
    markOldQuotes();
    if (manual) {
      const readyLabel = sameObservation ? 'No new quotes' : 'Updated';
      feedback = {ready:readyLabel, partial:'Partial data', unavailable:'Unavailable'}[snapshot.status];
    }
  } catch (error) {
    if (manual) feedback = 'Retry quotes';
    lastFailure = `Quote request failed (${error.message}).`;
    if (activeSnapshot) {
      markOldQuotes();
    } else {
      nextRefreshAt = Date.now() + fastRefreshInterval;
      status(lastFailure,true);
      if (view === 'overview') {
        renderFunding(null, null);
        for (const id of ['exposure', 'exposure-amount', 'equity', 'pnl']) text(id, '—');
        renderMarketData();
      } else {
        text('position-count', 'Current position values unavailable');
      }
    }
  } finally {
    saveQuotes();
    pending=null;
    button.disabled=false;
    button.setAttribute('aria-busy','false');
    button.dataset.state=feedback === 'Updated' ? 'updated' : '';
    text('refresh-label', feedback || 'Refresh quotes');
    if (feedback) refreshFeedback = setTimeout(() => {
      button.dataset.state='';
      text('refresh-label','Refresh quotes');
    }, 1700);
    if (manualQueued) {
      manualQueued=false;
      queueMicrotask(() => refresh());
    } else scheduleRefresh(automatic);
  }
}
function scheduleRefresh(afterAutomaticRead = false) {
  clearTimeout(refreshTimer);
  if (document.hidden || pending) return;
  const now = Date.now();
  const latestSampleTime = afterAutomaticRead ? (samples.at(-1)?.t ?? -Infinity) : undefined;
  const minuteRead = nextMinuteReadAt(now, latestSampleTime, fastRefreshInterval);
  const due = fastUpdates || !activeSnapshot || nextRefreshAt === 0 ? nextRefreshAt : minuteRead;
  refreshTimer = setTimeout(() => refresh(true), Math.max(0, due-now));
}
function restoreQuotes() {
  const saved = loadQuotes();
  if (!saved) return;
  samples = saved.samples ?? [];
  lastFailure = saved.lastFailure;
  displayQuotes(saved.snapshot, saved.latest ?? saved.snapshot, true);
  markOldQuotes();
}
const observations = [];
const readable = (value) => String(value ?? '—').replaceAll('_', ' ');
function showObservation(index) {
  const row = observations[index];
  for (const [i, button] of [...el('diagnostic-log').children].entries()) button.setAttribute('aria-pressed', String(i === index));
  text('diagnostic-selection', row ? `${clock(row.observed_at,true)} · ${readable(row.classification)}` : 'Observation details');
  el('diagnostic-times').hidden = !row;
  if (!row) { text('diagnostic-detail', ''); return; }
  const {snapshot, inputs, sources, ...observation} = row;
  const timeRows = Object.entries({spot: 'Spot', linear: 'Perpetual'}).map(([key, label]) => {
    const source = sources?.[key];
    return `<tr><th scope="row">${label}</th><td>${clock(source?.exchange_time_ms, true)}</td><td>${clock(source?.received_at, true)}</td></tr>`;
  });
  markup('diagnostic-time-rows', timeRows.join(''));
  const {sources: capturedSources, ...valuation} = snapshot;
  text('diagnostic-detail', JSON.stringify({observation, sources, inputs, valuation}, null, 2));
}
const latestDiagnosticsRequest = latestRequest();
async function loadDiagnostics() {
  const isCurrent = latestDiagnosticsRequest();
  const filters = new URLSearchParams({limit:'25'});
  for (const [key, value] of new FormData(el('diagnostic-filter'))) if (value.trim()) filters.set(key, value.trim());
  const params = new URLSearchParams(filters);
  params.delete('limit');
  history.replaceState(null, '', params.size ? `/data?${params}` : '/data');
  text('diagnostic-message', 'Reading local observations…');
  try {
    const result = await api.diagnostics(filters);
    if (!isCurrent()) return;
    observations.splice(0, observations.length, ...result.observations);
    markup('diagnostic-log', observations.map((row,index)=>`<button type="button" data-index="${index}" aria-pressed="false"><strong>${clock(row.observed_at)}</strong><span>${escape(row.alert_event?.type ?? row.classification)}</span><code title="${escape(row.request_id)}">${escape(row.request_id.slice(0,8))}</code></button>`).join(''));
    showObservation(observations.length ? 0 : -1);
    text('diagnostic-message', observations.length ? `${observations.length} latest matches · Select a row to inspect inputs and valuation` : 'No matching observations');
  } catch (error) {
    if (!isCurrent()) return;
    observations.length = 0;
    markup('diagnostic-log', '');
    showObservation(-1);
    text('diagnostic-message', error.message);
  }
}
async function loadQuoteHealth() {
  try {
    const health = await api.health();
    const label = healthLabel[health.state];
    text('data-health', health.classification ? `${label} · ${readable(health.classification)}` : label);
    const lastEvent = health.state === 'alerting' || health.state === 'degraded'
      ? `Incomplete since ${clock(health.since,true)} · ${health.threshold_seconds}s alert threshold`
      : health.state === 'stale' && health.last_observation_at
        ? `Last observation ${clock(health.last_observation_at,true)}` : (health.issues?.[0] ?? '');
    text('data-last-event', lastEvent);
    const eventLinks = [['Last alert', health.last_alert], ['Last recovery', health.last_recovery]]
      .filter(([, event])=>event).map(([name,event])=>`<a href="/data?${new URLSearchParams({request_id:event.request_id})}">${name} · ${clock(event.at,true)}</a>`);
    el('health-event-links').hidden = !eventLinks.length;
    markup('health-event-links', eventLinks.join(' · '));
    const check = health.last_observation_at ? ` · Checked ${clock(health.last_observation_at)}` : '';
    status(`${label}${check}`, health.state !== 'healthy');
  } catch (error) {
    status('Diagnostics unavailable', true);
    text('data-health', 'Diagnostics unavailable');
    text('data-last-event', error.message);
    el('health-event-links').hidden = true;
  }
}

async function loadArchiveHealth() {
  try {
    const ingestion = await api.historyStatus();
    const coverage = ingestion.covered_through ? `Through ${ingestion.covered_through} UTC · ` : '';
    text('data-ingestion', `${coverage}${readable(ingestion.state)}`);
    const target = ingestion.target_utc_day !== ingestion.covered_through ? ingestion.target_utc_day : null;
    text('data-ingestion-detail', [ingestion.warning, ingestion.error].filter(Boolean).join(' · ')
      || (target ? `Pending day ${target} UTC` : ''));
  } catch (error) {
    text('data-ingestion', 'History status unavailable');
    text('data-ingestion-detail', error.message);
  }
}

let quoteHealthPending;
let archiveHealthPending;
function loadHealth() {
  quoteHealthPending ??= loadQuoteHealth().finally(() => { quoteHealthPending = null; });
  archiveHealthPending ??= loadArchiveHealth().finally(() => { archiveHealthPending = null; });
}
if (view==='data') {
  el('refresh').hidden=true;
  el('update-mode').hidden=true;
  const filter = el('diagnostic-filter');
  for (const [key, value] of new URLSearchParams(location.search)) if (filter.elements.namedItem(key) && ['start','end','request_id'].includes(key)) filter.elements.namedItem(key).value = value;
  filter.addEventListener('submit', event => { event.preventDefault(); loadDiagnostics(); loadHealth(); });
  el('diagnostic-log').addEventListener('click', event => {
    const index = event.target.closest('button')?.dataset.index;
    if (index != null) showObservation(Number(index));
  });
  loadDiagnostics();
  loadHealth();
  setInterval(() => { if (!document.hidden) loadHealth(); }, 5000);
} else if (view==='history') {
  el('refresh').hidden=true;
  el('update-mode').hidden=true;
  el('research-nav').addEventListener('click',event=>{
    const mode = event.target.closest('[data-research]')?.dataset.research;
    if (mode) selectResearch(mode);
  });
  for (const id of ['funding-symbol','funding-days','funding-granularity']) el(id).addEventListener('change',loadFundingArchive);
  const backtestForm = el('backtest-form');
  const reportBacktestChanges = () => {
    el('backtest-status').dataset.error = 'false';
    text('backtest-status',`Inputs changed. Run backtest to apply.${displayedBacktestNote()}`);
  };
  backtestForm.addEventListener('submit',event=>{
    event.preventDefault();
    if (!el('run-backtest').disabled) runBacktest(backtestForm);
  });
  backtestForm.addEventListener('input',event=>{
    if (event.target.name === 'slippage_bps') return;
    if (['start', 'end'].includes(event.target.name)) {
      for (const name of ['start', 'end']) event.currentTarget.elements[name].removeAttribute('aria-invalid');
    }
    if (displayedBacktestParams) reportBacktestChanges();
  });
  el('backtest-slippage').addEventListener('change',()=>{
    if (sameBacktestInputs(new FormData(backtestForm))) runBacktest(backtestForm);
    else if (displayedBacktestParams) reportBacktestChanges();
  });
  el('history-day').addEventListener('change',event=>{ if (selectedRows.length) selectHistory(Number(event.target.value)); });
  el('history-range').addEventListener('click',event=>{
    const button=event.target.closest('[data-days]');
    if (!button || !report || report.status!=='available') return;
    historyDays=button.dataset.days==='all' ? 'all' : Number(button.dataset.days);
    for (const candidate of el('history-range').querySelectorAll('button')) candidate.setAttribute('aria-pressed',String(candidate===button));
    renderHistory();
  });
  el('history-jump').addEventListener('click',event=>{
    const jump=event.target.closest('[data-jump]')?.dataset.jump;
    if (!jump || selectedRows.length < 2) return;
    const field = jump === 'drawdown' ? 'periodDrawdown' : 'daily_funding';
    const index=selectedRows.reduce((best,day,i)=>{
      if (i === 0) return best;
      return (jump === 'funding' ? day[field] > selectedRows[best][field] : day[field] < selectedRows[best][field]) ? i : best;
    },1);
    selectHistory(index);
  });
  getHistory();
  setInterval(checkHistoryCoverage, minute);
  document.addEventListener('visibilitychange', checkHistoryCoverage);
  const initialMode = new URLSearchParams(location.search).get('view');
  if (initialMode==='funding' || initialMode==='backtest') selectResearch(initialMode);
} else {
  renderUpdateMode();
  el('update-mode').addEventListener('click',event=>{
    const next = event.target.closest('[data-update]')?.dataset.update;
    if (!next || (next === 'fast') === fastUpdates) return;
    fastUpdates = next === 'fast';
    try { sessionStorage.setItem('cmcc-fast-quotes',String(fastUpdates)); } catch { /* The mode is still available in this page. */ }
    renderUpdateMode();
    nextRefreshAt=0;
    scheduleRefresh();
  });
  el('refresh').addEventListener('click',()=>refresh());
  setInterval(updateCountdown,1000);
  restoreQuotes();
  if (view === 'overview') {
    el('trend-mode').addEventListener('click', event => {
      const mode = event.target.closest('[data-trend]')?.dataset.trend;
      if (mode) selectTrend(mode);
    });
    renderSettledFunding();
    loadSettledFunding();
  }
  scheduleRefresh();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      markOldQuotes();
      const age = Date.now() - Date.parse(latestSnapshot?.as_of);
      if (!latestSnapshot || age >= (fastUpdates ? fastRefreshInterval : minute)) nextRefreshAt=0;
    }
    if (view === 'overview') loadSettledFunding();
    scheduleRefresh();
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted || pending) return;
    restoreQuotes();
    nextRefreshAt=0;
    if (view === 'overview') loadSettledFunding();
    scheduleRefresh();
  });
}
