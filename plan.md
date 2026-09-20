# AI Agent Work Plan: Lightweight Multi-User Book Server

## 0. Project Objective

Build a lightweight, self-hosted digital book server in Python with:

- **Multi-user accounts and login**
- **Read-only media/library directories**
- **EPUB, CBZ, and PDF support**
- **Embedded metadata extraction**
- **Online metadata enrichment**, initially Google Books
- **Web UI** (HTMX + Jinja2)
- **OPDS catalog feed**
- **OPDS Progression 1.0** synchronization
- **User-specific reading progress**
- **X4-optimized EPUB representation**
- **On-demand X4 generation and caching**
- **Linux x86-64 and ARM64 support** using the identical code and dependency set
- **Docker multi-platform deployment**
- **Native uv installation**
- **SQLite database** (with FTS5 full-text search)
- **Zero Redis / Celery requirement** (lightweight SQLite-backed jobs)

> [!NOTE]
> The application is specifically designed for home servers, NAS devices, and SBCs (Single Board Computers like Raspberry Pi).

---

## Phase 1: Establish Project Foundation

### Tasks

1. Create repository structure.
2. Configure `pyproject.toml`.
3. Configure `uv`.
4. Pin dependencies with `uv.lock`.
5. Set minimum supported Python version (`>=3.14`).
6. Configure formatting, linting, and type checking (`ruff`, `mypy`/`pyright`).
7. Configure `pytest`.
8. Create basic FastAPI application.
9. Create CLI entry points:
   ```bash
   bookserver serve
   bookserver scan
   bookserver migrate
   ```
10. Create configuration system supporting:
    - TOML configuration file
    - Environment variables
    - Docker standard paths
11. Establish `/config` and `/books` directory conventions.

### Acceptance Criteria

- [x] `uv sync` installs the project cleanly.
- [x] `uv run bookserver serve` starts the application.
- [x] Docker container starts the same application.
- [x] No code contains architecture checks such as `if platform.machine() == ...` unless absolutely unavoidable.

---

## Phase 2: Database and Domain Model

Implement SQLAlchemy models and Alembic migrations.

### Core Tables

- `users`
- `sessions`
- `libraries`
- `books`
- `book_files`
- `authors`
- `book_authors`
- `series` (and `books.series_id`)
- `reading_progress`
- `metadata_sources`
- `metadata_matches`
- `representations`
- `jobs`
- `collections`
- `collection_books`
- `book_identifiers` (for ISBN and external identifiers)

### Important Design Rule

> [!IMPORTANT]
> **A physical file is not the identity of a book.**

```text
Book (Logical Entity)
 ├── BookFile (EPUB)
 ├── BookFile (PDF)
 └── BookFile (CBZ)
```

### Acceptance Criteria

- [x] Fresh database can be created through Alembic migrations.
- [x] Database survives application restart.
- [x] Multiple physical files can belong to one logical book.
- [x] Progress belongs to composite key `(user_id, book_id)`.
- [x] Migrations run cleanly from an empty database.

---

## Phase 3: Authentication and Multi-User

### User System
Implement user records with:
- `username`
- `password_hash` (using Argon2id or modern equivalent)
- `display_name`
- `is_active` (active/inactive flag)
- `is_admin` (admin flag)
- `created_at` / `updated_at` timestamps

### Web Authentication
- Use secure server-side sessions.
- Endpoints:
  - `POST /login`
  - `POST /logout`
  - `POST /change-password`

### Authorization
- Create `AuthenticationService` and `AuthorizationService`.
- Do not scatter permission checks throughout routes.

### Roles & Permissions

| Role | Permissions |
| :--- | :--- |
| **ADMIN** | Manage users, configure libraries, trigger library scans, manage metadata, manage system settings. |
| **USER** | Browse, search, read, download, manage personal reading progress, manage personal collections. |

### Acceptance Criteria

- [x] Two users can log in simultaneously.
- [x] Each user maintains independent reading progress.
- [x] User A cannot access User B's private state.
- [x] Disabled users cannot authenticate.
- [x] Admin-only endpoints reject normal users with HTTP 403.

---

## Phase 4: Read-Only Library Scanner

Implement the read-only library scanner.

### Requirements
- **Strictly read-only:** The application must never write to media directories.
- Support `/books`, `/books/subdirectory`, and multiple configured library roots.

