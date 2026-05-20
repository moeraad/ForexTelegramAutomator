"""Time-bounded diagnostics-bundle exporter.

Produces a single ZIP containing every log / DB extract / MT5 log the
operator needs to triage an incident, scoped to a `[start, end]`
window so the bundle stays small and focused.

Sources (all filtered to the window unless noted):

- `logs/*.log{,*.1..5}` — line-by-line filter on the leading
  `%(asctime)s` timestamp; multi-line tracebacks stay glued to their
  parent timestamped line so a stack trace whose first line is in
  range survives intact.
- `logs/ai_calls.jsonl{,.1..5}` — line-by-line filter on the `ts`
  field that `ai_logger.log_call` stamps on every record.
- `logs/nssm-*.{out,err}.log` — same Python log format, same parser.
- ``service-*-crash.log`` (per-target) from the operator's
  ``%APPDATA%/CopyTrades`` AND from the LocalSystem profile
  (``.../config/systemprofile/AppData/Roaming/CopyTrades``).
  Block-delimited by ``========`` — whole block kept when its
  ``timestamp:`` is in range.
- `copytrades.db` — sanitized copy with every row in
  `db_settings.SECRET_KEYS` value-NULLed; per-table JSON dump
  (`actions`, `positions`, `messages`, `signal_memory`) WHERE-clauses
  on the right timestamp column per table.
- MT5 Terminal + Experts logs — file name IS the date; pick days
  overlapping the window. Auto-detected by walking
  ``%APPDATA%/MetaQuotes/Terminal/*/MQL5/Experts`` for a
  ``CopyTrades*`` file. MT5 logs are left in broker server time
  (noted in manifest).

Secrets policy: every key listed in `db_settings.SECRET_KEYS` is
blanked in the sanitized DB copy AND in the manifest's profile dump.
The manifest records what was redacted so a recipient can tell
"missing" from "redacted".
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from src.gui.services.stack_registry import Stack

log = logging.getLogger(__name__)


# Tables to dump as JSON, keyed by the timestamp column used to scope
# each row to the export window. Tables not listed here are dropped
# from the per-table dump (still survive in the sanitized DB copy).
_DB_TABLES_WITH_TS: dict[str, str] = {
    "actions":       "created_at",
    "positions":     "opened_at",
    "messages":      "received_at",
    "signal_memory": "created_at",
}

# Maximum bytes we'll read into memory from a single source file.
# Files larger than this get streamed line-by-line; the cap is just
# to bound peak memory on pathological cases (>500MB rotated logs).
_PER_FILE_BUDGET_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ExportOptions:
    """User-chosen export window + toggles. Built in the dialog,
    consumed by `collect_diagnostics`."""
    stack: Stack
    start: datetime
    end: datetime
    include_mt5: bool
    sanitize_db: bool
    output_zip: Path
    mt5_terminal_dir: Path | None = None


@dataclass(frozen=True)
class ExportResult:
    """Returned to the dialog when the worker finishes. The path is
    suitable for surfacing in a "Open folder" affordance after success."""
    output_zip: Path
    files_written: int
    bytes_written: int
    skipped_sources: list[str]


# ---- Public entry point --------------------------------------------------


def collect_diagnostics(
    opts: ExportOptions,
    progress: Callable[[str, int, int], None] | None = None,
) -> ExportResult:
    """Build the diagnostics ZIP. `progress(stage, current, total)` is
    invoked on each stage transition so the dialog can render its bar.
    """
    def _bump(stage: str, cur: int, tot: int) -> None:
        if progress is not None:
            progress(stage, cur, tot)

    opts.output_zip.parent.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []
    files_written = 0
    bytes_written = 0

    with zipfile.ZipFile(
        opts.output_zip, "w",
        compression=zipfile.ZIP_DEFLATED, compresslevel=6,
    ) as zf:
        # 1. CopyTrades logs (line-filtered).
        _bump("logs", 0, 1)
        for arcname, content in _collect_logs(opts):
            if content is None:
                skipped.append(arcname)
                continue
            zf.writestr(arcname, content)
            files_written += 1
            bytes_written += len(content)
        _bump("logs", 1, 1)

        # 2. Service crash logs (block-filtered).
        _bump("crashlogs", 0, 1)
        for arcname, content in _collect_crashlogs(opts):
            if content is None:
                skipped.append(arcname)
                continue
            zf.writestr(arcname, content)
            files_written += 1
            bytes_written += len(content)
        _bump("crashlogs", 1, 1)

        # 3. DB — sanitized copy + per-table window dump.
        _bump("db", 0, 1)
        for arcname, content in _collect_db(opts):
            if content is None:
                skipped.append(arcname)
                continue
            zf.writestr(arcname, content)
            files_written += 1
            bytes_written += len(content)
        _bump("db", 1, 1)

        # 4. MT5 logs (date-name-filtered).
        if opts.include_mt5:
            _bump("mt5", 0, 1)
            for arcname, content in _collect_mt5_logs(opts):
                if content is None:
                    skipped.append(arcname)
                    continue
                zf.writestr(arcname, content)
                files_written += 1
                bytes_written += len(content)
            _bump("mt5", 1, 1)

        # 5. Manifest (last so it can record what made it in).
        manifest = _build_manifest(opts, files_written, bytes_written, skipped)
        zf.writestr("manifest.txt", manifest.encode("utf-8"))
        files_written += 1
        bytes_written += len(manifest)

    return ExportResult(
        output_zip=opts.output_zip,
        files_written=files_written,
        bytes_written=bytes_written,
        skipped_sources=skipped,
    )


# ---- Auto-detection helpers ---------------------------------------------


@dataclass(frozen=True)
class Mt5Terminal:
    """One MT5 install discovered under %APPDATA%/MetaQuotes/Terminal/.

    The combobox in the dialog renders these; the operator picks which
    one (or none) to bundle logs from. Auto-detect — the install whose
    `MQL5/Experts/` carries a `CopyTrades*` file — is just the
    pre-selection hint, not a hard gate, so a different install can be
    chosen explicitly.
    """
    terminal_id: str          # folder name (32-char hex hash)
    path: Path                # full path to the terminal folder
    has_copytrades_ea: bool   # MQL5/Experts/ contains CopyTrades*
    last_modified: datetime   # most-recent mtime under the logs dirs
    log_file_count: int       # how many *.log files in MQL5/Logs (incl. Experts)


def enumerate_mt5_terminals() -> list[Mt5Terminal]:
    """List every MT5 install under %APPDATA%/MetaQuotes/Terminal/.

    Ordered: installs with our EA first (most likely the right one),
    then by most-recently-active. Empty list when no MetaQuotes folder
    exists. `Common` / `Community` / `Help` siblings are skipped —
    they're not terminal-id folders.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    root = Path(appdata) / "MetaQuotes" / "Terminal"
    if not root.exists():
        return []
    out: list[Mt5Terminal] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        # Terminal-id folders are 32-char hex hashes; siblings like
        # Common / Community / Help have plain names. Filter so the
        # dropdown only shows real terminals.
        if not (len(entry.name) == 32 and all(c in "0123456789ABCDEFabcdef" for c in entry.name)):
            continue
        logs_root = entry / "MQL5" / "Logs"
        experts_dir = entry / "MQL5" / "Experts"
        has_ea = False
        if experts_dir.exists():
            has_ea = any(experts_dir.glob("CopyTrades*"))
        log_files: list[Path] = []
        if logs_root.exists():
            log_files = list(logs_root.glob("*.log")) + list(
                (logs_root / "Experts").glob("*.log")
                if (logs_root / "Experts").exists() else []
            )
        if log_files:
            last_mod = datetime.fromtimestamp(
                max(p.stat().st_mtime for p in log_files)
            )
        else:
            # Fall back to the terminal folder's own mtime so the sort
            # order still works for fresh installs with no logs yet.
            last_mod = datetime.fromtimestamp(entry.stat().st_mtime)
        out.append(Mt5Terminal(
            terminal_id=entry.name,
            path=entry,
            has_copytrades_ea=has_ea,
            last_modified=last_mod,
            log_file_count=len(log_files),
        ))
    # Sort: EA-bearing first, then most-recently-active.
    out.sort(key=lambda t: (not t.has_copytrades_ea, -t.last_modified.timestamp()))
    return out


