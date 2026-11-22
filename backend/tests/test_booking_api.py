import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.settings import engine
from src.constants.workflow import Role
from src.main import app
from src.models.tables import cases, evidence, runs, schema, users
from src.services.domain import SPEC, solve
from src.services.security import hash_password
from src.utils.time import utc_time


PASSWORD = 'Demo-Walkthrough-2026'


@pytest.fixture(scope='session')
def client():
    schema.create_all(engine)
    with engine.begin() as conn:
        for role in Role:
            conn.execute(users.insert().values(username=role.value, role=role.value,
                                               password_hash=hash_password(PASSWORD)))
    with TestClient(app) as test_client:
        yield test_client


def _login(client, role):
    response = client.post('/login', json={'username': role, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


@pytest.fixture(scope='session')
def operator(client):
    return _login(client, Role.OPERATOR)


@pytest.fixture(scope='session')
def reviewer(client):
    return _login(client, Role.REVIEWER)


@pytest.fixture(scope='session')
def viewer(client):
    return _login(client, Role.VIEWER)


@pytest.fixture(autouse=True)
def clean_bookings():
    schema.create_all(engine)
    with engine.begin() as conn:
        for table in ('manifest_bookings', 'node_bandwidths', 'business_evidence', 'business_runs'):
            conn.exec_driver_sql(f'DELETE FROM {table}')
    yield


def _reviewed_evidence(client, operator, reviewer, rows):
    payload = dict(SPEC['sample'])
    payload['snapshot_id'] = f'TY-{uuid.uuid4().hex[:8]}'
    report = {'summary': '搬迁清单', 'metrics': {}, 'sections': [
        {'title': '本轮搬迁清单', 'rows': rows},
    ]}
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(
            slug=uuid.uuid4().hex, title='清单', payload=payload, author='operator',
            created_at=utc_time())).inserted_primary_key[0]
        run_id = conn.execute(runs.insert().values(
            case_id=case_id, status='complete', owner='operator', created_at=utc_time())).inserted_primary_key[0]
        evidence_id = conn.execute(evidence.insert().values(
            run_id=run_id, report=report, digest='0' * 64)).inserted_primary_key[0]
    response = client.post(f'/evidence/{evidence_id}/review', headers=reviewer, json={'note': '核对无误'})
    assert response.status_code == 200, response.text
    return evidence_id


def _row(source, target, size):
    return {'序号': 1, '批次编号': 'TY-1', '源节点': source, '目标节点': target, '搬迁字节': size}


def _bandwidth(client, reviewer, node, rate):
    response = client.put('/bandwidths', headers=reviewer,
                          json={'node_id': node, 'bandwidth_bytes_per_hour': rate})
    assert response.status_code == 200, response.text


def _book(client, headers, evidence_id, start, end):
    return client.post(f'/evidence/{evidence_id}/booking', headers=headers,
                       json={'start_at': start, 'end_at': end})


WINDOW = ('2026-11-23T09:00:00+08:00', '2026-11-23T10:00:00+08:00')
NEXT = ('2026-11-23T10:00:00+08:00', '2026-11-23T11:00:00+08:00')


def test_unreviewed_manifest_cannot_be_booked(client, operator, reviewer):
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(
            slug=uuid.uuid4().hex, title='t', payload=SPEC['sample'], author='operator',
            created_at=utc_time())).inserted_primary_key[0]
        run_id = conn.execute(runs.insert().values(
            case_id=case_id, status='complete', owner='operator', created_at=utc_time())).inserted_primary_key[0]
        evidence_id = conn.execute(evidence.insert().values(
            run_id=run_id, report={'sections': [{'title': '本轮搬迁清单', 'rows': [_row('OLD', 'NORTH', 10)]}]},
            digest='0' * 64)).inserted_primary_key[0]
    _bandwidth(client, reviewer, 'OLD', 1_000_000)
    _bandwidth(client, reviewer, 'NORTH', 1_000_000)
    response = _book(client, operator, evidence_id, *WINDOW)
    assert response.status_code == 422
    assert '复核' in response.json()['detail']


def test_booking_uses_bytes_divided_by_duration(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 50)
    _bandwidth(client, reviewer, 'NORTH', 50)
    # One hour would need 100 bytes/h and fail; spreading the same bytes over two
    # hours halves the rate to 50 bytes/h, which exactly fits.
    response = _book(client, operator, evidence_id, '2026-11-23T09:00:00+08:00', '2026-11-23T11:00:00+08:00')
    assert response.status_code == 201, response.text
    booking = response.json()
    assert booking['status'] == 'booked'
    assert booking['duration_seconds'] == 7200
    assert {item['node_id']: item['bytes_per_hour'] for item in booking['node_usage']} == {'NORTH': 50, 'OLD': 50}

    # Same manifest, same window: retry returns the existing slot, no duplicate.
    retry = _book(client, operator, evidence_id, '2026-11-23T09:00:00+08:00', '2026-11-23T11:00:00+08:00')
    assert retry.status_code == 200
    assert retry.json()['id'] == booking['id']
    assert len(client.get('/bookings', headers=operator).json()) == 1