### Scanner Pipeline

```text
filesystem
    │
    ▼
discover files
    │
    ▼
detect format
    │
    ▼
compare size & mtime
    │
    ▼
hash changed files
    │
    ▼
parse metadata
    │
    ▼
identify / create Book
    │
    ▼
update BookFile
    │
    ▼
queue metadata enrichment
```

### Change Detection
Handle the following file states:
- **New file:** Register file, extract metadata, link or create book.
- **Modified file:** Re-hash, re-parse metadata, update records.
- **Deleted file:** Mark file as missing; **do not** immediately destroy book or user records.
- **Moved / Renamed file:** Detect via file hash matching and update path.
- **Unchanged file:** Skip processing based on mtime and file size.

### Acceptance Criteria

- [x] Scanning a library twice without changes performs essentially no expensive reprocessing.
- [x] The scanner functions properly when `/books` is mounted read-only (`:ro`).

---

## Phase 5: Format Abstraction

### Interface Definition

Create a common abstraction interface:

```python
class FormatHandler(ABC):
    @abstractmethod
    def detect(self, path: Path) -> bool: ...

    @abstractmethod
    def extract_metadata(self, path: Path) -> BookMetadata: ...

    @abstractmethod
    def extract_cover(self, path: Path) -> bytes | None: ...

    @abstractmethod
    def get_content(self, path: Path, identifier: str) -> BinaryIO: ...
```

### Supported Formats & Extraction

#### 1. EPUB
Extract:
- Title & Subtitle
- Authors
- Language
- Publisher & Publication Date
- ISBN & Identifiers
- Description
- Series & Series Index
- Cover image

#### 2. CBZ
Extract:
- Image list
- Page count
- Filename-derived metadata
- Cover image (first page)

#### 3. PDF
Extract:
- PDF document metadata
- Page count
- Cover / thumbnail image (first page)

### Acceptance Criteria

- [x] Malformed or corrupted files fail gracefully with logged warnings and do not crash the scanner.

---

## Phase 6: Metadata Architecture

### Providers Hierarchy

```text
MetadataProvider (Abstract)
    ├── GoogleBooksProvider
    └── [Future Providers: OpenLibrary, etc.]
```

### Internal Data Structures
- `MetadataQuery`
- `MetadataMatch`
- `BookMetadata`

### Matching Priority Flow
```text
ISBN
  │
  ▼
ISBN + other identifiers
  │
  ▼
Title + Author
  │
  ▼
Title
```
> [!WARNING]
> Avoid blindly applying fuzzy matches without high confidence thresholds.

### Metadata Provenance
Every imported attribute tracks its source:
- `embedded`
- `google_books`
- `user`
- `filename`

> [!IMPORTANT]
> User-edited metadata must always take precedence over automated enrichment.

### Acceptance Criteria

- [x] For a book containing an ISBN: query Google Books, find match, and populate missing metadata.
- [x] Existing user edits remain untouched during future rescans.

---

## Phase 7: Metadata Review UI

Create an admin interface for metadata matching and curation.

### Workflow

```text
Book ──► Metadata Matches ──► Review ──► Apply Selected / Missing Fields
```

### Supported Actions
- View current metadata side-by-side with external match.
- Apply all missing fields.
- Apply selectively chosen fields.
- Manually edit metadata fields.
- Reject match.
- Never automatically overwrite existing user metadata.

### Acceptance Criteria

- [x] Admin-only review endpoints expose pending matches with the current
      metadata side-by-side (`GET /api/v1/admin/metadata/review`,
      `GET /api/v1/admin/metadata/books/{book_id}`).
- [x] A review can apply all missing fields from a candidate without touching
      user-edited fields (`POST .../matches/{match_id}/apply-missing`).
- [x] A review can apply a hand-picked subset of candidate fields
      (`POST .../matches/{match_id}/apply-fields`).
- [x] Manual metadata edits are persisted with `user` provenance and protected
      from later automated enrichment (`PUT .../books/{book_id}`).
- [x] A candidate match can be rejected so it leaves the pending queue
      (`POST .../matches/{match_id}/reject`).
- [x] Applied fields are re-provenanced to their provider source; user edits
      are never automatically overwritten.

---

## Phase 8: Search

Implement SQLite FTS5 (Full-Text Search).