def detect_mt5_terminal() -> Path | None:
    """Backward-compat shim: pick the best candidate or None.

    Returns the first EA-bearing terminal if any, else the most-
    recently-active terminal, else None. Newer callers should use
    `enumerate_mt5_terminals()` and let the operator pick.
    """
    terminals = enumerate_mt5_terminals()
    if not terminals:
        return None
    return terminals[0].path


def estimate_bundle_size(opts: ExportOptions) -> int:
    """Cheap pre-flight estimate of the bundle's uncompressed size.

    Sums on-disk size of every file whose mtime overlaps the window;
    actual compressed size will be a fraction of this. Used by the
    dialog to warn the operator before huge exports.
    """
    total = 0
    for path in _candidate_log_paths(opts.stack):
        try:
            st = path.stat()
        except OSError:
            continue
        if _mtime_in_range(st.st_mtime, opts.start, opts.end):
            total += st.st_size
    db = opts.stack.db_path
    if db.exists():
        total += db.stat().st_size  # sanitized copy is ~same size
    if opts.include_mt5 and opts.mt5_terminal_dir:
        for path in _mt5_log_files(opts.mt5_terminal_dir, opts.start, opts.end):
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


# ---- Log file collection ------------------------------------------------


def _logs_dir(stack: Stack) -> Path:
    """Each stack writes logs alongside its DB."""
    return stack.db_path.parent / "logs"


