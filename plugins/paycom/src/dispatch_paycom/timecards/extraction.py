"""Reviewed Paycom timecard DOM projection executed in the managed page."""

# Keep this as a browser-evaluable function expression.  The managed Browser
# Manager supplies ``config`` as the second argument to page.evaluate().
EXTRACTION_SCRIPT = r'''(function runtimeExtract(config) {
 const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
 const decode = value => {
  let out = String(value ?? '');
  for (let index = 0; index < 2; index++) {
   const div = document.createElement('div');
   div.innerHTML = out.replace(/<br\s*\/?\s*>/gi, '\n');
   out = div.textContent || '';
  }
  return out.replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
 };
 const numeric = value => {
  const text = clean(value);
  if (!text) return null;
  const number = Number(text.replace(/[$,]/g, ''));
  return Number.isFinite(number) && number >= 0 ? number : null;
 };
 const visible = element => {
  if (!element) return '';
  const clone = element.cloneNode(true);
  clone.querySelectorAll('script,style').forEach(node => node.remove());
  return clean(clone.textContent);
 };
 const same = (left, right) => left.length === right.length && left.every((item, index) => item === right[index]);
 const canonicalHeaders = ['date', 'paycode', 'i1', 'allocation1', 'o1', 'i2', 'allocation2', 'o2', 'hours', 'total_hours', 'amount', 'exception-points', 'waiver', 'comment', 'missing-punch', 'delete'];
 const noWaiverHeaders = canonicalHeaders.filter(item => item !== 'waiver');
 const weekdays = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
 if (!config || !config.period || !Array.isArray(config.period.dates) || config.period.dates.length !== 14) return null;
 const expectedDates = config.period.dates.map(value => String(value));
 const expectedByMonthDay = new Map(expectedDates.map(value => [value.slice(5), value]));
 const table = document.querySelector('#tbltimesheet');
 if (!table) return null;
 const headers = Array.from(table.querySelectorAll('thead [data-column]')).map(element => element.getAttribute('data-column'));
 if (!same(headers, canonicalHeaders) && !same(headers, noWaiverHeaders)) return null;
 const column = name => headers.indexOf(name);
 const cell = (cells, name) => cells[column(name)];
 const allRows = Array.from(table.querySelectorAll(':scope > tbody > tr'));
 const isWeekly = row => /^Weekly Totals$/i.test(clean(row.children[0]?.textContent));
 const dayShape = row => /^[A-Z]{3}\s*\(\d{2}\/\d{2}\)$/.test(clean(row.children[0]?.textContent));
 const renderedDay = row => {
  const text = clean(row.children[0]?.textContent);
  const match = text.match(/^([A-Z]{3})\s*\((\d{2})\/(\d{2})\)$/);
  if (!match) return null;
  const date = expectedByMonthDay.get(`${match[2]}-${match[3]}`);
  if (!date) return { invalid: true };
  const parsed = new Date(`${date}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || weekdays[parsed.getUTCDay()] !== match[1]) return { invalid: true };
  return { date, label: match[1] };
 };
 const meaningful = row => Boolean(clean(row.textContent) || row.querySelector('input,select,textarea,button,[title]'));
 const slots = [['i1', column('i1')], ['o1', column('o1')], ['i2', column('i2')], ['o2', column('o2')]];
 const parsePunch = (element, slot, rowIndex) => {
  const displayTime = clean(element?.textContent);
  if (!displayTime || displayTime === '??') return null;
  const changeRequestStatus = element.querySelector('.pcrApproved') ? 'approved' : element.querySelector('.pcrPending') ? 'pending' : element.querySelector('.pcrRejected') ? 'rejected' : null;
  const approved = changeRequestStatus === 'approved';
  const node = element.querySelector('[title*="Actual:"]');
  const raw = node?.getAttribute('title');
  if (!raw) return { ordinal: 0, rowIndex, slot, kind: '', displayTime, actualTime: '', roundedTime: '', clockName: '', clockCode: '', comment: '', provenanceAvailable: false, changeRequestStatus, approved };
  const text = decode(raw);
  const lines = text.split(/\n+/).map(clean).filter(Boolean);
  const first = lines[0] || '';
  const kind = (first.match(/^(IN DAY|OUT LUNCH|IN LUNCH|OUT DAY)\b/i) || [])[1]?.toUpperCase() || '';
  const field = name => {
   const line = lines.find(item => item.toLowerCase().startsWith(name.toLowerCase() + ':'));
   return line ? clean(line.slice(name.length + 1)) : '';
  };
  const clock = field('Clock');
  const match = clock.match(/^(.*?)\s*\(([^()]*)\)\s*$/);
  return { ordinal: 0, rowIndex, slot, kind, displayTime, actualTime: field('Actual'), roundedTime: field('Rounded'), clockName: match ? clean(match[1]) : clock, clockCode: match ? clean(match[2]) : '', comment: field('Comment'), provenanceAvailable: true, changeRequestStatus, approved };
 };
 const projectRow = (row, rowIndex) => {
  const cells = Array.from(row.children);
  const unresolvedSlots = slots.filter(([, index]) => clean(cells[index]?.textContent) === '??').map(([slot]) => slot);
  const punches = slots.map(([slot, index]) => parsePunch(cells[index], slot, rowIndex)).filter(Boolean);
  const waiver = cell(cells, 'waiver')?.querySelector('input[type=checkbox]');
  const comments = Array.from(cell(cells, 'comment')?.querySelectorAll('[title]') || []).map(element => decode(element.getAttribute('title')).replace(/^Comment:\s*/i, '').trim()).filter(Boolean);
  return { payCode: visible(cell(cells, 'paycode')), allocation1: visible(cell(cells, 'allocation1')), allocation2: visible(cell(cells, 'allocation2')), hours: numeric(visible(cell(cells, 'hours'))), totalHours: numeric(visible(cell(cells, 'total_hours'))), dollars: numeric(visible(cell(cells, 'amount'))), exceptionText: visible(cell(cells, 'exception-points')), waiverChecked: waiver ? Boolean(waiver.checked) : null, comments, unresolvedSlots, punches };
 };
 const dayRows = [];
 const weeklyRows = [];
 const continuationRows = [];
 let dayIndex = -1;
 for (const row of allRows) {
  const parsed = renderedDay(row);
  if (parsed) {
   if (parsed.invalid) return null;
   dayRows.push({ row, date: parsed.date, label: parsed.label });
   dayIndex += 1;
   continue;
  }
  if (dayShape(row)) return null;
  if (isWeekly(row)) {
   weeklyRows.push(row);
   continue;
  }
  if (!meaningful(row)) continue;
  const cells = Array.from(row.children);
  if (dayIndex < 0 || clean(row.children[0]?.textContent) !== '' || cells.length !== headers.length) return null;
  continuationRows.push({ row, dayIndex });
 }
 if (dayRows.length !== expectedDates.length) return null;
 if (dayRows.some((item, index) => item.date !== expectedDates[index] || item.label !== weekdays[new Date(`${expectedDates[index]}T00:00:00Z`).getUTCDay()])) return null;
 const days = dayRows.map(item => {
  const projected = projectRow(item.row, 0);
  projected.punches.forEach((punch, punchIndex) => { punch.ordinal = punchIndex + 1; });
  return { date: item.date, label: item.label, payCode: projected.payCode, allocation1: projected.allocation1, allocation2: projected.allocation2, hours: projected.hours, totalHours: projected.totalHours, dollars: projected.dollars, exceptionText: projected.exceptionText, waiverChecked: projected.waiverChecked, comments: projected.comments, missingPunch: projected.unresolvedSlots.length > 0, unresolvedSlots: [...projected.unresolvedSlots], punches: projected.punches };
 });
 const additionalRows = [];
 const nextRowIndex = Array(14).fill(1);
 for (const item of continuationRows) {
  const rowIndex = nextRowIndex[item.dayIndex]++;
  const projected = projectRow(item.row, rowIndex);
  const day = days[item.dayIndex];
  const start = day.punches.length;
  projected.punches.forEach((punch, index) => { punch.ordinal = start + index + 1; day.punches.push(punch); });
  const unresolved = projected.unresolvedSlots.map(slot => `${rowIndex}:${slot}`);
  day.unresolvedSlots.push(...unresolved);
  day.missingPunch = day.unresolvedSlots.length > 0;
  additionalRows.push({ date: day.date, rowIndex, rowClass: clean(item.row.className), payCode: projected.payCode, allocation1: projected.allocation1, allocation2: projected.allocation2, hours: projected.hours, totalHours: projected.totalHours, dollars: projected.dollars, exceptionText: projected.exceptionText, waiverChecked: projected.waiverChecked, comments: projected.comments, unresolvedSlots: projected.unresolvedSlots, punchOrdinals: projected.punches.map(punch => punch.ordinal) });
 }
 const weeklyTotals = weeklyRows.map(row => numeric(row.children[1]?.textContent));
 const generic = id => {
  const found = document.querySelector(id);
  if (!found) return [];
  return Array.from(found.querySelectorAll(':scope > tbody > tr')).map(row => Array.from(row.children).map(visible)).filter(row => row.some(Boolean) && !/^No Records Found$/i.test(row.join(' ')));
 };
 const periodTotalHours = weeklyTotals.length && weeklyTotals.every(item => typeof item === 'number') ? Number(weeklyTotals.reduce((a, b) => a + b, 0).toFixed(2)) : null;
 return { version: 1, sourceFormat: 'paycom-timecard-dom.v1', employeeCode: config.employeeCode, periodStart: config.period.start, periodEnd: config.period.end, periodKey: config.period.key, sourceUrl: config.sourceUrl, pageTitle: document.title, headers, days, additionalRows, weeklyTotals, periodTotalHours, approvals: generic('#approvals-table'), attestations: generic('#timecard-attestation-table'), mealWaivers: generic('#meal-waivers-table') };
})'''

__all__ = ["EXTRACTION_SCRIPT"]