### Indexed Fields
- `title`
- `subtitle`
- `authors`
- `series`
- `description`
- `publisher`
- `subjects`
- `ISBN`
- `tags`

### Endpoints
- `GET /api/v1/search`
- `GET /search` (Web UI)
- Support pagination (`limit`, `offset`/`cursor`).

### Acceptance Criteria

- [x] A `books_fts` FTS5 virtual table indexes all nine fields — `title`,
      `subtitle`, `authors`, `series`, `description`, `publisher`, `subjects`,
      `ISBN`, `tags` — with `rowid` aligned to `books.id` and a `unicode61`
      tokenizer (`subjects`/`tags` are reserved columns until the metadata
      model persists them).
- [x] The index is created by the Phase 8 Alembic migration
      (`9f6cd2e8374a`) and backfilled from the existing catalog at upgrade.
- [x] `bookserver reindex` rebuilds the full index from the catalog on demand.
- [x] `SearchService` keeps the index in sync at every catalog mutation point:
      scanner book creation, metadata enrichment, and Phase 7 review curation
      — with no writes to the read-only media directory.
- [x] `GET /api/v1/search` supports `q`, `limit`, and `offset`, is available
      to any authenticated active user, and ranks results by FTS5 bm25
      relevance.
- [x] Query terms are sanitized (no FTS5 syntax injection) and prefix-matched
      so search-as-you-type works (`dun` finds `Dune`) and hyphenated ISBN
      lookups match.
- [x] `GET /search` serves the web UI route (placeholder page until the
      Phase 9 Jinja2/HTMX interface replaces it).
- [x] Search functions fast across 10,000+ books without requiring
      Elasticsearch or external services: benchmarked at < 1.5 ms per query
      against a 10,000-book index.

---

## Phase 9: Web UI

### Tech Stack
- **Templates:** Jinja2
- **Interactivity:** HTMX + lightweight CSS/JS
- Avoid React/Vue for v1 to keep runtime lightweight and simple.

### Routes

```text
/login
/
/dashboard
/books
/books/{id}
/series/{id}
/authors/{id}
/search
/reader/{id}
/collections
/settings
/admin/users
/admin/libraries
/admin/metadata
/admin/jobs
```

### Book Detail Page Elements
- Cover image
- Title, subtitle, authors, series
- Description / summary
- Metadata tags & publication details
- Available formats (EPUB, PDF, CBZ)
- Personal reading progress & "Continue Reading" button
- Download options

### Acceptance Criteria

- [x] Browser UI is built with Jinja2 templates + HTMX interactivity and
      bundled lightweight CSS/JS — no client-side framework.
- [x] All Phase 9 routes exist: `/login`, `/`, `/dashboard`, `/books`,
      `/books/{id}`, `/series/{id}`, `/authors/{id}`, `/search`,
      `/reader/{id}`, `/collections`, `/settings`, `/admin/users`,
      `/admin/libraries`, `/admin/metadata`, `/admin/jobs`.
- [x] Anonymous visitors are redirected to `/login` (HTTP 303) with a safe
      `next` target; already-signed-in users skip the login page.
- [x] The login page posts to the JSON auth endpoint and stores the
      server-side session cookie.
- [x] The book detail page shows cover, title, subtitle, authors, series,
      description, publication details (publisher, date, language, ISBN),
      available formats, per-user reading progress with a "Continue Reading"
      action, and download options.
- [x] `/books` supports title filtering, sorting, and pagination.
- [x] `/search` provides search-as-you-type via an HTMX fragment powered by
      the Phase 8 FTS5 `SearchService`.
- [x] The admin metadata review pages (queue, book review, apply
      missing/selected fields, reject, manual edit with `user` provenance)
      are reachable from the browser and delegate to the Phase 7 review
      service.
- [x] Downloads stream only from configured library roots: unknown files 404,
      missing files serve 410, and paths outside the library root are
      rejected.
- [x] `/reader/{id}` serves a reader shell page that the Phase 11 browser
      EPUB reader will replace.
- [x] Admin-only pages reject normal users by redirecting (303) to `/`.
- [x] Routes are thin transport layers: every page delegates data access to
      the `catalog`, `admin`, `auth`, `search`, and `metadata_review`
      services — nothing writes to the media directories.

---

## Phase 10: Reading & Progression Service