def _candidate_log_paths(stack: Stack) -> Iterable[Path]:
    """Yield every log file we'd consider for the bundle, regardless of
    range. The range filter applies later."""
    d = _logs_dir(stack)
    if not d.exists():
        return
    yield from d.glob("*.log")
    yield from d.glob("*.log.*")  # rotations: foo.log.1, foo.log.2 …
    yield from d.glob("*.jsonl")
    yield from d.glob("*.jsonl.*")


def _collect_logs(opts: ExportOptions) -> Iterable[tuple[str, bytes | None]]:
    """Yield (arcname, content) for every log file overlapping the window."""
    for path in _candidate_log_paths(opts.stack):
        try:
            st = path.stat()
        except OSError as e:
            yield f"logs/{path.name}", None
            log.warning("diagnostics: stat failed for %s: %s", path, e)
            continue
        if not _mtime_in_range(st.st_mtime, opts.start, opts.end):
            continue
        if st.st_size > _PER_FILE_BUDGET_BYTES:
            log.warning(
                "diagnostics: %s exceeds per-file budget (%d > %d), skipping",
                path, st.st_size, _PER_FILE_BUDGET_BYTES,
            )
            yield f"logs/{path.name}", None
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            yield f"logs/{path.name}", None
            log.warning("diagnostics: read failed for %s: %s", path, e)
            continue
        if path.suffix.startswith(".jsonl") or path.name.endswith(".jsonl") or ".jsonl." in path.name:
            filtered = _filter_jsonl(text, opts.start, opts.end)
        else:
            filtered = _filter_text_log(text, opts.start, opts.end)
        if not filtered:
            continue
        yield f"logs/{path.name}", filtered.encode("utf-8")


# ---- Text log parser ----------------------------------------------------


# Matches the configure_logging default: "2026-05-19 14:30:25,123 ..."
# Captures the leading timestamp; trailing comma-millis optional.
import re as _re

_TS_PREFIX_RE = _re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,.](\d{1,6}))?"
)


def _parse_log_ts(line: str) -> datetime | None:
    """Parse the leading timestamp of one log line. Returns None for
    continuation lines (tracebacks, multi-line messages) that lack a
    leading timestamp — caller treats those as part of the previous
    timestamped block."""
    m = _TS_PREFIX_RE.match(line)
    if not m:
        return None
    base = m.group(1).replace("T", " ")
    try:
        dt = datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # configure_logging writes local-naive timestamps via logging's
    # default; treat as naive and compare against naive bounds the
    # caller derived from `datetime.now()`. UTC tagging happens in
    # ai_logger.jsonl which we filter separately by the `ts` field.
    return dt


def _filter_text_log(text: str, start: datetime, end: datetime) -> str:
    """Keep multi-line blocks whose first line's timestamp falls inside
    [start, end]. Lines without a timestamp prefix (traceback bodies,
    continuations) stay attached to the preceding timestamped line —
    so a Python traceback whose first line is in range survives whole.

    Inputs without ANY parseable timestamp lines fall through with the
    full content preserved — better to over-include than to silently
    drop a log we couldn't parse (e.g. EA logs in a different format).
    """
    start_n = _to_naive(start)
    end_n = _to_naive(end)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    block: list[str] = []
    block_ts: datetime | None = None
    saw_any_ts = False

    def _flush() -> None:
        if block and block_ts is not None and start_n <= block_ts <= end_n:
            out.extend(block)

    for line in lines:
        ts = _parse_log_ts(line)
        if ts is not None:
            saw_any_ts = True
            _flush()
            block = [line]
            block_ts = ts
        else:
            block.append(line)
    _flush()
    if not saw_any_ts:
        # Don't know how to parse this log format; include everything
        # rather than silently dropping it.
        return text
    return "".join(out)


