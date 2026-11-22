"""Time-window booking for reviewed relocation manifests.

A booking reserves borrowable bandwidth on every warehouse the manifest touches.
The rules live in :mod:`src.services.booking_rules`; this module adds persistence,
locking and the idempotency guarantees the manifest page relies on.
"""

import json
import threading
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.settings import engine
from src.constants.workflow import BookingStatus
from src.models.tables import bookings, evidence, node_bandwidths
from src.services import booking_rules
from src.services.workflow import log
from src.utils.time import utc_time

# SQLite ignores ``FOR UPDATE`` and shares one in-memory connection per process,
# so a process-wide guard serializes those accesses.  MySQL/Postgres use real
# connections and row locks instead.
_SQLITE = engine.dialect.name == 'sqlite'
_SQLITE_GUARD = threading.RLock()


@contextmanager
def _reader():
    if _SQLITE:
        with _SQLITE_GUARD:
            with engine.connect() as conn:
                yield conn
        return
    with engine.connect() as conn:
        yield conn


@contextmanager
def _writer(nodes):
    """Open a write transaction that serializes bookings sharing any of ``nodes``."""
    ordered = sorted(set(nodes))
    if _SQLITE:
        with _SQLITE_GUARD:
            with engine.begin() as conn:
                yield conn
        return
    with engine.connect() as connection:
        # READ COMMITTED means the capacity read made after the warehouse locks
        # are granted sees the other booking that just committed, instead of a
        # stale snapshot from before it.  Locks are taken in sorted order so
        # concurrent writers cannot form a lock cycle.
        conn = connection.execution_options(isolation_level='READ COMMITTED')
        with conn.begin():
            if ordered:
                conn.execute(select(node_bandwidths.c.node_id)
                             .where(node_bandwidths.c.node_id.in_(ordered))
                             .order_by(node_bandwidths.c.node_id)
                             .with_for_update()).all()
            yield conn


def _node_text(value):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError('节点编号须为非空文本，且首尾不能有空白')
    return value


def _mapping(row):
    result = dict(row)
    raw = result.get('node_bytes')
    if isinstance(raw, str):
        result['node_bytes'] = json.loads(raw)
    return result


def _number(value):
    return int(value) if value.denominator == 1 else round(float(value), 2)