### Architecture
Central `ProgressionService` managing canonical progress states.

```text
ReadingProgress
├── user_id: int
├── book_id: int
├── progression: float      # (0.0 to 1.0)
├── href: str               # Chapter/resource href
├── fragment: str | None    # Anchor / DOM element locator
├── title: str | None       # Section title
├── modified_at: datetime   # UTC timestamp
├── device_id: str | None
└── device_name: str | None
```

### Rules & Invariants
- Progress belongs strictly to the **logical Book** (i.e. `user + Book`), not `user + EPUB` or `user + X4 EPUB`.
- **Conflict handling:** Use modification timestamps. If an incoming update timestamp is older than stored state, return `HTTP 409 Conflict`; otherwise apply update.

### Acceptance Criteria

- [x] A central `ProgressionService` is the single authority for reading
      progress: scans and every read/write (web dashboard, book detail page,
      and future reader/OPDS sync) go through it.
- [x] Progress rows are keyed by the composite `(user_id, book_id)` unique
      constraint — progress belongs to the logical Book, never a specific
      EPUB/X4 representation.
- [x] User A's progress is fully isolated from User B's: reading, writing,
      and listing never cross user boundaries.
- [x] `ProgressionService.update` creates or updates a record with the full
      Phase 10 shape — `progression` (0.0–1.0), `href`, `fragment`, `title`,
      `modified_at` (UTC), `device_id`, `device_name`.
- [x] Timestamp conflict resolution: an update strictly older than the stored
      `modified_at` is rejected (`HTTP 409` with stored + incoming snapshots);
      newer and equal timestamps are applied and the client timestamp becomes
      the stored `modified_at`.
- [x] Timestamps are normalized to aware UTC for comparison even though
      SQLite stores naive values, so mixed naive/aware client clocks resolve
      deterministically.
- [x] `progression` is clamped to `[0.0, 1.0]` and non-finite values are
      rejected; long locator/device fields are truncated to column limits.
- [x] `GET /api/v1/progress/{book_id}` and `PUT /api/v1/progress/{book_id}`
      expose progression to any authenticated user (404 on missing book,
      409 on stale writes) as thin transport layers over the service.

---

## Phase 11: Web EPUB Reader

Build an integrated browser reader.

### Requirements
- Open and render EPUB in-browser.
- Chapter navigation & Table of Contents (TOC).
- Previous / Next navigation controls.
- Progress indicator display.
- Resume reading from last stored position.
- Periodic background progress saving.
- Save on navigation and save on exit (beforeunload) where possible.
- Use the central `ProgressionService` identical to OPDS sync.

---

## Phase 12: OPDS 1.2 Catalog

Implement standard OPDS catalog feeds.

### Endpoints
- `GET /opds` (Root catalog)
- `GET /opds/books`
- `GET /opds/series`
- `GET /opds/authors`
- `GET /opds/search`

### Features
- HTTP Basic / Bearer authentication.
- Pagination links (`first`, `next`, `previous`, `last`).
- Acquisition links for supported formats.
- Thumbnail and cover image links.
- Embedded metadata (Atom feed entries).
- Clean separation between OPDS feed serializers and internal domain models.

---

## Phase 13: OPDS Progression 1.0

Implement progression endpoints conforming to the OPDS Progression 1.0 specification.

### Endpoints
- `GET /opds/progression/{book_id}`
- `PUT /opds/progression/{book_id}`

### Flow

```text
OPDS Progression API ──► ProgressionService ──► ReadingProgress Table
```

### Verification & Synchronization
- Web reader updates progress $\rightarrow$ verified via `GET /opds/progression/{book_id}`.
- External OPDS client sends `PUT` $\rightarrow$ Web reader resumes at new position.

---

## Phase 14: Representation System

Implement a generic representation layer for alternative book profiles.

```text
Representation
├── original
├── x4
└── [future profiles]
```

### Service Interface
Implement `RepresentationService`:
- `get(book_id, profile)`
- `generate(book_id, profile)`
- `exists(book_id, profile) -> bool`
- `invalidate(book_id, profile)`

---

## Phase 15: X4 Optimization

Integrate `epubkit` to produce lightweight, e-ink-optimized EPUB representations.