def _filter_jsonl(text: str, start: datetime, end: datetime) -> str:
    """Filter ai_calls.jsonl-style file: one JSON object per line, each
    with a `ts` field stamped by ai_logger.log_call. Lines without `ts`
    are included (defensive — better to over-include)."""
    start_iso = start.astimezone(timezone.utc).isoformat()
    end_iso = end.astimezone(timezone.utc).isoformat()
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            out.append(line)  # malformed entry — include for forensics
            continue
        ts = obj.get("ts") or obj.get("timestamp")
        if not isinstance(ts, str):
            out.append(line)
            continue
        if start_iso <= ts <= end_iso:
            out.append(line)
    return "".join(out)


# ---- Crash-log collection (block-delimited) -----------------------------


def _crashlog_paths() -> Iterable[tuple[str, Path]]:
    """Yield (label, path) for every per-target crashlog we should
    consider. `gui_launcher._write_service_crashlog` writes to
    %APPDATA%\\CopyTrades, but when the listener runs as LocalSystem
    that resolves to the systemprofile path instead. Check both."""
    appdata = os.environ.get("APPDATA")
    candidates = []
    if appdata:
        candidates.append(("user", Path(appdata) / "CopyTrades"))
    candidates.append((
        "localsystem",
        Path(r"C:\Windows\System32\config\systemprofile\AppData\Roaming\CopyTrades"),
    ))
    for label, base in candidates:
        if not base.exists():
            continue
        for target in ("api", "bot", "listener"):
            path = base / f"service-{target}-crash.log"
            if path.exists():
                yield f"service-crashlogs/{label}-{target}.log", path


_CRASH_BLOCK_SEP = "=" * 72


def _filter_crashlog(text: str, start: datetime, end: datetime) -> str:
    """Each crash block starts with the `========` separator and has a
    `timestamp: <ISO>` line right under it. Keep blocks whose timestamp
    is in range."""
    start_iso = start.astimezone(timezone.utc).isoformat()
    end_iso = end.astimezone(timezone.utc).isoformat()
    blocks: list[str] = []
    current: list[str] = []
    current_ts: str | None = None

    def _flush() -> None:
        if not current:
            return
        if current_ts and start_iso <= current_ts <= end_iso:
            blocks.append("".join(current))

    for line in text.splitlines(keepends=True):
        if line.startswith(_CRASH_BLOCK_SEP):
            _flush()
            current = [line]
            current_ts = None
        elif line.startswith("timestamp:"):
            current.append(line)
            # `timestamp:   2026-05-19T14:30:25.123Z`
            parts = line.split(":", 1)
            if len(parts) == 2:
                ts = parts[1].strip().rstrip("Z").rstrip()
                current_ts = ts + "+00:00" if "+" not in ts else ts
        else:
            current.append(line)
    _flush()
    return "".join(blocks)


def _collect_crashlogs(opts: ExportOptions) -> Iterable[tuple[str, bytes | None]]:
    for arcname, path in _crashlog_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            yield arcname, None
            log.warning("diagnostics: crashlog read failed %s: %s", path, e)
            continue
        filtered = _filter_crashlog(text, opts.start, opts.end)
        if not filtered:
            continue
        yield arcname, filtered.encode("utf-8")


# ---- DB collection (sanitized copy + per-table dump) --------------------


def _collect_db(opts: ExportOptions) -> Iterable[tuple[str, bytes | None]]:
    db = opts.stack.db_path
    if not db.exists():
        yield "db/copytrades.db", None
        return

    # Sanitized copy: copy the file, open it, NULL secrets, ship it.
    if opts.sanitize_db:
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp(prefix="ct-diag-"))
        try:
            tmp_db = tmp_dir / "copytrades.db"
            shutil.copy2(db, tmp_db)
            _sanitize_db_file(tmp_db)
            yield "db/copytrades.db", tmp_db.read_bytes()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        yield "db/copytrades.db", db.read_bytes()

    # Per-table JSON dump, windowed.
    try:
        dump = _dump_tables(db, opts.start, opts.end)
        yield "db/tables.json", json.dumps(dump, indent=2, default=str).encode("utf-8")
    except sqlite3.Error as e:
        log.warning("diagnostics: per-table dump failed: %s", e)
        yield "db/tables.json", None


