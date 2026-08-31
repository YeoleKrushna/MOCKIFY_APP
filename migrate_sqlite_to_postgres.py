"""One-way, non-destructive SQLite-to-PostgreSQL data importer for Mockify."""

import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


# =========================================================
# CONFIG
# =========================================================

SOURCE = Path(
    os.environ.get(
        "SQLITE_DATABASE_PATH",
        "instance/mockify.db"
    )
).resolve()

TARGET_URL = os.environ.get("DATABASE_URL", "").strip()

if TARGET_URL.startswith("postgres://"):
    TARGET_URL = "postgresql://" + TARGET_URL[len("postgres://"):]

if not TARGET_URL or not TARGET_URL.startswith("postgresql"):
    raise SystemExit(
        "Set DATABASE_URL to the target PostgreSQL connection string."
    )

if not SOURCE.is_file():
    raise SystemExit(
        f"SQLite source database not found: {SOURCE}"
    )


# =========================================================
# ENGINES
# =========================================================

source = create_engine(
    f"sqlite:///{SOURCE}",
    future=True,
)

target = create_engine(
    TARGET_URL,
    future=True,
)


# =========================================================
# TABLES
# =========================================================

TABLES = (
    "users",
    "pending_otps",
    "otp_events",
    "mocks",
    "results",
)


# =========================================================
# HELPERS
# =========================================================

BOOLEAN_COLUMNS = {
    "users": {
        "email_verified",
        "is_admin",
        "is_super_admin",
    },
}


def normalize_value(table_name, column_name, value):
    """Convert SQLite values to PostgreSQL-compatible values."""

    if value is None:
        return None

    if column_name in BOOLEAN_COLUMNS.get(table_name, set()):
        return bool(value)

    return value


def quote_identifier(name):
    """Safely quote a database identifier."""
    return '"' + name.replace('"', '""') + '"'


# =========================================================
# MIGRATION
# =========================================================

with source.connect() as source_conn:

    source_inspector = inspect(source)

    with target.begin() as target_conn:

        target_inspector = inspect(target)

        # -------------------------------------------------
        # Verify all target tables exist.
        # -------------------------------------------------

        missing_target_tables = [
            table
            for table in TABLES
            if source_inspector.has_table(table)
            and not target_inspector.has_table(table)
        ]

        if missing_target_tables:
            raise SystemExit(
                "Target PostgreSQL database is missing tables: "
                + ", ".join(missing_target_tables)
                + "\nRun the application/schema initialization first."
            )

        counts = {}

        # -------------------------------------------------
        # Copy each table.
        # -------------------------------------------------

        for table in TABLES:

            if not source_inspector.has_table(table):
                counts[table] = 0
                continue

            source_columns = {
                column["name"]
                for column in source_inspector.get_columns(table)
            }

            target_columns = {
                column["name"]
                for column in target_inspector.get_columns(table)
            }

            columns = sorted(
                source_columns & target_columns
            )

            if not columns:
                counts[table] = 0
                continue

            quoted_columns = ", ".join(
                quote_identifier(column)
                for column in columns
            )

            rows = source_conn.execute(
                text(
                    f"""
                    SELECT {quoted_columns}
                    FROM {quote_identifier(table)}
                    """
                )
            ).mappings().all()

            if not rows:
                counts[table] = 0
                continue

            # -------------------------------------------------
            # Target table must be empty.
            # -------------------------------------------------

            existing_count = target_conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM {quote_identifier(table)}
                    """
                )
            ).scalar_one()

            if existing_count:
                raise SystemExit(
                    f"Target table '{table}' is not empty. "
                    "Use a fresh/empty Neon database for this migration."
                )

            # -------------------------------------------------
            # Normalize values.
            # -------------------------------------------------

            normalized_rows = []

            for row in rows:

                normalized = {}

                for column in columns:
                    normalized[column] = normalize_value(
                        table,
                        column,
                        row[column],
                    )

                normalized_rows.append(normalized)

            # -------------------------------------------------
            # Insert rows.
            # -------------------------------------------------

            placeholders = ", ".join(
                f":{column}"
                for column in columns
            )

            insert_sql = text(
                f"""
                INSERT INTO {quote_identifier(table)}
                ({quoted_columns})
                VALUES ({placeholders})
                """
            )

            target_conn.execute(
                insert_sql,
                normalized_rows,
            )

            counts[table] = len(normalized_rows)

        # -------------------------------------------------
        # Restore PostgreSQL sequences.
        # -------------------------------------------------

        for table in TABLES:

            if not target_inspector.has_table(table):
                continue

            # Only tables with an id column need this.
            target_columns = {
                column["name"]
                for column in target_inspector.get_columns(table)
            }

            if "id" not in target_columns:
                continue

            sequence_name = target_conn.execute(
                text(
                    """
                    SELECT pg_get_serial_sequence(:table_name, 'id')
                    """
                ),
                {
                    "table_name": table,
                },
            ).scalar_one_or_none()

            if not sequence_name:
                continue

            target_conn.execute(
                text(
                    """
                    SELECT setval(
                        :sequence_name,
                        COALESCE(
                            (
                                SELECT MAX(id)
                                FROM
                                    __TABLE_PLACEHOLDER__
                            ),
                            1
                        ),
                        true
                    )
                    """.replace(
                        "__TABLE_PLACEHOLDER__",
                        quote_identifier(table),
                    )
                ),
                {
                    "sequence_name": sequence_name,
                },
            )


# =========================================================
# RESULT
# =========================================================

print(
    "Migration complete. Rows imported: "
    + ", ".join(
        f"{name}={count}"
        for name, count in counts.items()
    )
)