### Invariants & Preserved Elements
The X4 optimization pipeline must preserve:
- `META-INF`
- OPF package path
- XHTML filenames and internal IDs
- Anchors (`#locator`)
- Spine and reading order
- Table of Contents (NCX / Nav)
- CSS stylesheet references

> [!CRITICAL]
> **Locator Invariant:** Given a locator like `chapter03.xhtml#p42`, the X4 EPUB must retain that locator to guarantee cross-profile reading progress compatibility.

---

## Phase 16: On-Demand X4 OPDS

Expose X4 representations dynamically.

### Endpoints
- `GET /opds/x4`
- `GET /opds/x4/books`
- `GET /opds/x4/series`
- `GET /opds/x4/search`
- `GET /opds/x4/books/{id}.epub`

### Cache Strategy
- Storage: `/config/cache/x4/`
- Cache Key Composition:
  $$\text{CacheKey} = f(\text{book\_id}, \text{source\_hash}, \text{profile}, \text{optimizer\_version})$$
- Regenerate if source hash, profile, or optimizer version changes.
- Never write to or modify original media files.

---

## Phase 17: Background Job System

Implement an internal SQLite-backed asynchronous job queue (avoiding Redis and Celery).

### Job Schema
```text
jobs
├── id: UUID / int
├── type: str
├── status: queued | running | completed | failed
├── payload: JSON
├── created_at: datetime
├── started_at: datetime | None
├── finished_at: datetime | None
├── attempts: int
└── error: str | None
```

### Initial Job Types
- `scan_library`
- `extract_metadata`
- `metadata_lookup`
- `generate_cover`
- `generate_x4`

### Worker Concurrency Defaults
- 1 Library Scan worker
- 1 X4 Generation worker
- 2 Metadata Enrichment workers

---

## Phase 18: Cache Management

Disposable cache hierarchy located under `/config/cache/`:

```text
/config/cache/
├── covers/
├── metadata/
└── x4/
```

### Features
- Cache validation and checksum integrity.
- Manual and automated invalidation.
- Configurable maximum size limits.
- Eviction policy for stale representations.
- Database and original media files remain single source of truth.

---

## Phase 19: Admin System

Comprehensive web management console.

### Admin Sections
- **Libraries:** Add/remove library roots, trigger immediate scan.
- **Users:** Create, edit, disable, password reset, role assignment.
- **Metadata:** Review queue, provider settings, match approvals.
- **Jobs:** Active jobs status, execution logs, retry failed jobs.
- **Cache:** Inspect disk usage, purge covers/X4 caches.
- **Settings:** Server config, logging levels, session durations.

---

## Phase 20: API Cleanup

Establish uniform REST API architecture before declaring v1 complete.

### API Routes
- `/api/v1/auth/...`
- `/api/v1/books/...`
- `/api/v1/series/...`
- `/api/v1/authors/...`
- `/api/v1/search`
- `/api/v1/progress/...`
- `/api/v1/users/...`
- `/api/v1/collections/...`

> [!NOTE]
> Routes must act strictly as HTTP transport layers calling underlying service modules.

---

## Phase 21: Security Audit

Audit and test against the following vectors:

- [ ] SQL Injection (parameterized SQLAlchemy queries throughout).
- [ ] Path traversal attacks (e.g. `/books/../../config/database.sqlite`).
- [ ] Access control outside configured library roots.
- [ ] Malicious filenames in ZIP/EPUB/CBZ containers.
- [ ] Decompression bombs and oversized uploads.
- [ ] Unauthorized book downloads or metadata modification.
- [ ] Cross-user progress data leaks.
- [ ] Session fixation, CSRF, and timing attacks.
- [ ] Brute-force rate limiting on authentication endpoints.
- [ ] Secure OPDS credentials handling.

---

## Phase 22: Performance Testing

Benchmark system response across library scales:

| Scale Tier | Target Book Count | Key Benchmark Focus |
| :--- | :--- | :--- |
| **Tier 1** | 100 books | Baseline correctness & fast initial scan |
| **Tier 2** | 1,000 books | Incremental scan speed & web UI responsiveness |
| **Tier 3** | 10,000 books | FTS5 search index latency & OPDS pagination |
| **Tier 4** | 50,000 books | Memory footprint under modest NAS/SBC hardware |

### Performance Metrics to Track
- Initial library scan duration
- Incremental scan duration (no-op scans)
- Search query latency
- OPDS catalog browsing speed
- Book page rendering latency
- Concurrent user session handling