def serialize(row):
    row = _mapping(row)
    node_bytes = row['node_bytes']
    rates = booking_rules.usage_rates(node_bytes, row['duration_seconds'])
    return {
        'id': row['id'],
        'evidence_id': row['evidence_id'],
        'status': row['status'],
        'start_at': row['start_at'],
        'end_at': row['end_at'],
        'duration_seconds': row['duration_seconds'],
        'total_bytes': row['total_bytes'],
        'node_bytes': node_bytes,
        'node_usage': [
            {'node_id': node, 'bytes': node_bytes[node], 'bytes_per_hour': _number(rates[node])}
            for node in sorted(node_bytes)
        ],
        'owner': row['owner'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def list_bandwidths():
    with _reader() as conn:
        rows = conn.execute(select(node_bandwidths).order_by(node_bandwidths.c.node_id)).mappings()
        return [dict(row) for row in rows]


def set_bandwidth(node_id, bytes_per_hour, actor):
    try:
        node_id = _node_text(node_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if type(bytes_per_hour) is not int or bytes_per_hour < 0:
        raise HTTPException(422, '可借带宽须为非负整数字节/小时')
    with _writer([node_id]) as conn:
        values = {'bandwidth_bytes_per_hour': bytes_per_hour, 'updated_by': actor, 'updated_at': utc_time()}
        count = conn.execute(node_bandwidths.update()
                             .where(node_bandwidths.c.node_id == node_id).values(**values)).rowcount
        if not count:
            try:
                with conn.begin_nested():
                    conn.execute(node_bandwidths.insert().values(node_id=node_id, **values))
            except IntegrityError:
                # Another request configured the same warehouse first.
                conn.execute(node_bandwidths.update()
                             .where(node_bandwidths.c.node_id == node_id).values(**values))
        log(conn, actor, 'set_bandwidth', node_id)
        return dict(conn.execute(select(node_bandwidths)
                                 .where(node_bandwidths.c.node_id == node_id)).mappings().first())


def _reviewed_evidence(conn, identity):
    row = conn.execute(select(evidence).where(evidence.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该执行证据')
    if row['reviewed_at'] is None:
        raise HTTPException(422, '搬迁清单复核后才能预约时段')
    return dict(row)


def _active(conn, evidence_id, lock=False):
    query = select(bookings).where(bookings.c.active_key == evidence_id)
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).mappings().first()
    return dict(row) if row else None


def _capacity_map(conn, nodes):
    rows = conn.execute(select(node_bandwidths)
                        .where(node_bandwidths.c.node_id.in_(sorted(nodes)))).mappings()
    return {row['node_id']: row['bandwidth_bytes_per_hour'] for row in rows}


def _overlapping(conn, window, exclude_id=None):
    query = select(bookings.c.id, bookings.c.start_at, bookings.c.end_at,
                   bookings.c.node_bytes, bookings.c.duration_seconds).where(
        bookings.c.status == BookingStatus.BOOKED,
        bookings.c.start_at < window.end_at,
        bookings.c.end_at > window.start_at)
    if exclude_id is not None:
        query = query.where(bookings.c.id != exclude_id)
    return conn.execute(query).mappings()


def _assert_capacity(conn, node_bytes, window, exclude_id=None):
    """Reject the candidate unless every warehouse has room at every instant."""
    nodes = sorted(node_bytes)
    capacities = _capacity_map(conn, nodes)
    missing = [node for node in nodes if node not in capacities]
    if missing:
        raise HTTPException(422, f'节点 {", ".join(missing)} 尚未设置可借带宽，请库管先配置')

    rates = booking_rules.usage_rates(node_bytes, window.duration_seconds)
    existing = [
        (booking_rules.parse_utc(row['start_at']),
         booking_rules.parse_utc(row['end_at']),
         booking_rules.usage_rates(_mapping(row)['node_bytes'], row['duration_seconds']))
        for row in _overlapping(conn, window, exclude_id)
    ]
    failures = booking_rules.capacity_failures(
        {node: capacities[node] for node in nodes}, window, rates, existing)
    if failures:
        details = [
            f'节点 {node} 在同一时刻的用量 {booking_rules.human_rate(peak)} 字节/小时，'
            f'超过可借带宽 {booking_rules.human_rate(capacity)} 字节/小时'
            for node, peak, capacity in failures
        ]
        raise HTTPException(409, '；'.join(details) + '。请换一个时段或先释放预约。')


def _same_window(row, window):
    return row['start_at'] == window.start_at and row['end_at'] == window.end_at


def reserve(evidence_id, start_at, end_at, actor):
    """Create the manifest's single active booking; retries are idempotent."""
    try:
        window = booking_rules.parse_window(start_at, end_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    with _reader() as conn:
        record = _reviewed_evidence(conn, evidence_id)
    try:
        node_bytes, total = booking_rules.read_manifest(record['report'])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    with _writer(node_bytes) as conn:
        existing = _active(conn, evidence_id, lock=True)
        if existing:
            if _same_window(existing, window):
                return serialize(existing), True
            raise HTTPException(409, '该清单已有有效预约，请改期或先撤销后再约')
        _assert_capacity(conn, node_bytes, window)
        now = utc_time()
        try:
            with conn.begin_nested():
                booking_id = conn.execute(bookings.insert().values(
                    evidence_id=evidence_id, active_key=evidence_id,
                    start_at=window.start_at, end_at=window.end_at,
                    duration_seconds=window.duration_seconds, node_bytes=node_bytes,
                    total_bytes=total, status=BookingStatus.BOOKED, owner=actor,
                    created_at=now, updated_at=now)).inserted_primary_key[0]
        except IntegrityError:
            # Another process created this manifest's slot between our read and write.
            raise HTTPException(409, '该清单已有有效预约，请改期或先撤销后再约') from None
        log(conn, actor, 'book_window', booking_id)
        row = conn.execute(select(bookings).where(bookings.c.id == booking_id)).mappings().first()
        return serialize(row), False


def reschedule(evidence_id, start_at, end_at, actor):
    """Move a booking only when the new window fits; otherwise keep the old one."""
    try:
        window = booking_rules.parse_window(start_at, end_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    with _reader() as conn:
        preview = _active(conn, evidence_id)
    if preview is None:
        raise HTTPException(404, '该清单没有有效预约，无法改期')
    node_bytes = _mapping(preview)['node_bytes']

    with _writer(node_bytes) as conn:
        current = conn.execute(select(bookings).where(bookings.c.id == preview['id'])
                               .with_for_update()).mappings().first()
        if current is None or current['status'] != BookingStatus.BOOKED:
            raise HTTPException(404, '该清单没有有效预约，无法改期')
        current = dict(current)
        if _same_window(current, window):
            return serialize(current)
        _assert_capacity(conn, _mapping(current)['node_bytes'], window, exclude_id=current['id'])
        conn.execute(bookings.update().where(
            bookings.c.id == current['id'], bookings.c.status == BookingStatus.BOOKED).values(
            start_at=window.start_at, end_at=window.end_at,
            duration_seconds=window.duration_seconds, updated_at=utc_time()))
        log(conn, actor, 'rebook_window', current['id'])
        row = conn.execute(select(bookings).where(bookings.c.id == current['id'])).mappings().first()
        return serialize(row)


def cancel(evidence_id, actor):
    """Release the manifest's slot immediately; repeated cancels stay harmless."""
    with _writer([]) as conn:
        current = _active(conn, evidence_id, lock=True)
        if current is not None:
            conn.execute(bookings.update().where(
                bookings.c.id == current['id'],
                bookings.c.status == BookingStatus.BOOKED).values(
                status=BookingStatus.CANCELLED, active_key=None, updated_at=utc_time()))
            log(conn, actor, 'cancel_window', current['id'])
            row = conn.execute(select(bookings).where(bookings.c.id == current['id'])).mappings().first()
            return serialize(row)
        latest = conn.execute(select(bookings).where(bookings.c.evidence_id == evidence_id)
                              .order_by(bookings.c.id.desc()).limit(1)).mappings().first()
    if latest is not None:
        return serialize(latest)
    raise HTTPException(404, '该清单没有可撤销的预约')


def recent(limit=100):
    with _reader() as conn:
        rows = conn.execute(select(bookings).order_by(bookings.c.id.desc()).limit(limit)).mappings()
        return [serialize(row) for row in rows]
