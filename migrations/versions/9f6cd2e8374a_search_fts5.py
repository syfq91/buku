"""full-text search index (FTS5)

Revision ID: 9f6cd2e8374a
Revises: d44d0fbdcff2
Create Date: 2026-09-20 00:00:00.000000+00:00

Phase 8: creates the ``books_fts`` FTS5 virtual table over the catalog's
searchable fields and backfills it from existing books.

The index is a *stored* FTS5 table (not ``content='books'`` external content)
because several indexed fields — ``authors``, ``series``, ``isbn`` — live in
join tables rather than on ``books`` itself. Application code keeps it in sync
via :class:`buku.services.search.SearchService`; ``rowid`` always equals
``books.id``.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f6cd2e8374a"
down_revision: str | None = "d44d0fbdcff2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIRTUAL TABLE books_fts USING fts5(
            title,
            subtitle,
            authors,
            series,
            description,
            publisher,
            subjects,
            isbn,
            tags,
            tokenize = 'unicode61'
        )
        """
    )
    # Backfill the index with the current catalog. ``subjects`` and ``tags``
    # are reserved columns: the Book model does not persist them yet, so they
    # are indexed (empty) to keep the schema aligned with plan.md Phase 8.
    op.execute(
        """
        INSERT INTO books_fts(
            rowid, title, subtitle, authors, series, description,
            publisher, subjects, isbn, tags
        )
        SELECT
            b.id,
            b.title,
            b.subtitle,
            COALESCE(
                (SELECT group_concat(a.name, ', ')
                 FROM book_authors ba
                 JOIN authors a ON a.id = ba.author_id
                 WHERE ba.book_id = b.id),
                ''
            ),
            COALESCE((SELECT s.name FROM series s WHERE s.id = b.series_id), ''),
            b.description,
            b.publisher,
            '',
            COALESCE(
                (SELECT group_concat(i.identifier_value, ', ')
                 FROM book_identifiers i
                 WHERE i.book_id = b.id AND i.identifier_type IN ('isbn', 'isbn_10')),
                ''
            ),
            ''
        FROM books b
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS books_fts")