def _sanitize_db_file(path: Path) -> None:
    """Blank every row whose `key` is in SECRET_KEYS so the shipped
    copy can't leak credentials. Matches the same set the encrypted
    `get_str` path consults — keep them in sync if SECRET_KEYS
    changes.

    The CopyTrades DB runs in WAL journal mode. `shutil.copy2`
    duplicates only the main `.db` file, not the `.db-wal` /
    `.db-shm` sidecars, so the freshly-opened copy creates its own
    WAL. Without forcing the WAL contents back into the main file
    before we `read_bytes()`, the UPDATE we just committed sits in
    the WAL — invisible to a raw-bytes reader, which means secrets
    survive in the shipped DB.

    Two-step fix:
      1. `PRAGMA journal_mode=DELETE` switches the copy to rollback-
         journal mode where the main file is canonical after commit.
      2. `PRAGMA wal_checkpoint(TRUNCATE)` flushes any pre-existing
         WAL contents that copy might have inherited. Order matters:
         checkpoint before switching modes so we don't lose data.
    """
    from src.db_settings import SECRET_KEYS
    placeholders = ",".join("?" * len(SECRET_KEYS))
    conn = sqlite3.connect(str(path))
    try:
        # Force any inherited WAL contents into the main file, then
        # switch the copy to rollback-journal mode so subsequent
        # writes go directly into the main file we're about to read.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            f"UPDATE settings SET value = '' WHERE key IN ({placeholders})",
            tuple(SECRET_KEYS),
        )
        conn.commit()
    finally:
        conn.close()


def _dump_tables(
    db_path: Path, start: datetime, end: datetime,
) -> dict[str, list[dict]]:
    """One JSON dump of the four operationally-interesting tables,
    scoped to the export window via per-table timestamp WHEREs."""
    start_iso = _to_naive(start).isoformat()
    end_iso = _to_naive(end).isoformat()
    out: dict[str, list[dict]] = {}
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for table, ts_col in _DB_TABLES_WITH_TS.items():
            try:
                rows = conn.execute(
                    f"SELECT * FROM {table} "
                    f"WHERE {ts_col} BETWEEN ? AND ? "
                    f"ORDER BY {ts_col}",
                    (start_iso, end_iso),
                ).fetchall()
            except sqlite3.OperationalError:
                # Table or column missing — older DB schema, skip.
                continue
            out[table] = [dict(r) for r in rows]
    return out


# ---- MT5 log collection -------------------------------------------------


def _mt5_log_files(terminal_dir: Path, start: datetime, end: datetime) -> list[Path]:
    """Return every MT5 terminal + experts log file whose filename-date
    falls in the window. MT5 names logs YYYYMMDD.log, one per day."""
    out: list[Path] = []
    for sub in (terminal_dir / "MQL5" / "Logs",
                terminal_dir / "MQL5" / "Logs" / "Experts"):
        if not sub.exists():
            continue
        for p in sub.glob("*.log"):
            stem = p.stem  # "20260519"
            if len(stem) != 8 or not stem.isdigit():
                continue
            try:
                d = datetime.strptime(stem, "%Y%m%d")
            except ValueError:
                continue
            # Inclusive: a file dated D is in range if D's day overlaps
            # [start, end]. Compare on date only.
            if start.date() <= d.date() <= end.date():
                out.append(p)
    return out


def _collect_mt5_logs(opts: ExportOptions) -> Iterable[tuple[str, bytes | None]]:
    if not opts.mt5_terminal_dir:
        return
    for path in _mt5_log_files(opts.mt5_terminal_dir, opts.start, opts.end):
        rel = path.relative_to(opts.mt5_terminal_dir / "MQL5" / "Logs")
        arcname = "mt5/" + str(rel).replace("\\", "/")
        try:
            yield arcname, path.read_bytes()
        except OSError as e:
            yield arcname, None
            log.warning("diagnostics: MT5 log read failed %s: %s", path, e)


# ---- Manifest -----------------------------------------------------------


