const svgNS = 'http://www.w3.org/2000/svg';

function svgNode(name, fields) {
  const node = document.createElementNS(svgNS, name);
  for (const [key, value] of Object.entries(fields)) node.setAttribute(key, value);
  return node;
}
export function drawChart(container, rows, {
  label,
  onSelect,
  highlightLatest = false,
  inspect = false,
  zeroBaseline = false,
  showEndPoint = false,
  axisUnit,
  axisValue,
  chartValue,
  minPadding,
}) {
  const id = container.id;
  const data = rows.filter(row => Number.isFinite(row.value));
  if (!data.length) {
    const empty = document.createElement('p');
    empty.className = 'chart-text';
    empty.textContent = 'Waiting for a complete equity observation.';
    container.replaceChildren(empty);
    return null;
  }
  const width = 720, height = 146, pad = 10;
  const values = data.map(row => row.value);
  if (zeroBaseline) values.push(0);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const margin = Math.max(minPadding, (max - min) * .15);
  const low = min - margin, high = max + margin;
  const floor = rows[0].t, top = rows.at(-1).t;
  const x = (t) => top === floor ? width / 2 : pad + (width - 2 * pad) * (t - floor) / (top - floor);
  const y = (n) => height - pad - (height - 2 * pad) * (n - low) / (high - low);
  const chart = svgNode('svg', {viewBox:`0 0 ${width} ${height}`, preserveAspectRatio:'none', role:'img', 'aria-label':label});
  const area = svgNode('linearGradient', {id:`${id}-fill`, x2:0, y2:1});
  area.append(svgNode('stop', {offset:0, 'stop-color':'currentColor', 'stop-opacity':.16}));
  area.append(svgNode('stop', {offset:1, 'stop-color':'currentColor', 'stop-opacity':0}));
  const definitions = svgNode('defs', {});
  definitions.append(area);
  chart.append(definitions);
  for (const n of [pad, height / 2, height - pad]) chart.append(svgNode('line', {x1:pad, x2:width-pad, y1:n, y2:n}));
  if (zeroBaseline) chart.append(svgNode('line', {class:'zero-line', x1:pad, x2:width-pad, y1:y(0), y2:y(0)}));
  let points = [];
  const flush = () => {
    if (points.length > 1) {
      const bottom = height - pad;
      const left = points[0].split(',')[0];
      const right = points.at(-1).split(',')[0];
      chart.append(svgNode('polygon', {points:`${left},${bottom} ${points.join(' ')} ${right},${bottom}`, fill:`url(#${id}-fill)`}));
      chart.append(svgNode('polyline', {points:points.join(' ')}));
    }
    if (points.length === 1) {
      const [cx,cy] = points[0].split(',');
      chart.append(svgNode('circle', {cx,cy,r:3}));
    }
    points = [];
  };
  for (const row of rows) {
    if (!Number.isFinite(row.value)) flush();
    else points.push(`${x(row.t)},${y(row.value)}`);
  }
  flush();
  const latest = rows.at(-1);
  if (highlightLatest && Number.isFinite(latest.value)) {
    const previous = rows.at(-2);
    if (Number.isFinite(previous?.value)) {
      chart.append(svgNode('line', {class:'latest-segment', x1:x(previous.t), y1:y(previous.value), x2:x(latest.t), y2:y(latest.value)}));
    }
  }
  if (showEndPoint && Number.isFinite(latest.value)) {
    chart.append(svgNode('circle', {class:highlightLatest ? 'end-point latest-point' : 'end-point', cx:x(latest.t), cy:y(latest.value), r:4}));
  }
  const indicator = svgNode('line', {x1:0, x2:0, y1:pad, y2:height-pad, class:'cursor-line', visibility:'hidden'});
  const dot = svgNode('circle', {cx:0, cy:0, r:4, class:'cursor-point', visibility:'hidden'});
  const moveCursor = (index) => {
    const row = rows[index];
    indicator.style.transform = `translateX(${x(row.t)}px)`;
    indicator.setAttribute('visibility', 'visible');
    const known = Number.isFinite(row.value);
    dot.setAttribute('visibility', known ? 'visible' : 'hidden');
    if (known) dot.style.transform = `translate(${x(row.t)}px, ${y(row.value)}px)`;
  };
  const tooltip = inspect || onSelect ? document.createElement('div') : null;
  if (tooltip) {
    chart.append(indicator,dot);
    tooltip.className = 'chart-tooltip';
    tooltip.hidden = true;
    tooltip.setAttribute('aria-hidden', 'true');
    let selected = rows.length - 1;
    const showPoint = (index) => {
      selected = index;
      moveCursor(index);
      onSelect?.(index);
      const rect = chart.getBoundingClientRect();
      const row = rows[index];
      const bounds = frame.getBoundingClientRect();
      const pointX = rect.left - bounds.left + x(row.t) * rect.width / width;
      const pointY = rect.top - bounds.top + (Number.isFinite(row.value) ? y(row.value) : height / 2) * rect.height / height;
      const utcLabel = row.label.endsWith(' UTC') ? row.label : `${row.label} UTC`;
      tooltip.textContent = row.tooltip ?? `${utcLabel} · ${Number.isFinite(row.value) ? chartValue(row.value) : 'No observation'}`;
      tooltip.hidden = false;
      const left = Math.max(0, Math.min(bounds.width - tooltip.offsetWidth, pointX - tooltip.offsetWidth / 2));
      const above = pointY - tooltip.offsetHeight - 10;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${above < 0 ? pointY + 10 : above}px`;
    };
    const selectPoint = (event) => {
      const rect = chart.getBoundingClientRect();
      const offset = (event.clientX - rect.left) * width / rect.width;
      const index = rows.reduce((best,row,i) => Math.abs(x(row.t) - offset) < Math.abs(x(rows[best].t) - offset) ? i : best, 0);
      showPoint(index);
    };
    const hidePoint = () => {
      tooltip.hidden = true;
      if (inspect) {
        indicator.setAttribute('visibility', 'hidden');
        dot.setAttribute('visibility', 'hidden');
      }
    };
    chart.addEventListener('click', selectPoint);
    chart.addEventListener('pointermove', (event) => {
      if (event.pointerType === 'mouse') selectPoint(event);
    });
    chart.addEventListener('pointerleave', hidePoint);
    if (inspect) {
      chart.setAttribute('tabindex', '0');
      chart.addEventListener('focus', () => showPoint(selected));
      chart.addEventListener('blur', hidePoint);
      chart.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        const step = event.key === 'ArrowLeft' ? -1 : 1;
        showPoint(Math.min(rows.length - 1, Math.max(0, selected + step)));
      });
    }
  }
  const scale = document.createElement('div');
  scale.className = 'chart-scale';
  for (const label of rows.length === 1 ? [latest.label] : [rows[0].label, latest.label]) {
    const tick = document.createElement('span');
    tick.textContent = label;
    scale.append(tick);
  }
  const axes = document.createElement('div');
  axes.className = 'chart-axis';
  const units = document.createElement('span');
  units.className = 'chart-unit';
  units.textContent = axisUnit;
  axes.append(units);
  for (const tick of [high, (low + high) / 2, low]) {
    const value = document.createElement('span');
    value.textContent = axisValue(tick);
    axes.append(value);
  }
  axes.setAttribute('aria-hidden', 'true');
  chart.setAttribute('aria-label', `${label} Vertical scale ${chartValue(low)} to ${chartValue(high)}. ${rows[0].label} to ${latest.label}.`);
  const frame = document.createElement('div');
  frame.className = 'chart-frame';
  frame.append(axes, chart);
  if (tooltip) frame.append(tooltip);
  container.replaceChildren(frame,scale);
  return moveCursor;
}
