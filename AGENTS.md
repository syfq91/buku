# AGENTS.md

> **Operational Guidelines and Architectural Invariants for AI Coding Agents**  
> Repository: `buku` (Lightweight, Self-Hosted Digital Book Server)  
> Master Roadmap: [`plan.md`](plan.md)

---

## 1. Project Mission & Identity

`buku` is a lightweight, self-hosted, multi-user digital book server written in Python. It is designed for home servers, NAS devices, and Single Board Computers (SBCs like Raspberry Pi) running on `linux/amd64` and `linux/arm64`.

The development of this project strictly follows the 28-phase blueprint defined in [`plan.md`](plan.md). Any AI agent working on this repository must read, understand, and honor the constraints and acceptance criteria outlined here.

---

## 2. Non-Negotiable Architectural Invariants

Every agent modifying or extending this codebase must adhere to the following non-negotiable rules:

### Rule 1: Strictly Read-Only Media Directory (`/books`)
- **Never write to media directories.** The server must never write, edit, delete, or create any file or folder in `/books`.
- The application must support read-only filesystem mounts (`:ro`).
- If a media file disappears or is moved, **do not** immediately delete database records or user annotations; mark the file state as missing explicitly and attempt re-linking via file hashes.

### Rule 2: Logical Book vs. Physical File Identity
- **A physical file is NOT the identity of a book.**
- A single logical `Book` entity can hold multiple physical formats (`BookFile`: EPUB, PDF, CBZ).
- User reading progress, personal collections, and metadata edits belong to the logical `Book`, never individual files or specific representations.

```text
Book (Logical Entity)
 ├── BookFile (EPUB)
 ├── BookFile (PDF)
 └── BookFile (CBZ)
```

### Rule 3: User-Isolated Reading Progression
- Progress belongs to composite key `(user_id, book_id)`.
- User A must never access or modify User B's reading state.
- Synchronization must support both the Web EPUB Reader and external e-readers via **OPDS Progression 1.0**.
- Timestamp conflict resolution: If an incoming progress update is older than the stored timestamp, return `HTTP 409 Conflict`.

### Rule 4: Zero Heavy External Infrastructure
- **Database**: SQLite with SQLAlchemy ORM and Alembic migrations.
- **Search**: SQLite FTS5 (Full-Text Search). No Elasticsearch.
- **Background Jobs**: Lightweight SQLite-backed job queue. **No Redis, Celery, or external brokers.**

### Rule 5: Cross-Architecture Release Gate (`amd64` & `arm64`)
- **Zero architecture checks.** Do not write code containing `if platform.machine() == ...` unless absolutely unavoidable.
- Maintain a single Python codebase, identical `pyproject.toml` dependencies, and a unified multi-arch Dockerfile for both `linux/amd64` and `linux/arm64`.

### Rule 6: Representation System & Locator Preservation
- Alternative formats (such as the e-ink optimized **X4** profile) are generated on-demand and cached under `/config/cache/x4/`. Original media is never modified.
- **Locator Invariant:** An X4 EPUB must preserve the internal structure (`META-INF`, OPF path, XHTML filenames, IDs, anchors, spine, TOC, and reading order). A locator like `chapter03.xhtml#p42` must resolve accurately across both original and X4 EPUB representations.

---

## 3. Environment & Tooling Standards

All development, dependency management, and testing must use `uv`. Never invoke `pip` or system `python` directly.

| Task | Command | Standard / Expectation |
| :--- | :--- | :--- |
| **Install & Sync** | `uv sync` | Clean virtual environment matching `uv.lock` |
| **Add Dependency** | `uv add <pkg>` / `uv add --dev <pkg>` | Ensure multi-platform wheels exist for Python 3.14 |
| **Linting** | `uv run ruff check .` | 0 errors |
| **Formatting** | `uv run ruff format --check .` | 100% compliant with Ruff formatter (line length: 100) |
| **Type Checking** | `uv run mypy src tests` | Strict mode (`strict = true`), 0 errors |
| **Test Suite** | `uv run pytest` | All unit & integration tests must pass |
| **Run CLI** | `uv run bookserver <cmd>` | Commands: `serve`, `scan`, `migrate` |