def test_retry_with_another_window_does_not_hold_two_slots(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 1_000)
    _bandwidth(client, reviewer, 'NORTH', 1_000)
    first = _book(client, operator, evidence_id, *WINDOW)
    assert first.status_code == 201
    second = _book(client, operator, evidence_id, *NEXT)
    assert second.status_code == 409


def test_missing_bandwidth_blocks_booking(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 1_000)
    response = _book(client, operator, evidence_id, *WINDOW)
    assert response.status_code == 422
    assert 'NORTH' in response.json()['detail']


def test_overlapping_usage_sums_and_excess_is_rejected(client, operator, reviewer):
    first = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    second = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 150)
    _bandwidth(client, reviewer, 'NORTH', 150)
    assert _book(client, operator, first, *WINDOW).status_code == 201
    clash = _book(client, operator, second, '2026-11-23T09:30:00+08:00', '2026-11-23T10:30:00+08:00')
    assert clash.status_code == 409
    assert 'OLD' in clash.json()['detail']


def test_back_to_back_windows_are_allowed(client, operator, reviewer):
    first = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    second = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 100)
    _bandwidth(client, reviewer, 'NORTH', 100)
    assert _book(client, operator, first, *WINDOW).status_code == 201
    response = _book(client, operator, second, *NEXT)
    assert response.status_code == 201, response.text


def test_all_touched_nodes_must_fit_together(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer,
                                     [_row('OLD', 'NORTH', 100), _row('WEST', 'OLD', 100)])
    _bandwidth(client, reviewer, 'OLD', 150)
    _bandwidth(client, reviewer, 'NORTH', 1_000)
    _bandwidth(client, reviewer, 'WEST', 1_000)
    response = _book(client, operator, evidence_id, *WINDOW)
    assert response.status_code == 409
    assert 'OLD' in response.json()['detail']
    assert client.get('/bookings', headers=operator).json() == []


def test_warehouses_are_checked_independently(client, operator, reviewer):
    # Two manifests that touch different warehouses may share a window.
    first = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    second = _reviewed_evidence(client, operator, reviewer, [_row('WEST', 'EAST', 100)])
    for node in ('OLD', 'NORTH', 'WEST', 'EAST'):
        _bandwidth(client, reviewer, node, 100)
    assert _book(client, operator, first, *WINDOW).status_code == 201
    assert _book(client, operator, second, *WINDOW).status_code == 201
    assert len(client.get('/bookings', headers=operator).json()) == 2


def test_bandwidth_of_zero_blocks_everything(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 100)
    _bandwidth(client, reviewer, 'NORTH', 0)
    response = _book(client, operator, evidence_id, *WINDOW)
    assert response.status_code == 409
    assert 'NORTH' in response.json()['detail']


def test_reschedule_keeps_old_slot_when_new_window_fails(client, operator, reviewer):
    held = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    other = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 100)
    _bandwidth(client, reviewer, 'NORTH', 100)
    original = _book(client, operator, held, *WINDOW).json()
    assert _book(client, operator, other, *NEXT).status_code == 201

    response = client.post(f'/evidence/{held}/booking/reschedule', headers=operator,
                           json={'start_at': NEXT[0], 'end_at': NEXT[1]})
    assert response.status_code == 409
    current = client.get('/bookings', headers=operator).json()
    mine = next(row for row in current if row['evidence_id'] == held)
    assert mine['start_at'] == original['start_at']
    assert mine['status'] == 'booked'


def test_reschedule_to_free_window_moves_the_slot(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 100)
    _bandwidth(client, reviewer, 'NORTH', 100)
    original = _book(client, operator, evidence_id, *WINDOW).json()
    response = client.post(f'/evidence/{evidence_id}/booking/reschedule', headers=operator,
                           json={'start_at': NEXT[0], 'end_at': NEXT[1]})
    assert response.status_code == 200, response.text
    moved = response.json()
    assert moved['id'] == original['id']
    assert moved['start_at'] == '2026-11-23T02:00:00+00:00'
    assert len(client.get('/bookings', headers=operator).json()) == 1


def test_cancel_releases_capacity_immediately(client, operator, reviewer):
    first = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    second = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 100)
    _bandwidth(client, reviewer, 'NORTH', 100)
    booked = _book(client, operator, first, *WINDOW).json()
    assert _book(client, operator, second, *WINDOW).status_code == 409

    cancelled = client.delete(f'/evidence/{first}/booking', headers=operator)
    assert cancelled.status_code == 200
    assert cancelled.json()['status'] == 'cancelled'

    assert _book(client, operator, second, *WINDOW).status_code == 201
    # Cancelling again is harmless and does not revive the old slot.
    again = client.delete(f'/evidence/{first}/booking', headers=operator)
    assert again.status_code == 200
    assert again.json()['status'] == 'cancelled'


