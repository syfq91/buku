"""metadata provenance tracking

Revision ID: d44d0fbdcff2
Revises: ec4c39d9cd6a
Create Date: 2026-09-20 00:00:00.000000+00:00

Adds the per-field ``metadata_provenance`` table and seeds the
``metadata_sources`` registry (embedded, google_books, user, filename) so
Phase 6 enrichment can track where every book attribute came from.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d44d0fbdcff2"
down_revision: str | None = "ec4c39d9cd6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_provenance",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("field_name", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["books.id"],
            name=op.f("fk_metadata_provenance_book_id_books"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["metadata_sources.id"],
            name=op.f("fk_metadata_provenance_source_id_metadata_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_provenance")),
        sa.UniqueConstraint("book_id", "field_name", name="uq_book_field_provenance"),
    )
    with op.batch_alter_table("metadata_provenance", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_metadata_provenance_book_id"), ["book_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_metadata_provenance_source_id"), ["source_id"], unique=False
        )

    # Seed the provenance source registry.
    sources = sa.table(
        "metadata_sources",
        sa.column("name", sa.String(50)),
        sa.column("description", sa.String(255)),
    )
    op.bulk_insert(
        sources,
        [
            {"name": "embedded", "description": "Metadata extracted from the file itself"},
            {
                "name": "google_books",
                "description": "Metadata enriched from the Google Books API",
            },
            {"name": "user", "description": "Metadata manually edited by a user"},
            {"name": "filename", "description": "Metadata inferred from the file name"},
        ],
    )


def downgrade() -> None:
    with op.batch_alter_table("metadata_provenance", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_metadata_provenance_source_id"))
        batch_op.drop_index(batch_op.f("ix_metadata_provenance_book_id"))

    op.drop_table("metadata_provenance")
    op.execute(
        "DELETE FROM metadata_sources WHERE name IN ('embedded', 'google_books', "
        "'user', 'filename');"
    )