def _build_manifest(
    opts: ExportOptions,
    files_written: int,
    bytes_written: int,
    skipped: list[str],
) -> str:
    """Plain-text manifest. Plain text (not JSON) because operators
    paste this into bug reports and want to read it without a viewer."""
    import platform
    from src.db_settings import SECRET_KEYS

    profile_summary = _profile_summary(opts.stack)
    service_states = _service_states(opts.stack)

    lines = [
        "CopyTrades diagnostics bundle",
        "=" * 60,
        f"generated:    {datetime.now().isoformat(timespec='seconds')} (local)",
        f"stack:        {opts.stack.name}",
        f"db_path:      {opts.stack.db_path}",
        f"window start: {opts.start.isoformat(timespec='seconds')} (local)",
        f"window end:   {opts.end.isoformat(timespec='seconds')} (local)",
        f"include MT5:  {opts.include_mt5}",
        f"mt5 terminal: {opts.mt5_terminal_dir or '(none detected)'}",
        f"sanitized DB: {opts.sanitize_db}",
        "",
        f"OS:           {platform.system()} {platform.release()} ({platform.version()})",
        f"Python:       {platform.python_version()}",
        "",
        "Sources",
        "-" * 60,
        f"files written: {files_written}",
        f"bytes written: {bytes_written:,}",
        f"skipped:       {len(skipped)}",
    ]
    if skipped:
        for s in skipped:
            lines.append(f"  - {s}")

    lines += [
        "",
        "Service states (snapshot at export time)",
        "-" * 60,
    ]
    for svc, state in service_states.items():
        lines.append(f"  {svc:40} {state}")

    lines += [
        "",
        "Profile (channels/<stack>.json, secrets redacted)",
        "-" * 60,
        profile_summary,
        "",
        "Secret-key redaction",
        "-" * 60,
        f"The sanitized DB copy has settings.value blanked for these keys:",
    ]
    for k in sorted(SECRET_KEYS):
        lines.append(f"  - {k}")

    lines += [
        "",
        "Notes",
        "-" * 60,
        "- CopyTrades log timestamps are in local time (Python's logging",
        "  default). ai_calls.jsonl `ts` fields are UTC ISO.",
        "- MT5 log timestamps are in BROKER SERVER TIME, not local or",
        "  UTC. Convert before cross-referencing with CopyTrades logs.",
        "- Multi-line tracebacks survive intact when the first line is",
        "  inside the window.",
    ]
    return "\n".join(lines) + "\n"


def _profile_summary(stack: Stack) -> str:
    """Pretty-print the active profile JSON with secrets stripped.

    Profiles don't carry API keys today, but the redaction is here as
    a forward-compat guard — if any field key ever contains 'key',
    'token', or 'secret', it gets blanked. Cheap insurance against
    future schema additions."""
    path = stack.db_path.parent / "profile.json"
    if not path.exists():
        return "(no profile.json)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"(read failed: {e})"

    def _scrub(node):
        if isinstance(node, dict):
            return {
                k: ("***REDACTED***" if _looks_secret(k) else _scrub(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [_scrub(x) for x in node]
        return node

    return json.dumps(_scrub(data), indent=2, ensure_ascii=False)


def _looks_secret(key: str) -> bool:
    low = key.lower()
    return any(tag in low for tag in ("key", "token", "secret", "password"))


def _service_states(stack: Stack) -> dict[str, str]:
    """Read the SCM state of each per-stack service via `sc query`.
    Best-effort — if sc isn't available or a service is missing we
    note that instead of failing."""
    from src.gui.services import nssm_client
    out: dict[str, str] = {}
    for svc in stack.service_names:
        try:
            if not nssm_client.service_exists(svc):
                out[svc] = "NOT INSTALLED"
            elif nssm_client.service_running(svc):
                out[svc] = "RUNNING"
            else:
                out[svc] = "STOPPED"
        except Exception as e:  # noqa: BLE001 — informational manifest field
            out[svc] = f"(probe failed: {e})"
    return out


# ---- Small helpers ------------------------------------------------------


def _mtime_in_range(mtime: float, start: datetime, end: datetime) -> bool:
    """File mtime gates whether we open it. We compare against the
    file's most-recently-written instant: if the file was last
    modified before `start`, none of its content matters; if it was
    modified after `end`, the early part might still be in range — so
    include and let the line-level filter sort it out."""
    mtime_dt = datetime.fromtimestamp(mtime)
    if mtime_dt < _to_naive(start):
        return False
    return True  # may contain in-range content even if mtime > end


def _to_naive(dt: datetime) -> datetime:
    """Strip tzinfo. CopyTrades file logs are naive-local; the dialog
    passes naive too. Single source of truth for the comparison shape."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)
