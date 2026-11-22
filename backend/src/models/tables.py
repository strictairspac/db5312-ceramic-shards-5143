from sqlalchemy import (JSON, BigInteger, Column, ForeignKey, Integer, MetaData,
                        String, Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))

# Borrowable bandwidth per warehouse, in bytes per hour.
node_bandwidths = Table('node_bandwidths', schema,
    Column('node_id', String(64), primary_key=True),
    Column('bandwidth_bytes_per_hour', BigInteger, nullable=False),
    Column('updated_by', String(40), nullable=False),
    Column('updated_at', String(32), nullable=False))

# One active booking per reviewed manifest.  ``active_key`` is the evidence id
# while a booking is live and NULL once cancelled, so a cancelled manifest can
# be booked again while a live manifest can never hold two slots.
bookings = Table('manifest_bookings', schema,
    Column('id', Integer, primary_key=True),
    Column('evidence_id', ForeignKey('business_evidence.id'), nullable=False, index=True),
    Column('active_key', Integer, nullable=True),
    Column('start_at', String(32), nullable=False),
    Column('end_at', String(32), nullable=False),
    Column('duration_seconds', BigInteger, nullable=False),
    Column('node_bytes', JSON, nullable=False),
    Column('total_bytes', BigInteger, nullable=False),
    Column('status', String(20), nullable=False),
    Column('owner', String(40), nullable=False),
    Column('created_at', String(32), nullable=False),
    Column('updated_at', String(32), nullable=False),
    UniqueConstraint('active_key', name='uq_manifest_booking_active'))