def test_cancelled_manifest_can_be_booked_again(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 100)
    _bandwidth(client, reviewer, 'NORTH', 100)
    first = _book(client, operator, evidence_id, *WINDOW).json()
    client.delete(f'/evidence/{evidence_id}/booking', headers=operator)
    second = _book(client, operator, evidence_id, *WINDOW)
    assert second.status_code == 201, second.text
    assert second.json()['id'] != first['id']


def test_concurrent_bookings_never_exceed_bandwidth(client, operator, reviewer):
    evidence_ids = [_reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)]) for _ in range(6)]
    _bandwidth(client, reviewer, 'OLD', 200)
    _bandwidth(client, reviewer, 'NORTH', 200)

    def attempt(evidence_id):
        return _book(client, operator, evidence_id, *WINDOW).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(attempt, evidence_ids))
    assert codes.count(201) == 2
    assert codes.count(409) == 4
    active = [row for row in client.get('/bookings', headers=operator).json() if row['status'] == 'booked']
    assert len(active) == 2


def test_viewer_can_read_but_not_book(client, operator, reviewer, viewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 100)
    _bandwidth(client, reviewer, 'NORTH', 100)
    _book(client, operator, evidence_id, *WINDOW)
    assert client.get('/bookings', headers=viewer).status_code == 200
    assert client.get('/bandwidths', headers=viewer).status_code == 200
    assert _book(client, viewer, evidence_id, *NEXT).status_code == 403
    assert client.put('/bandwidths', headers=operator,
                      json={'node_id': 'OLD', 'bandwidth_bytes_per_hour': 1}).status_code == 403


def test_bandwidth_must_be_non_negative_integer(client, reviewer):
    response = client.put('/bandwidths', headers=reviewer,
                          json={'node_id': 'OLD', 'bandwidth_bytes_per_hour': -1})
    assert response.status_code == 422


def test_booking_reads_the_real_domain_manifest(client, operator, reviewer):
    # The demo scenario moves 120,000,000 bytes: OLD -> NORTH (70M) and OLD -> WEST (50M).
    report = solve(SPEC['sample'], engine)
    manifest = next(section for section in report['sections'] if section['title'] == '本轮搬迁清单')
    assert manifest['rows'], manifest
    expected = {}
    for row in manifest['rows']:
        expected[row['源节点']] = expected.get(row['源节点'], 0) + row['搬迁字节']
        expected[row['目标节点']] = expected.get(row['目标节点'], 0) + row['搬迁字节']

    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(
            slug=uuid.uuid4().hex, title='真实清单', payload=SPEC['sample'], author='operator',
            created_at=utc_time())).inserted_primary_key[0]
        run_id = conn.execute(runs.insert().values(
            case_id=case_id, status='complete', owner='operator', created_at=utc_time())).inserted_primary_key[0]
        evidence_id = conn.execute(evidence.insert().values(
            run_id=run_id, report=report, digest='0' * 64)).inserted_primary_key[0]
    client.post(f'/evidence/{evidence_id}/review', headers=reviewer, json={'note': '核对无误'})

    _bandwidth(client, reviewer, 'OLD', 120_000_000)
    _bandwidth(client, reviewer, 'NORTH', 70_000_000)
    _bandwidth(client, reviewer, 'WEST', 50_000_000)
    response = _book(client, operator, evidence_id, *WINDOW)
    assert response.status_code == 201, response.text
    usage = {item['node_id']: item['bytes_per_hour'] for item in response.json()['node_usage']}
    assert usage == expected


def test_booking_rejects_bad_windows_and_missing_records(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 1_000)
    _bandwidth(client, reviewer, 'NORTH', 1_000)
    assert _book(client, operator, evidence_id, 'not-a-time', '2026-11-23T10:00:00+08:00').status_code == 422
    assert _book(client, operator, evidence_id, *reversed(WINDOW)).status_code == 422
    assert _book(client, operator, 999_999, *WINDOW).status_code == 404
    assert client.post('/evidence/999999/booking/reschedule', headers=operator,
                       json={'start_at': WINDOW[0], 'end_at': WINDOW[1]}).status_code == 404
    assert client.delete('/evidence/999999/booking', headers=operator).status_code == 404


def test_reschedule_without_an_active_booking_is_rejected(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 1_000)
    _bandwidth(client, reviewer, 'NORTH', 1_000)
    response = client.post(f'/evidence/{evidence_id}/booking/reschedule', headers=operator,
                           json={'start_at': WINDOW[0], 'end_at': WINDOW[1]})
    assert response.status_code == 404


def test_cancel_then_reschedule_is_rejected(client, operator, reviewer):
    evidence_id = _reviewed_evidence(client, operator, reviewer, [_row('OLD', 'NORTH', 100)])
    _bandwidth(client, reviewer, 'OLD', 1_000)
    _bandwidth(client, reviewer, 'NORTH', 1_000)
    _book(client, operator, evidence_id, *WINDOW)
    client.delete(f'/evidence/{evidence_id}/booking', headers=operator)
    response = client.post(f'/evidence/{evidence_id}/booking/reschedule', headers=operator,
                           json={'start_at': NEXT[0], 'end_at': NEXT[1]})
    assert response.status_code == 404