---

## 4. Codebase Architecture & Conventions

### Directory Layout
```text
.
├── src/
│   └── buku/
│       ├── __init__.py         # Package exports & version
│       ├── config.py           # Layered settings (Defaults < TOML < Env < CLI)
│       ├── app.py              # FastAPI factory & lifespan
│       ├── cli.py              # CLI commands (serve, scan, migrate)
│       ├── models/             # SQLAlchemy ORM models (Phase 2)
│       ├── services/           # Domain business logic (Auth, Progress, Representation)
│       ├── scanner/            # Read-only library scanner & format handlers
│       ├── metadata/           # Metadata providers & provenance tracking
│       └── opds/               # OPDS 1.2 catalog & Progression 1.0 feeds
├── tests/
│   ├── conftest.py             # Isolated fixtures & TestClient
│   ├── test_config.py          # Configuration & directory tests
│   ├── test_app.py             # Web API tests
│   └── test_cli.py             # CLI & architecture invariant tests
├── Dockerfile                  # Multi-arch, non-root container definition
├── pyproject.toml              # Build config, dependencies, tooling settings
├── plan.md                     # Master 28-phase roadmap & acceptance criteria
└── AGENTS.md                   # This document
```

### Path Conventions
- `/config`: Application configuration, SQLite database (`buku.db`), and cache directories (`/config/cache/{covers,metadata,x4}`).
- `/books`: Read-only media directory.
- Local fallback: When running locally without root `/config` and `/books`, the application gracefully falls back to `./config` and `./books`.

### Separation of Concerns: Thin Routes Rule
- FastAPI route handlers in `app.py` or router modules must act strictly as HTTP transport layers.
- All business logic, database queries, and transformation logic must reside in dedicated service classes (e.g. `AuthenticationService`, `ProgressionService`, `RepresentationService`).
- Routes validate inputs, call services, and serialize responses.

---

## 5. Security & Error Handling Policies

1. **Path Traversal Protection**:
   - Strict validation on all file paths. A request such as `/books/../../config/buku.db` must be rejected immediately.
   - All access must be constrained within the configured library roots.
2. **Safe Archive Parsing**:
   - When inspecting EPUB and CBZ ZIP archives, protect against zip bombs, decompression bombs, and malicious relative paths.
3. **Graceful Scanner Degradation**:
   - Malformed, corrupt, or unsupported files must log a clear warning and be skipped; they must **never** crash the library scanner.
4. **Metadata Provenance & User Precedence**:
   - Track provenance for every field (`embedded`, `google_books`, `user`, `filename`).
   - Automated rescans and enrichment jobs must **never overwrite user-edited metadata**.

---

## 6. Development Workflow for Subsequent Phases

When implementing any subsequent phase from [`plan.md`](plan.md):

1. **Review Phase Specifications**:
   - Inspect the corresponding section in [`plan.md`](plan.md).
   - Check data structures, endpoints, and acceptance criteria.
2. **Follow Planning Guidelines**:
   - In `/plan` mode, generate a detailed implementation plan artifact before editing code or running modifying commands.
   - Solicit user approval on design choices.
3. **Implement & Test**:
   - Implement models, services, and routes adhering to the architectural invariants.
   - Write comprehensive unit and integration tests in `tests/`.
4. **Enforce Verification Gate**:
   Before declaring work complete, run:
   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src tests
   uv run pytest
   ```
5. **Update Roadmap**:
   - Check off the completed phase's acceptance criteria in [`plan.md`](plan.md).
   - Update [`README.md`](README.md) roadmap and create a `walkthrough.md` artifact if required.