---

## Phase 23: Architecture Compatibility

Release gate requirement: identical behavior on `linux/amd64` and `linux/arm64`.

- Single Python codebase and unified `pyproject.toml`.
- Validate native binary dependencies on both platforms:
  - `Pillow`
  - `PyMuPDF`
  - EPUB processing tools
  - `epubkit`
- No architecture-conditional logic in application code.

---

## Phase 24: Docker Deployment

Multi-platform container image (`linux/amd64`, `linux/arm64`).

### Example `docker-compose.yml`

```yaml
services:
  bookserver:
    image: bookserver:latest
    container_name: bookserver
    ports:
      - "8080:8080"
    volumes:
      - ./config:/config
      - ./books:/books:ro
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=UTC
    restart: unless-stopped
```

### Requirements
- Non-root user execution inside container.
- Verification of read-only volume mount support (`./books:/books:ro`).

---

## Phase 25: Native `uv` Deployment

Validate standard commands work out of the box:

```bash
uv sync
uv run bookserver migrate
uv run bookserver scan
uv run bookserver serve
```

Document native setup steps for Linux amd64 and arm64.

---

## Phase 26: CI/CD Pipeline

GitHub Actions automated workflow:

- [ ] Code formatting check (`ruff format --check`)
- [ ] Linting (`ruff check`)
- [ ] Type checking (`mypy` / `pyright`)
- [ ] Unit & integration tests (`pytest`)
- [ ] EPUB extraction & locator tests
- [ ] OPDS 1.0 & OPDS Progression 1.0 tests
- [ ] Multi-user auth & isolation tests
- [ ] Multi-arch Docker build verification (`linux/amd64`, `linux/arm64`)

---

## Phase 27: Documentation

Comprehensive guides to be written in the repository:

| Document | Purpose |
| :--- | :--- |
| `README.md` | Overview, quickstart, key features, screenshot tour |
| `INSTALL.md` | Native `uv` installation and environment setup |
| `CONFIGURATION.md` | Configuration file reference, env variables, storage paths |
| `DOCKER.md` | Docker & Docker Compose setup, volume permissions, reverse proxy |
| `OPDS.md` | OPDS client setup (Kobo, KOReader, Moon+ Reader) & sync guide |
| `METADATA.md` | Metadata extraction details, Google Books provider setup |
| `DEVELOPMENT.md` | Contributing guide, testing suite, local development setup |
| `ARCHITECTURE.md` | Domain models, representation system, progression sync architecture |

---

## Phase 28: Final v1 Acceptance Test

The v1 release gate is validated when the complete end-to-end integration scenario passes:

- [ ] 1. Start clean Docker container.
- [ ] 2. Create initial admin account.
- [ ] 3. Add read-only `/books` library root.
- [ ] 4. Run library scanner.
- [ ] 5. Discover EPUB files.
- [ ] 6. Extract embedded metadata and covers.
- [ ] 7. Identify ISBN.
- [ ] 8. Query Google Books API.
- [ ] 9. Apply missing metadata fields.
- [ ] 10. Create second user account.
- [ ] 11. Log in as second user.
- [ ] 12. Browse book collection.
- [ ] 13. Open EPUB in Web Reader.
- [ ] 14. Save reading progress.
- [ ] 15. Access library via OPDS feed.
- [ ] 16. Download and open book in external reader.
- [ ] 17. OPDS client retrieves progression via OPDS Progression API.
- [ ] 18. Update progression from OPDS client.
- [ ] 19. Verify Web Reader reflects updated position.
- [ ] 20. Access `/opds/x4` feed.
- [ ] 21. Request X4-optimized EPUB.
- [ ] 22. Generate X4 representation on-demand.
- [ ] 23. Verify representation is cached under `/config/cache/x4/`.
- [ ] 24. Open X4 EPUB.
- [ ] 25. Verify locator compatibility (e.g. `chapter03.xhtml#p42`).
- [ ] 26. Verify progress still maps correctly to logical Book.
- [ ] 27. Restart Docker container.
- [ ] 28. Verify database, users, and progress persist across restart.
- [ ] 29. Verify `/books` directory was never modified.
- [ ] 30. Run identical acceptance verification suite on both `amd64` and `arm64`.
