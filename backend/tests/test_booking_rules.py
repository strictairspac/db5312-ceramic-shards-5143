from datetime import timedelta
from fractions import Fraction

import pytest

from src.services import booking_rules as rules


REPORT = {
    'summary': 'x',
    'sections': [
        {'title': '本轮搬迁清单', 'rows': [
            {'序号': 1, '批次编号': 'A', '源节点': 'OLD', '目标节点': 'NORTH', '搬迁字节': 120_000_000},
            {'序号': 2, '批次编号': 'B', '源节点': 'OLD', '目标节点': 'WEST', '搬迁字节': 60_000_000},
        ]},
    ],
}


def test_manifest_counts_both_endpoints_and_totals():
    node_bytes, total = rules.read_manifest(REPORT)
    assert node_bytes == {'NORTH': 120_000_000, 'OLD': 180_000_000, 'WEST': 60_000_000}
    assert total == 180_000_000


def test_manifest_rejects_empty_and_malformed_rows():
    with pytest.raises(ValueError):
        rules.read_manifest({'sections': [{'title': '本轮搬迁清单', 'rows': []}]})
    bad = {'sections': [{'title': '本轮搬迁清单', 'rows': [{'源节点': 'OLD', '目标节点': 'WEST', '搬迁字节': 0}]}]}
    with pytest.raises(ValueError):
        rules.read_manifest(bad)


def test_usage_rate_divides_bytes_by_duration():
    rates = rules.usage_rates({'OLD': 180_000_000}, 3600)
    assert rates['OLD'] == Fraction(180_000_000)
    half = rules.usage_rates({'OLD': 180_000_000}, 1800)
    assert half['OLD'] == Fraction(360_000_000)


def test_window_requires_positive_whole_seconds_and_defaults_to_local_zone():
    window = rules.parse_window('2026-11-22T09:00:00', '2026-11-22T10:00:00')
    assert window.duration_seconds == 3600
    assert window.start_at == '2026-11-22T01:00:00+00:00'
    with pytest.raises(ValueError):
        rules.parse_window('2026-11-22T09:00:00', '2026-11-22T09:00:00')
    with pytest.raises(ValueError):
        rules.parse_window('2026-11-22T09:00:00.5', '2026-11-22T10:00:00')


def _intervals(window_end):
    start = rules.parse_utc('2026-11-22T09:00:00+08:00')
    return [(start, window_end, {'OLD': Fraction(100)})]


def test_back_to_back_windows_are_allowed():
    window = rules.parse_window('2026-11-22T10:00:00+08:00', '2026-11-22T11:00:00+08:00')
    existing = _intervals(window.start)
    assert rules.capacity_failures({'OLD': 100}, window, {'OLD': Fraction(100)}, existing) == []


def test_point_in_time_overlap_is_rejected():
    window = rules.parse_window('2026-11-22T09:30:00+08:00', '2026-11-22T10:30:00+08:00')
    existing = _intervals(rules.parse_utc('2026-11-22T10:00:00+08:00'))
    failures = rules.capacity_failures({'OLD': 100}, window, {'OLD': Fraction(100)}, existing)
    assert failures and failures[0][0] == 'OLD'


def test_partial_overlap_with_room_is_allowed():
    window = rules.parse_window('2026-11-22T09:30:00+08:00', '2026-11-22T10:30:00+08:00')
    existing = _intervals(rules.parse_utc('2026-11-22T10:00:00+08:00'))
    assert rules.capacity_failures({'OLD': 201}, window, {'OLD': Fraction(100)}, existing) == []


def test_missing_bandwidth_is_reported():
    window = rules.parse_window('2026-11-22T10:00:00+08:00', '2026-11-22T11:00:00+08:00')
    failures = rules.capacity_failures({}, window, {'NORTH': Fraction(1)}, [])
    assert failures == [('NORTH', None, None)]


def test_overlapping_same_window_sums_rates():
    window = rules.parse_window('2026-11-22T10:00:00+08:00', '2026-11-22T11:00:00+08:00')
    existing = [(window.start, window.end, {'OLD': Fraction(60)})]
    assert rules.capacity_failures({'OLD': 100}, window, {'OLD': Fraction(50)}, existing)
    assert rules.capacity_failures({'OLD': 110}, window, {'OLD': Fraction(50)}, existing) == []
