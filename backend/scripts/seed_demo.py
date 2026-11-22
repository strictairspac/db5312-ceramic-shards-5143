from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from src.config.settings import IS_MYSQL, engine
from src.constants.workflow import Role
from src.models.tables import (audit, cases, evidence, node_bandwidths, runs,
                               schema, users)
from src.services.domain import SPEC
from src.services.security import hash_password
from src.utils.time import utc_time


BANDWIDTHS = {
    'EAST': 90_000_000,
    'NORTH': 60_000_000,
    'WEST': 80_000_000,
    'OLD': 120_000_000,
}


def keep_existing(conn, table, key, values):
    if IS_MYSQL:
        statement = mysql_insert(table).values(**values).on_duplicate_key_update(**{key: table.c[key]})
    else:
        statement = postgres_insert(table).values(**values).on_conflict_do_nothing(index_elements=[key])
    conn.execute(statement)


def seed():
    with engine.begin() as conn:
        for role in Role:
            keep_existing(conn, users, 'username', {'username': role.value, 'role': role.value,
                'password_hash': hash_password('Demo-Walkthrough-2026')})
        keep_existing(conn, cases, 'slug', {'slug': 'demo', 'title': SPEC['name'],
            'payload': SPEC['sample'], 'author': 'demo', 'created_at': utc_time()})
        for node_id, rate in BANDWIDTHS.items():
            keep_existing(conn, node_bandwidths, 'node_id', {'node_id': node_id,
                'bandwidth_bytes_per_hour': rate, 'updated_by': 'demo', 'updated_at': utc_time()})
        return {table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (users, cases, runs, evidence, audit, node_bandwidths)}


if __name__ == '__main__':
    schema.create_all(engine)
    print(seed())
