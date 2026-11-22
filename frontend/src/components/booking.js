import {panel} from './common/heading.js';
import {records} from './common/records.js';
import {esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const pad = (value)=>String(value).padStart(2,'0');

// Convert a stored UTC timestamp to the Asia/Shanghai wall-clock value a
// ``datetime-local`` input expects.  The server reads offset-less values in the
// same zone, so round-tripping keeps the intended slot.
export function localInput(value) {
  if (!value) return '';
  const parts = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit',
    day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}).formatToParts(new Date(value));
  const pick = (type)=>parts.find(part=>part.type===type)?.value || '';
  return `${pick('year')}-${pick('month')}-${pick('day')}T${pad(pick('hour'))}:${pad(pick('minute'))}`;
}

export function displayWindow(start, end) {
  if (!start || !end) return '—';
  const format = new Intl.DateTimeFormat('zh-CN', {dateStyle:'short', timeStyle:'short', timeZone:'Asia/Shanghai'});
  return `${format.format(new Date(start))} 至 ${format.format(new Date(end))}`;
}

function rate(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${new Intl.NumberFormat('zh-CN').format(number)} 字节/小时` : '—';
}

function usageRows(booking) {
  return (booking.node_usage || []).map(item=>[esc(item.node_id), esc(new Intl.NumberFormat('zh-CN').format(item.bytes)),
    esc(rate(item.bytes_per_hour))]);
}

function activeBooking(state, evidenceId) {
  return state.bookings.find(row=>row.evidence_id===evidenceId && row.status==='booked');
}

function latestBooking(state, evidenceId) {
  return state.bookings.find(row=>row.evidence_id===evidenceId);
}

function nodeNames(state, current) {
  const names = new Set(state.bandwidths.map(row=>row.node_id));
  state.bookings.forEach(row=>(row.node_usage || []).forEach(item=>names.add(item.node_id)));
  (current?.report?.sections || []).forEach(section=>(section.rows || []).forEach(row=>{
    if (row && row['源节点']) names.add(row['源节点']);
    if (row && row['目标节点']) names.add(row['目标节点']);
  }));
  (state.spec?.sample?.nodes || []).forEach(node=>names.add(node.node_id));
  return Array.from(names).filter(Boolean).sort();
}

function bandwidthSection(state, current) {
  const reviewer = state.session.role==='reviewer';
  const known = new Map(state.bandwidths.map(row=>[row.node_id, row]));
  const nodes = nodeNames(state, current);
  if (!nodes.length) return '';
  if (reviewer) {
    const rows = nodes.map(node=>{
      const current = known.get(node);
      return `<label class="field"><span>${esc(node)}</span><input type="number" min="0" step="1" name="bandwidth-${esc(node)}" data-current="${current ? esc(current.bandwidth_bytes_per_hour) : ''}" value="${current ? esc(current.bandwidth_bytes_per_hour) : ''}" placeholder="未设置" /></label>`;
    }).join('');
    return panel('库管可借带宽', `<p class="note">按字节/小时为各窑口库设置可借带宽；未设置的库不能预约。</p>
      <form id="bandwidth-form" class="field-grid">${rows}<div class="form-actions actions"><button>保存带宽</button></div></form>`);
  }
  return panel('库管可借带宽', records(['节点','可借带宽'], nodes.map(node=>{
    const current = known.get(node);
    return [esc(node), current ? esc(rate(current.bandwidth_bytes_per_hour)) : '<span class="note">未设置</span>'];
  })));
}

function bookingSection(state, current) {
  const role = state.session.role;
  const canBook = role==='operator';
  const reviewed = Boolean(current.reviewer);
  const active = activeBooking(state, current.id);
  const latest = latestBooking(state, current.id);
  const status = active
    ? `<p data-testid="booking-status">已预约：${esc(displayWindow(active.start_at, active.end_at))}（${esc(active.owner)}）</p>`
    : latest
      ? `<p data-testid="booking-status" class="note">最近的预约已于 ${esc(displayWindow(latest.start_at, latest.end_at))} 撤销。</p>`
      : `<p data-testid="booking-status" class="note">${reviewed ? '尚未预约时段。' : '复核员签署后可预约时段。'}</p>`;

  let form = '';
  if (!reviewed) {
    form = '<p class="note">搬迁清单经复核员签署后才能占用白班质检之外的带宽。</p>';
  } else if (canBook) {
    const prefill = active || {};
    form = `<form id="booking-form"><div class="field-grid">
      <label class="field"><span>开始时间</span><input type="datetime-local" name="start_at" required value="${esc(localInput(prefill.start_at))}" /></label>
      <label class="field"><span>结束时间</span><input type="datetime-local" name="end_at" required value="${esc(localInput(prefill.end_at))}" /></label>
      </div><div class="form-actions actions"><button>${active ? '保存改期' : '预约时段'}</button>
      ${active ? '<button type="button" class="secondary" id="cancel-booking">撤销预约</button>' : ''}</div></form>`;
  }

  const usage = active ? panel('本次预约用量', records(['节点','清单字节','用量'], usageRows(active))) : '';
  const holder = active ? `<p class="note">同一清单只保留一份有效预约；改期不成功时保留原时段。</p>` : '';
  return panel('时段预约', `${status}${form}${usage}${holder}`);
}

function bookingsOverview(state) {
  const active = state.bookings.filter(row=>row.status==='booked');
  if (!active.length) return '';
  const headers = ['清单证据','时段','总字节','经办','节点用量'];
  const body = active.map(row=>[
    `<a href="#evidence/${row.evidence_id}">证据 ${row.evidence_id}</a>`,
    esc(displayWindow(row.start_at, row.end_at)),
    esc(new Intl.NumberFormat('zh-CN').format(row.total_bytes)),
    esc(row.owner),
    (row.node_usage || []).map(item=>`${esc(item.node_id)} ${esc(rate(item.bytes_per_hour))}`).join('<br>'),
  ]);
  return panel('当前有效预约', records(headers, body));
}

export function bookingBlocks(state, current) {
  if (!current) return '';
  return `${bookingSection(state, current)}${bookingsOverview(state)}${bandwidthSection(state, current)}`;
}

export function bindBooking(state, current, api, update, fail) {
  if (!current) return;
  const active = activeBooking(state, current.id);
  submit('booking-form', async data=>{
    const window = {start_at:data.get('start_at'), end_at:data.get('end_at')};
    if (active) await api(`/evidence/${current.id}/booking/reschedule`, {method:'POST', data:window});
    else await api(`/evidence/${current.id}/booking`, {method:'POST', data:window});
    await update();
  }, fail);

  document.getElementById('cancel-booking')?.addEventListener('click', async()=>{
    try {
      await api(`/evidence/${current.id}/booking`, {method:'DELETE'});
      await update();
    } catch (error) { fail(error); }
  });

  const form = document.getElementById('bandwidth-form');
  form?.addEventListener('submit', async event=>{
    event.preventDefault();
    const button = event.submitter;
    if (button) button.disabled = true;
    try {
      const entries = Array.from(event.currentTarget.querySelectorAll('input[type="number"]'))
        .filter(input=>input.value !== '' && input.value !== input.dataset.current);
      if (!entries.length) { await update(); return; }
      for (const input of entries) {
        await api('/bandwidths', {method:'PUT', data:{node_id:input.name.replace('bandwidth-', ''),
          bandwidth_bytes_per_hour:Number(input.value)}});
      }
      await update();
    } catch (error) { fail(error); }
    finally { if (button) button.disabled = false; }
  });
}
