"""Pure rules for bandwidth booking: windows, manifest usage and overlap checks.

Keeping this module free of database imports makes the capacity arithmetic easy to
reason about and to exercise on its own.  Usage is expressed as bytes per hour,
matching the warehouse keeper's borrowable-bandwidth setting.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction

HOUR_SECONDS = 3600
MANIFEST_TITLE = '本轮搬迁清单'
# The control room works in China Standard Time; timestamps without an explicit
# offset are read in that zone so the form's local inputs book the intended slot.
LOCAL_ZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    start_at: str
    end_at: str
    duration_seconds: int


def _timestamp(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}须为 ISO 时间')
    text = value.strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f'{label}须为 ISO 时间') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=LOCAL_ZONE)
    return parsed.astimezone(timezone.utc)


def parse_window(start_at, end_at):
    start = _timestamp(start_at, '开始时间')
    end = _timestamp(end_at, '结束时间')
    if end <= start:
        raise ValueError('结束时间须晚于开始时间')
    delta = end - start
    if delta.microseconds:
        raise ValueError('时段须按整秒预约')
    return Window(
        start=start,
        end=end,
        start_at=start.isoformat(timespec='seconds'),
        end_at=end.isoformat(timespec='seconds'),
        duration_seconds=int(delta.total_seconds()),
    )


def read_manifest(report):
    """Return (node -> transferred bytes, total bytes) for the reviewed list.

    Every manifest row is one batch move.  Its bytes are sent by the source
    warehouse and received by the target, so both endpoints carry that volume.
    """
    if not isinstance(report, dict):
        raise ValueError('执行证据格式不正确，无法读取搬迁清单')
    sections = report.get('sections')
    if not isinstance(sections, list):
        raise ValueError('执行证据缺少搬迁清单')
    rows = None
    for section in sections:
        if isinstance(section, dict) and section.get('title') == MANIFEST_TITLE:
            rows = section.get('rows')
            break
    if not isinstance(rows, list):
        raise ValueError('执行证据缺少本轮搬迁清单')
    if not rows:
        raise ValueError('该清单没有搬迁任务，无需预约时段')

    node_bytes = {}
    total = 0
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f'搬迁清单第 {index} 行格式不正确')
        source = row.get('源节点')
        target = row.get('目标节点')
        size = row.get('搬迁字节')
        for value, label in ((source, '源节点'), (target, '目标节点')):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'搬迁清单第 {index} 行{label}不正确')
        if type(size) is not int or size <= 0:
            raise ValueError(f'搬迁清单第 {index} 行搬迁字节须为正整数')
        node_bytes[source] = node_bytes.get(source, 0) + size
        node_bytes[target] = node_bytes.get(target, 0) + size
        total += size
    return dict(sorted(node_bytes.items())), total


def usage_rates(node_bytes, duration_seconds):
    if type(duration_seconds) is not int or duration_seconds <= 0:
        raise ValueError('预约时长须为正整数秒')
    return {
        node: Fraction(int(size) * HOUR_SECONDS, duration_seconds)
        for node, size in node_bytes.items()
        if size
    }


def parse_utc(value):
    return _timestamp(value, '预约时间')


def _peak_rate(intervals, start, end):
    """Highest point-in-time sum of rates over [start, end).

    Intervals are half-open, so one ending exactly when another starts is a
    valid back-to-back (首尾相接) arrangement.
    """
    events = {}
    for item_start, item_end, rate in intervals:
        if not rate:
            continue
        left = max(item_start, start)
        right = min(item_end, end)
        if left >= right:
            continue
        events[left] = events.get(left, Fraction(0)) + rate
        events[right] = events.get(right, Fraction(0)) - rate
    running = Fraction(0)
    peak = Fraction(0)
    for moment in sorted(events):
        running += events[moment]
        if running > peak:
            peak = running
    return peak


def capacity_failures(capacities, window, candidate_rates, existing):
    """Return [(node, peak_rate, capacity)] for every over-committed node.

    ``existing`` is an iterable of ``(start, end, rates)`` for other active
    bookings.  All of them are included, but only their overlap with the
    candidate window contributes.  The check is exact and point-in-time, not a
    coarse "any overlap" sum, so end-to-start chains remain bookable.
    """
    failures = []
    for node in sorted(candidate_rates):
        capacity = capacities.get(node)
        if capacity is None:
            failures.append((node, None, None))
            continue
        intervals = [
            (start, end, rates.get(node, Fraction(0)))
            for start, end, rates in existing
        ]
        intervals.append((window.start, window.end, candidate_rates[node]))
        peak = _peak_rate(intervals, window.start, window.end)
        if peak > capacity:
            failures.append((node, peak, capacity))
    return failures


def human_rate(value):
    if value is None:
        return '未设置'
    if value.denominator == 1:
        return f'{int(value):,}'
    return f'{float(value):,.2f}'
