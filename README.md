# buku

A lightweight, self-hosted, multi-user digital book server built in Python, designed specifically for home servers, NAS devices, and single-board computers (SBCs like Raspberry Pi).

---

## Highlights & Design Principles

- **Strictly Read-Only Media**: The server **never writes** to the media directory. Mount your library as read-only (`:ro`) with complete peace of mind.
- **Logical Book vs. Physical File**: A single logical `Book` can hold multiple physical formats (`EPUB`, `PDF`, `CBZ`). User metadata and reading progress attach to the logical book, not individual files.
- **Multi-User Isolation**: Independent user accounts (Argon2id hashing, server-side sessions) with per-user reading progression and permissions.
- **Zero Heavy Infrastructure**: Built with SQLite (SQLAlchemy + Alembic), SQLite FTS5 for full-text search, and an internal SQLite-backed job queue. **No Redis, Celery, or Elasticsearch required.**
- **OPDS & Reader Ecosystem**: Standard OPDS 1.2 catalog feeds and **OPDS Progression 1.0** synchronization for seamless reading across Web readers and external e-ink devices (KOReader, Moon+ Reader, Kobo).
- **X4-Optimized EPUB Representation**: On-demand generation and caching of lightweight EPUBs optimized for e-ink devices, strictly preserving locators (e.g. `chapter03.xhtml#p42`), spine, and table of contents.
- **Cross-Platform Release Gate**: Identical Python codebase, dependencies, and Docker images across `linux/amd64` and `linux/arm64`.

---

## Quick Start (Local Development)

### Prerequisites
- [uv](https://docs.astral.sh/uv/) (fast Python package manager)
- Python `>=3.14` (automatically provisioned by `uv` if needed)

### 1. Install & Sync Dependencies
```bash
# Clone the repository
git clone https://github.com/syfq91/buku.git
cd buku

# Install dependencies and create virtual environment
uv sync
```

### 2. Start the Server
```bash
# Start the web server on http://0.0.0.0:8080
uv run bookserver serve

# Alternatively, the 'buku' command alias is also available
uv run buku serve
```

### 3. Verify Server
In a new terminal:
```bash
curl http://localhost:8080/health
# Output: {"status":"ok","app":"buku","version":"0.1.0"}
```
Interactive API documentation is available at `http://localhost:8080/docs`.

---

## CLI Reference

`buku` includes a unified command-line tool accessible via `bookserver` or `buku`:

```text
Usage: bookserver [OPTIONS] COMMAND [ARGS]...

Options:
  --version  Show the version and exit.
  --help     Show this message and exit.

Commands:
  serve    Start the book server web application.
  scan     Scan library directories for new or modified books (Read-Only).
  migrate  Run database schema migrations.
```

### `bookserver serve`
Starts the Uvicorn-based FastAPI application:
```bash
# Run with custom host and port
uv run bookserver serve --host 127.0.0.1 --port 9000

# Run with development auto-reload
uv run bookserver serve --reload

# Run with custom config file
uv run bookserver serve --config ./config.toml
```

### `bookserver scan`
Scans and indexes media directories without modifying media files:
```bash
uv run bookserver scan --books-dir /path/to/books
```

### `bookserver migrate`
Executes database schema migrations:
```bash
uv run bookserver migrate
```

---

## Configuration

`buku` supports a layered configuration architecture evaluated in the following order of precedence (highest to lowest):

1. **CLI Flags** (e.g. `--host`, `--port`)
2. **Environment Variables** (`BUKU_*` or `BOOKSERVER_*`)
3. **TOML Configuration File** (`/config/config.toml`, `./config.toml`, or specified by `BUKU_CONFIG`)
4. **Built-in Defaults**

### Configuration File Example (`config.toml`)
```toml
[server]
host = "0.0.0.0"
port = 8080
debug = false

[paths]
config_dir = "/config"
books_dir = "/books"

[database]
url = "sqlite:////config/buku.db"
```

### Environment Variables

| Variable | Fallback Alias | Default | Description |
| :--- | :--- | :--- | :--- |
| `BUKU_HOST` | `BOOKSERVER_HOST` | `0.0.0.0` | Server bind host address |
| `BUKU_PORT` | `BOOKSERVER_PORT` | `8080` | Server bind port |
| `BUKU_DEBUG` | `BOOKSERVER_DEBUG` | `false` | Enable verbose logging |
| `BUKU_CONFIG_DIR` | `BOOKSERVER_CONFIG_DIR` | `/config` (or `./config`) | Path to configuration and database storage |
| `BUKU_BOOKS_DIR` | `BOOKSERVER_BOOKS_DIR` | `/books` (or `./books`) | Path to read-only media library |
| `BUKU_CONFIG` | `BOOKSERVER_CONFIG` | *None* | Path to explicit TOML configuration file |
| `BUKU_DATABASE_URL` | `BOOKSERVER_DATABASE_URL` | `sqlite:///{config_dir}/buku.db` | SQLAlchemy connection URL |

---

## Docker Deployment

`buku` provides a multi-platform, non-root container image designed to run with read-only volume mounts.

### `docker-compose.yml` Example
```yaml
services:
  bookserver:
    image: ghcr.io/syfq91/buku:latest
    container_name: buku
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./config:/config
      - /path/to/my/ebooks:/books:ro
    environment:
      - BUKU_HOST=0.0.0.0
      - BUKU_PORT=8080
```

---

## Development & Testing

All formatting, linting, type-checking, and test runner configurations are managed via `uv`:

```bash
# Check code formatting & linting
uv run ruff check .
uv run ruff format --check .

# Auto-format code
uv run ruff format .

# Strict static type checking
uv run mypy src tests

# Run test suite
uv run pytest
```

---

## Roadmap

Development follows the 28-phase blueprint detailed in [`plan.md`](plan.md):

- [x] **Phase 1**: Establish Project Foundation (CLI, FastAPI app, config system, uv, Dockerfile)
- [x] **Phase 2**: Database and Domain Model (SQLAlchemy & Alembic)
- [x] **Phase 3**: Authentication and Multi-User (Argon2id & session management)
- [x] **Phase 4**: Read-Only Library Scanner
- [x] **Phase 5**: Format Abstraction (EPUB, CBZ, PDF)
- [ ] **Phases 6–7**: Metadata Enrichment & Review UI
- [ ] **Phase 8**: SQLite FTS5 Search
- [ ] **Phase 9**: Jinja2 + HTMX Web UI
- [ ] **Phases 10–11**: Reading Progression & Browser EPUB Reader
- [ ] **Phases 12–13**: OPDS 1.2 & OPDS Progression 1.0 Sync
- [ ] **Phases 14–16**: Representation System & On-Demand X4 EPUB Optimization
- [ ] **Phases 17–18**: SQLite Job Queue & Cache System
- [ ] **Phases 19–20**: Admin Console & Unified REST API
- [ ] **Phases 21–28**: Security, Benchmarking, Docker Multi-Arch, CI/CD, and End-to-End Acceptance

---

## License

MIT
