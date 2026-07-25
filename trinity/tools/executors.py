"""
trinity/tools/executors.py
───────────────────────────
Pure-Python port of the 6 core tools extracted from Cline's `tools.js`:

  • read_files        — read text/image files with optional line ranges
  • search_codebase   — regex search (ripgrep with Python fallback)
  • run_commands      — non-interactive shell command execution
  • fetch_web_content — HTTP fetch with HTML→text and JSON pretty-print
  • editor            — create / replace / insert text in files
  • apply_patch       — apply canonical freeform patch grammar to files

The module is intentionally synchronous at the leaf level (filesystem, regex,
HTML parsing) and exposes async wrappers (`execute_*`) that mirror the JS API
verbatim. The shape of inputs and outputs is the same as in `tools.js` — the
goal is to swap one implementation for the other without touching callers.

Host-safety contract (Trinity-specific, beyond the original Cline behaviour):
  1. `run_commands` REQUIRES manual Y/N confirmation via stdin BEFORE execution.
     The backend prints the command to console, waits for user input, and only
     then runs it. Without confirmation, nothing happens.
  2. `read_files`, `editor`, and `apply_patch` resolve every path against
     `settings.workspace_dir` (with `os.path.abspath`) and REFUSE to operate
     on anything that escapes that directory. This is the same sandbox model
     used by Trinity's existing Cline-style file tools.
  3. `search_codebase` operates on the same workspace root.
  4. `fetch_web_content` is outbound-only (http/https) and uses a sane UA
     so the LLM can pull documentation; there is no path-side risk.

The JSON schemas consumed by Gemini live in `trinity/tools/schemas.json`
(extracted from `extracted_tools/schemas.json` during the Cline extraction).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from core.config import settings

log = logging.getLogger("trinity.executors")


# =============================================================================
# Output limits (mirror tools.js)
# =============================================================================

MAX_COMMAND_OUTPUT_CHARS = 48_000
MAX_READ_LINES = 2_000
MAX_LINE_CHARS = 2_000
MAX_READ_OUTPUT_CHARS = 48_000
MAX_SEARCH_OUTPUT_CHARS = 48_000
INPUT_ARG_CHAR_LIMIT = 6_000

IMAGE_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

DEFAULT_INCLUDE_EXTENSIONS = (
    "ts", "tsx", "js", "jsx", "mjs", "cjs", "json", "md", "mdx", "txt",
    "yaml", "yml", "toml", "py", "rb", "go", "rs", "java", "kt", "swift",
    "c", "cpp", "h", "hpp", "css", "scss", "less", "html", "vue", "svelte",
    "sql", "sh", "bash", "zsh", "fish", "ps1", "env", "gitignore",
    "dockerignore", "editorconfig",
)

DEFAULT_EXCLUDE_DIRS = frozenset({
    "node_modules", ".git", "dist", "build", ".next", "coverage",
    "__pycache__", ".venv", "venv", ".cache", ".turbo", ".output",
    "out", "target", "bin", "obj",
})

DEFAULT_READ_OPTIONS = {
    "max_file_size_bytes": 10_000_000,
    "encoding": "utf-8",
    "include_line_numbers": True,
}
MAX_TEXT_STREAM_BYTES = 100_000_000

DEFAULT_SEARCH_OPTIONS = {
    "include_extensions": DEFAULT_INCLUDE_EXTENSIONS,
    "exclude_dirs": tuple(DEFAULT_EXCLUDE_DIRS),
    "max_results": 100,
    "context_lines": 2,
    "max_depth": 20,
}

DEFAULT_EDITOR_OPTIONS = {
    "encoding": "utf-8",
    "restrict_to_cwd": True,
    "max_diff_lines": 200,
}

DEFAULT_APPLY_PATCH_OPTIONS = {
    "encoding": "utf-8",
    "restrict_to_cwd": True,
}

DEFAULT_WEB_FETCH_OPTIONS = {
    "timeout_s": 30,
    "max_response_bytes": 5_000_000,
    "user_agent": "Mozilla/5.0 (compatible; TrinityAgent/1.0)",
    "headers": {},
    "follow_redirects": True,
}


# =============================================================================
# Result shape + exceptions
# =============================================================================

@dataclass
class ToolOperationResult:
    """One per-input result. Mirrors JS: {query, result, success, error?}."""
    query: str
    result: Any
    success: bool
    error: Optional[str] = None


class TimeoutError_(Exception):
    pass


class CommandExitError_(Exception):
    def __init__(self, exit_code: int, output: str):
        super().__init__(f"Command exited with code {exit_code}")
        self.exit_code = exit_code
        self.output = output


class DiffError_(Exception):
    pass


# =============================================================================
# Path safety (sandbox inside workspace_dir)
# =============================================================================

def _workspace_root() -> str:
    """Absolute workspace root used as the sandbox."""
    return os.path.abspath(settings.workspace_dir or ".")


def safe_resolve(user_path: str, *, restrict: bool = True) -> str:
    """
    Resolve a user-supplied path to an absolute path and, if `restrict` is True,
    make sure the result stays inside the workspace directory.

    Mirrors `resolveFilePath` from tools.js with the Cline semantics:
      • absolute input  → resolved as-is (still checked against workspace)
      • relative input  → joined with workspace root
      • escapes the sandbox (parent or absolute escape) → SecurityError
    """
    if not user_path or not str(user_path).strip():
        raise PermissionError("empty path")
    root = _workspace_root()
    if os.path.isabs(user_path):
        candidate = os.path.normpath(user_path)
    else:
        candidate = os.path.normpath(os.path.join(root, user_path))
    if restrict:
        rel = os.path.relpath(candidate, root)
        if rel.startswith("..") or os.path.isabs(rel):
            raise PermissionError(
                f"Path {user_path!r} resolves outside workspace {root!r}"
            )
    return candidate


# =============================================================================
# Manual confirmation for run_commands
# =============================================================================

def _prompt_user_yes_no(prompt: str, default: bool = False) -> bool:
    """
    Print `prompt` to stderr (so it doesn't get captured into a tool result)
    and read one line of input from stdin. Empty input → default.

    Used to gate `run_commands` before execution. Backend-only, single-threaded
    (this is the standard Python `input()` wrapped so we can stub it in tests
    and pick a sane default when stdin is closed/non-interactive).
    """
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        ans = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        log.warning("User prompt interrupted; defaulting to NO")
        return False
    if not ans:
        return default
    return ans in ("y", "yes", "д", "да")


def confirm_commands(commands: Sequence[str]) -> Tuple[List[str], List[str]]:
    """
    Print every command and ask for Y/N approval. Returns (approved, rejected).
    The backend does NOT execute a command unless the user explicitly OKs it.
    """
    if not commands:
        return [], []
    print("\n" + "=" * 64, file=sys.stderr)
    print("⚠  Trinity run_commands — manual approval required", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    for i, cmd in enumerate(commands, 1):
        print(f"[{i}/{len(commands)}] {cmd}", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    approved: List[str] = []
    rejected: List[str] = []
    for cmd in commands:
        ok = _prompt_user_yes_no(
            f"Approve execution of: {cmd!r}?", default=False
        )
        if ok:
            approved.append(cmd)
        else:
            rejected.append(cmd)
            print(f"  ✗ Rejected: {cmd}", file=sys.stderr)
    if approved:
        print(f"  → Approved {len(approved)} command(s)", file=sys.stderr)
    print("=" * 64 + "\n", file=sys.stderr)
    return approved, rejected


# =============================================================================
# Shared helpers
# =============================================================================

def _truncate_command_output(text: str, max_chars: int = MAX_COMMAND_OUTPUT_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max(1, max_chars - head)
    return (
        f"{text[:head]}\n"
        f"[... output truncated: {len(text)} chars total. "
        "Refine the command (grep, head, tail) to view the elided middle ...]\n"
        f"{text[-tail:]}"
    )


def _cap_search_output(text: str) -> str:
    if len(text) <= MAX_SEARCH_OUTPUT_CHARS:
        return text
    head = MAX_SEARCH_OUTPUT_CHARS // 2
    tail = max(1, MAX_SEARCH_OUTPUT_CHARS - head)
    return (
        f"{text[:head]}\n"
        f"[... search output truncated: {len(text)} chars total. "
        "Narrow the pattern or scope to view the elided matches ...]\n"
        f"{text[-tail:]}"
    )


def _format_error(error: Exception) -> str:
    if isinstance(error, BaseException):
        return str(error) or error.__class__.__name__
    return str(error)


def _default_shell_args(shell: str, command: str) -> List[str]:
    if shell in ("powershell", "pwsh"):
        return ["-NoProfile", "-NonInteractive", "-Command", command]
    return ["-c", command]


def _default_shell() -> str:
    if sys.platform == "win32":
        return "powershell"
    if sys.platform == "darwin":
        return "/bin/zsh"
    return "/bin/bash"


# =============================================================================
# read_files
# =============================================================================

async def _read_text_window(
    file_path: str,
    *,
    encoding: str,
    include_line_numbers: bool,
    start_line: Optional[int],
    end_line: Optional[int],
) -> str:
    """
    Read a slice of a text file with line numbers and the same capping
    rules as the JS implementation (2000 lines / 48k chars, soft 50k line scan).
    """
    requested_start = max(start_line or 1, 1)
    requested_end = end_line if end_line is not None else 10**12
    has_finite_end = end_line is not None
    max_captured_line_number = (
        min(end_line, requested_start + MAX_READ_LINES - 1)
        if has_finite_end
        else requested_start + MAX_READ_LINES - 1
    )
    line_number_prefix_chars = (
        len(str(max_captured_line_number)) + 3 if include_line_numbers else 0
    )

    captured: List[Tuple[int, str]] = []
    chars = 0
    total_lines = 0
    capped = False
    approximate_total_lines = False
    max_scanned_line = (
        requested_end
        if has_finite_end
        else requested_start + 50_000 - 1
    )

    # Stream the file line by line. utf-8 with `errors='replace'` is good
    # enough for the LLM use-case (we never want one bad byte to nuke the
    # whole read).
    with open(file_path, "r", encoding=encoding, errors="replace") as fh:
        for raw in fh:
            total_lines += 1
            if has_finite_end and total_lines > requested_end:
                total_lines = requested_end
                break
            if not has_finite_end and capped and total_lines >= max_scanned_line:
                approximate_total_lines = True
                break
            if total_lines < requested_start or capped:
                continue
            if len(captured) >= MAX_READ_LINES:
                capped = True
                continue

            line = raw.rstrip("\n").rstrip("\r")
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " [line truncated]"
            next_chars = chars + len(line) + line_number_prefix_chars + 1
            if next_chars > MAX_READ_OUTPUT_CHARS and captured:
                capped = True
                continue
            captured.append((total_lines, line))
            chars = next_chars

    if not captured:
        return ""
    max_line_num_width = len(str(captured[-1][0]))
    if include_line_numbers:
        body = "\n".join(
            f"{ln:>{max_line_num_width}} | {text}" for ln, text in captured
        )
    else:
        body = "\n".join(text for _, text in captured)
    last_line = captured[-1][0]
    if last_line >= (end_line or total_lines):
        return body
    total_text = (
        f"{total_lines}+ lines" if approximate_total_lines else str(total_lines)
    )
    return (
        f"{body}\n\n"
        f"[Showing lines {requested_start}-{last_line} of {total_text}. "
        "Use start_line/end_line to read other sections.]"
    )


async def execute_read_files(input_data: Any, *, workspace: Optional[str] = None,
                             options: Optional[Dict[str, Any]] = None) -> List[ToolOperationResult]:
    opts = {**DEFAULT_READ_OPTIONS, **(options or {})}
    max_file_size = int(opts["max_file_size_bytes"])
    encoding = str(opts["encoding"])
    include_line_numbers = bool(opts["include_line_numbers"])

    # ── normalize input ───────────────────────────────────────────
    if isinstance(input_data, str):
        requests = [{"path": input_data}]
    elif isinstance(input_data, list):
        requests = [
            {"path": x} if isinstance(x, str) else dict(x) for x in input_data
        ]
    elif isinstance(input_data, dict):
        if isinstance(input_data.get("files"), list):
            requests = [
                {"path": x} if isinstance(x, str) else dict(x)
                for x in input_data["files"]
            ]
        elif isinstance(input_data.get("paths"), list):
            requests = [
                {"path": x} if isinstance(x, str) else dict(x)
                for x in input_data["paths"]
            ]
        elif isinstance(input_data.get("file_paths"), list):
            requests = [{"path": p} for p in input_data["file_paths"]]
        else:
            requests = [dict(input_data)]
    else:
        raise ValueError("Invalid read_files input")

    # Workspace override (lets us pin to a different sandbox if needed).
    root_override = os.path.abspath(workspace) if workspace else None

    results: List[ToolOperationResult] = []
    for req in requests:
        path = req.get("path")
        if not path:
            results.append(ToolOperationResult(
                query=str(path or ""), result="", success=False,
                error="Missing 'path' in read_files request",
            ))
            continue
        start = req.get("start_line")
        end = req.get("end_line")
        if start is not None and end is not None and start > end:
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result="",
                success=False,
                error=f"start_line must be <= end_line (start={start}, end={end})",
            ))
            continue

        # Sandbox-resolve.
        try:
            abs_path = safe_resolve(path)
        except PermissionError as e:
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result="", success=False,
                error=f"Permission denied: {e}",
            ))
            continue
        if root_override and not abs_path.startswith(root_override):
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result="", success=False,
                error=f"Path {path!r} outside workspace {root_override!r}",
            ))
            continue

        if not os.path.isfile(abs_path):
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result="", success=False,
                error=f"Path is not a file: {abs_path}",
            ))
            continue
        try:
            stat = os.stat(abs_path)
        except OSError as e:
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result="", success=False,
                error=f"Cannot stat {abs_path}: {e}",
            ))
            continue

        ext = os.path.splitext(abs_path)[1].lower()
        if ext in IMAGE_MEDIA_TYPES:
            if stat.st_size > max_file_size:
                results.append(ToolOperationResult(
                    query=_format_read_query(path, start, end),
                    result="", success=False,
                    error=f"Image too large ({stat.st_size} > {max_file_size})",
                ))
                continue
            with open(abs_path, "rb") as fh:
                data = fh.read()
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result={
                    "type": "image",
                    "data_b64": data.hex(),  # placeholder; real client converts
                    "media_type": IMAGE_MEDIA_TYPES[ext],
                    "note": "image data preserved as hex; downstream may re-encode",
                },
                success=True,
            ))
            continue

        if stat.st_size > MAX_TEXT_STREAM_BYTES:
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result="", success=False,
                error=(
                    f"Text file too large to stream safely: {stat.st_size} bytes. "
                    "Use a targeted command such as sed/grep/head/tail."
                ),
            ))
            continue

        try:
            text = await _read_text_window(
                abs_path,
                encoding=encoding,
                include_line_numbers=include_line_numbers,
                start_line=start,
                end_line=end,
            )
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result=text, success=True,
            ))
        except Exception as e:  # noqa: BLE001
            results.append(ToolOperationResult(
                query=_format_read_query(path, start, end),
                result="", success=False,
                error=f"Error reading file: {_format_error(e)}",
            ))
    return results


def _format_read_query(path: str, start: Optional[int], end: Optional[int]) -> str:
    if start is None and end is None:
        return path
    s = start if start is not None else 1
    e = end if end is not None else "EOF"
    return f"{path}:{s}-{e}"


# =============================================================================
# search_codebase
# =============================================================================

_RG_AVAILABLE: Optional[bool] = None


def _check_ripgrep_available() -> bool:
    """Cache the result of `rg --version` for the lifetime of the process."""
    global _RG_AVAILABLE
    if _RG_AVAILABLE is not None:
        return _RG_AVAILABLE
    try:
        out = subprocess.run(
            ["rg", "--version"], capture_output=True, timeout=2, text=True
        )
        _RG_AVAILABLE = (out.returncode == 0)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        _RG_AVAILABLE = False
    return _RG_AVAILABLE


def _ripgrep_search(
    query: str,
    cwd: str,
    max_results: int,
    context_lines: int,
    timeout_s: float = 5.0,
) -> Optional[List[Dict[str, Any]]]:
    """
    Run ripgrep with `--json --context=N --max-count=1 -i` and return a list
    of match objects (or None on failure / empty).
    """
    try:
        proc = subprocess.run(
            ["rg", "--json", f"--context={context_lines}", "--max-count=1",
             "-i", query],
            cwd=cwd, capture_output=True, timeout=timeout_s, text=True,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    matches: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        if len(matches) >= max_results:
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "match":
            data = obj["data"]
            sub = data.get("submatches") or []
            if not sub:
                continue
            s0 = sub[0]
            matches.append({
                "file": data["path"]["text"],
                "line": data["line_number"],
                "column": (s0.get("start") or 0) + 1,
                "match": (s0.get("match") or {}).get("text", ""),
                "context": [],
            })
        elif obj.get("type") == "context" and matches:
            ctx = obj["data"]
            prefix = ">" if ctx.get("line_number") == matches[-1]["line"] else " "
            matches[-1]["context"].append(
                f"{prefix} {ctx.get('line_number')}: "
                f"{(ctx.get('lines') or {}).get('text', '')}"
            )
    return matches or None


def _should_include_file(rel: str, exclude_dirs: frozenset, include_exts: frozenset,
                        max_depth: int) -> bool:
    parts = rel.replace("\\", "/").split("/")
    file_name = parts[-1] if parts else ""
    depth = len(parts) - 1
    if depth > max_depth:
        return False
    for seg in parts[:-1]:
        if seg in exclude_dirs:
            return False
    ext = os.path.splitext(file_name)[1].lstrip(".").lower()
    return ext in include_exts or (not ext and not file_name.startswith("."))


def _walk_files_for_search(root: str, exclude_dirs: frozenset,
                          include_exts: frozenset, max_depth: int) -> List[str]:
    """Cheap, depth-limited walker used by the Python fallback."""
    out: List[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in exclude_dirs:
                        continue
                    rel = os.path.relpath(e.path, root)
                    if rel.count(os.sep) >= max_depth:
                        continue
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    rel = os.path.relpath(e.path, root)
                    if _should_include_file(rel, exclude_dirs, include_exts, max_depth):
                        out.append(e.path)
            except OSError:
                continue
    return out


def _format_search_results(query: str, matches: List[Dict[str, Any]],
                            searched_files: Optional[int] = None) -> str:
    head = f"Found {len(matches)} result{'s' if len(matches) != 1 else ''} for pattern: {query}"
    if searched_files is not None:
        head += f"\nSearched {searched_files} files."
    if not matches:
        return head + ("\nNo results." if searched_files is not None else "")
    lines: List[str] = [head, ""]
    for m in matches:
        lines.append(f"{m['file']}:{m['line']}:{m['column']}")
        lines.extend(m.get("context") or [])
        lines.append("")
    if len(matches) >= DEFAULT_SEARCH_OPTIONS["max_results"]:
        lines.append(
            f"(Showing first {len(matches)} results. "
            "Refine your search for more specific results.)"
        )
    return _cap_search_output("\n".join(lines))


async def execute_search_codebase(input_data: Any, *, workspace: Optional[str] = None,
                                 options: Optional[Dict[str, Any]] = None) -> List[ToolOperationResult]:
    opts = {**DEFAULT_SEARCH_OPTIONS, **(options or {})}
    cwd = os.path.abspath(workspace) if workspace else _workspace_root()
    include_exts = frozenset(e.lower() for e in opts["include_extensions"])
    exclude_dirs = frozenset(opts["exclude_dirs"])
    max_results = int(opts["max_results"])
    context_lines = int(opts["context_lines"])
    max_depth = int(opts["max_depth"])

    if isinstance(input_data, str):
        queries = [input_data]
    elif isinstance(input_data, list):
        queries = [str(q) for q in input_data]
    elif isinstance(input_data, dict):
        q = input_data.get("queries")
        if isinstance(q, str):
            queries = [q]
        elif isinstance(q, list):
            queries = [str(x) for x in q]
        else:
            raise ValueError("Invalid search_codebase input")
    else:
        raise ValueError("Invalid search_codebase input")

    rg_enabled = _check_ripgrep_available()
    results: List[ToolOperationResult] = []
    for query in queries:
        matches: Optional[List[Dict[str, Any]]] = None
        if rg_enabled:
            try:
                matches = await asyncio.to_thread(
                    _ripgrep_search, query, cwd, max_results, context_lines
                )
            except Exception as e:  # noqa: BLE001
                log.debug("ripgrep failed (%s); falling back", e)
                matches = None
        if not matches:
            try:
                regex = re.compile(query, re.IGNORECASE | re.MULTILINE)
            except re.error as e:
                results.append(ToolOperationResult(
                    query=query, result="", success=False,
                    error=f"Invalid regex: {e}",
                ))
                continue
            files = await asyncio.to_thread(
                _walk_files_for_search, cwd, exclude_dirs, include_exts, max_depth
            )
            matches = []
            for fp in files:
                if len(matches) >= max_results:
                    break
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                rel = os.path.relpath(fp, cwd)
                lines = text.split("\n")
                for i, line in enumerate(lines):
                    if len(matches) >= max_results:
                        break
                    for m in regex.finditer(line):
                        start = max(0, i - context_lines)
                        end = min(len(lines) - 1, i + context_lines)
                        ctx: List[str] = []
                        for j in range(start, end + 1):
                            prefix = ">" if j == i else " "
                            ctx.append(f"{prefix} {j + 1}: {lines[j]}")
                        matches.append({
                            "file": rel,
                            "line": i + 1,
                            "column": m.start() + 1,
                            "match": m.group(0),
                            "context": ctx,
                        })
                        if m.start() == m.end():
                            break
        try:
            text = _format_search_results(query, matches, searched_files=None)
            results.append(ToolOperationResult(query=query, result=text, success=True))
        except Exception as e:  # noqa: BLE001
            results.append(ToolOperationResult(
                query=query, result="", success=False,
                error=f"Search failed: {_format_error(e)}",
            ))
    return results


# =============================================================================
# run_commands (with manual confirmation)
# =============================================================================

async def _run_one_command(command: str, *, cwd: str, shell: str, timeout_s: float,
                            max_chars: int) -> str:
    """Spawn a single command, capture stdout+stderr, and enforce the timeout."""
    if shell in ("powershell", "pwsh"):
        args = [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        args = [shell, "-c", command]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise CommandExitError_(127, f"Shell not found: {e}")
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise TimeoutError_(f"Command timed out after {timeout_s}s")
    if proc.returncode != 0:
        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        combined = out + (f"\n[stderr]\n{err}" if err else "")
        combined = _truncate_command_output(combined, max_chars)
        raise CommandExitError_(proc.returncode or 1, combined)
    out = (stdout or b"").decode("utf-8", errors="replace")
    err = (stderr or b"").decode("utf-8", errors="replace")
    combined = out + (f"\n[stderr]\n{err}" if err else "")
    return _truncate_command_output(combined, max_chars)


async def execute_run_commands(input_data: Any, *, workspace: Optional[str] = None,
                              options: Optional[Dict[str, Any]] = None,
                              auto_approve: bool = False) -> List[ToolOperationResult]:
    """
    Run shell commands inside the workspace. **Every command requires manual
    approval (Y/N) via stdin** unless `auto_approve=True` is passed (intended
    for tests; production paths must NOT set this).

    Without approval, the command is rejected and a structured
    ToolOperationResult(success=False, error=...) is returned.
    """
    opts = {
        "shell": _default_shell(),
        "timeout_s": 30.0,
        "max_output_chars": MAX_COMMAND_OUTPUT_CHARS,
        **(options or {}),
    }
    cwd = os.path.abspath(workspace) if workspace else _workspace_root()
    if isinstance(input_data, str):
        commands = [input_data]
    elif isinstance(input_data, list):
        commands = [str(c) for c in input_data]
    elif isinstance(input_data, dict):
        c = input_data.get("commands")
        if isinstance(c, list):
            commands = [str(x) for x in c]
        elif isinstance(c, str):
            commands = [c]
        elif "command" in input_data:
            commands = [str(input_data["command"])]
        elif "cmd" in input_data:
            commands = [str(input_data["cmd"])]
        else:
            raise ValueError("Invalid run_commands input")
    else:
        raise ValueError("Invalid run_commands input")

    if auto_approve:
        approved = list(commands)
        rejected: List[str] = []
    else:
        approved, rejected = confirm_commands(commands)
    results: List[ToolOperationResult] = []
    for cmd in rejected:
        results.append(ToolOperationResult(
            query=cmd, result="", success=False,
            error="Rejected by user (manual Y/N confirmation).",
        ))
    for cmd in approved:
        try:
            out = await _run_one_command(
                cmd,
                cwd=cwd,
                shell=str(opts["shell"]),
                timeout_s=float(opts["timeout_s"]),
                max_chars=int(opts["max_output_chars"]),
            )
            results.append(ToolOperationResult(query=cmd, result=out, success=True))
        except CommandExitError_ as e:
            results.append(ToolOperationResult(
                query=cmd, result=e.output, success=False,
                error=f"Command exited with code {e.exit_code}",
            ))
        except TimeoutError_ as e:
            results.append(ToolOperationResult(
                query=cmd, result="", success=False,
                error=str(e),
            ))
        except Exception as e:  # noqa: BLE001
            results.append(ToolOperationResult(
                query=cmd, result="", success=False,
                error=f"Command failed: {_format_error(e)}",
            ))
    return results


# =============================================================================
# fetch_web_content
# =============================================================================

_HTML_TAG_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_BLOCK_TAG_RE = re.compile(
    r"<(p|div|br|hr|h[1-6]|li|tr)[^>]*>", re.IGNORECASE
)
_HTML_TAG_GENERIC_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_NUMERIC_RE = re.compile(r"&#(\d+);")


def _html_to_text(html_text: str) -> str:
    """Cheap HTML→text (script/style/comment strip, block-tag → newline)."""
    s = _HTML_TAG_RE.sub(" ", html_text)
    s = _HTML_COMMENT_RE.sub(" ", s)
    s = _HTML_BLOCK_TAG_RE.sub("\n", s)
    s = _HTML_TAG_GENERIC_RE.sub(" ", s)
    s = html.unescape(s)
    s = _HTML_ENTITY_NUMERIC_RE.sub(
        lambda m: chr(int(m.group(1))) if m.group(1).isdigit() else m.group(0),
        s,
    )
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


async def _fetch_one_url(url: str, prompt: str, *, opts: Dict[str, Any]) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid protocol: {parsed.scheme!r}. Only http(s) supported.")
    headers = {
        "User-Agent": opts["user_agent"],
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "text/plain;q=0.8,*/*;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        **opts.get("headers", {}),
    }
    req = urllib.request.Request(url, headers=headers)
    timeout = float(opts["timeout_s"])
    max_bytes = int(opts["max_response_bytes"])
    loop = asyncio.get_running_loop()

    def _do_fetch() -> Tuple[int, Dict[str, str], bytes]:
        # urllib follows redirects by default; we wrap to keep the call
        # short and to read the body in one shot (responses are small).
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            info = dict(resp.getheaders())
            data = resp.read(max_bytes + 1)
            return resp.status, info, data

    try:
        status, info, data = await asyncio.to_thread(_do_fetch)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"URL error: {e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError(f"Fetch timed out after {timeout}s") from e

    if status >= 400:
        raise RuntimeError(f"HTTP {status}: {info.get('Reason', 'error')}")
    if len(data) > max_bytes:
        data = data[:max_bytes]
    content_type = info.get("Content-Type", "")
    text = data.decode("utf-8", errors="replace")
    if "text/html" in content_type or "application/xhtml" in content_type:
        body = _html_to_text(text)
    elif "application/json" in content_type:
        try:
            body = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            body = text
    else:
        body = text
    head = (
        f"URL: {url}\n"
        f"Content-Type: {content_type}\n"
        f"Size: {len(data)} bytes\n\n--- Content ---\n"
    )
    tail = ""
    if len(body) > 50_000:
        body = body[:50_000]
        tail = f"\n\n[Content truncated: showing first 50000 of {len(text)} characters]"
    return f"{head}{body}{tail}\n\n--- Analysis Request ---\nPrompt: {prompt}"


async def execute_fetch_web_content(input_data: Any, *, options: Optional[Dict[str, Any]] = None,
                                    workspace: Optional[str] = None) -> List[ToolOperationResult]:
    """workspace is unused for HTTP; kept for signature symmetry with other tools."""
    opts = {**DEFAULT_WEB_FETCH_OPTIONS, **(options or {})}
    if not isinstance(input_data, dict) or not isinstance(input_data.get("requests"), list):
        raise ValueError("fetch_web_content requires { requests: [{url, prompt}, ...] }")
    results: List[ToolOperationResult] = []
    for req in input_data["requests"]:
        url = req.get("url")
        prompt = req.get("prompt") or ""
        if not url:
            results.append(ToolOperationResult(
                query=str(url or ""), result="", success=False,
                error="Missing 'url' in request",
            ))
            continue
        try:
            text = await _fetch_one_url(url, prompt, opts=opts)
            results.append(ToolOperationResult(query=url, result=text, success=True))
        except Exception as e:  # noqa: BLE001
            results.append(ToolOperationResult(
                query=url, result="", success=False,
                error=f"Error fetching web content: {_format_error(e)}",
            ))
    return results


# =============================================================================
# editor (create / replace / insert)
# =============================================================================

def _count_occurrences(content: str, needle: str) -> int:
    if not needle:
        return 0
    return content.count(needle)


def _create_line_diff(old: str, new: str, max_lines: int) -> str:
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    start = 0
    while (
        start < len(old_lines)
        and start < len(new_lines)
        and old_lines[start] == new_lines[start]
    ):
        start += 1
    old_end, new_end = len(old_lines), len(new_lines)
    while (
        old_end > start
        and new_end > start
        and old_lines[old_end - 1] == new_lines[new_end - 1]
    ):
        old_end -= 1
        new_end -= 1
    removed_count = old_end - start
    added_count = new_end - start
    removed_budget = removed_count
    added_budget = added_count
    if removed_count + added_count > max_lines:
        removed_budget = min(
            removed_count, max(max_lines // 2, max_lines - added_count)
        )
        added_budget = min(added_count, max_lines - removed_budget)
    out = ["```diff"]
    for i in range(start, start + removed_budget):
        out.append(f"-{i + 1}: {old_lines[i]}")
    for i in range(start, start + added_budget):
        out.append(f"+{i + 1}: {new_lines[i]}")
    om_r = removed_count - removed_budget
    om_a = added_count - added_budget
    if om_r or om_a:
        out.append(
            f"... diff truncated ({om_r} more removed, {om_a} more added lines) ..."
        )
    out.append("```")
    return "\n".join(out)


async def _editor_create(file_path: str, content: str, encoding: str) -> str:
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(file_path, "w", encoding=encoding) as fh:
        fh.write(content)
    return f"File created successfully at: {file_path}"


async def _editor_replace(file_path: str, old: str, new: str, encoding: str,
                          max_diff_lines: int) -> str:
    with open(file_path, "r", encoding=encoding) as fh:
        original = fh.read()
    occ = _count_occurrences(original, old)
    if occ == 0:
        raise ValueError(f"text not found in {file_path}")
    if occ > 1:
        raise ValueError(f"multiple occurrences of text found in {file_path}")
    updated = original.replace(old, new or "", 1)
    with open(file_path, "w", encoding=encoding) as fh:
        fh.write(updated)
    diff = _create_line_diff(original, updated, max_diff_lines)
    return f"Edited {file_path}\n{diff}"


async def _editor_insert(file_path: str, insert_line_one_based: int, content: str,
                         encoding: str) -> str:
    with open(file_path, "r", encoding=encoding) as fh:
        text = fh.read()
    lines = text.split("\n")
    max_boundary = len(lines) + 1
    if insert_line_one_based < 1 or insert_line_one_based > max_boundary:
        raise ValueError(
            f"Invalid insert_line: {insert_line_one_based}. "
            f"Must be in 1..{max_boundary} (use {max_boundary} to append at EOF)."
        )
    lines.insert(insert_line_one_based - 1, *content.split("\n"))
    with open(file_path, "w", encoding=encoding) as fh:
        fh.write("\n".join(lines))
    return f"Inserted content at line {insert_line_one_based} in {file_path}."


async def execute_editor(input_data: Any, *, workspace: Optional[str] = None,
                        options: Optional[Dict[str, Any]] = None) -> ToolOperationResult:
    if not isinstance(input_data, dict):
        return ToolOperationResult(
            query="editor", result="", success=False,
            error="editor input must be an object with path/new_text",
        )
    opts = {**DEFAULT_EDITOR_OPTIONS, **(options or {})}
    encoding = str(opts["encoding"])
    restrict = bool(opts["restrict_to_cwd"])
    max_diff_lines = int(opts["max_diff_lines"])
    operation = "insert" if input_data.get("insert_line") is not None else "edit"

    path = input_data.get("path")
    new_text = input_data.get("new_text")
    if not path or not isinstance(path, str):
        return ToolOperationResult(
            query=f"{operation}:{path or ''}", result="", success=False,
            error="path is required and must be a string",
        )
    if not isinstance(new_text, str):
        return ToolOperationResult(
            query=f"{operation}:{path}", result="", success=False,
            error="new_text is required and must be a string",
        )
    old_text = input_data.get("old_text")
    insert_line = input_data.get("insert_line")

    if isinstance(old_text, str) and len(old_text) > INPUT_ARG_CHAR_LIMIT:
        return ToolOperationResult(
            query=f"{operation}:{path}", result="", success=False,
            error=f"old_text too large ({len(old_text)} chars; cap {INPUT_ARG_CHAR_LIMIT})",
        )
    if len(new_text) > INPUT_ARG_CHAR_LIMIT:
        return ToolOperationResult(
            query=f"{operation}:{path}", result="", success=False,
            error=f"new_text too large ({len(new_text)} chars; cap {INPUT_ARG_CHAR_LIMIT})",
        )

    try:
        abs_path = safe_resolve(path, restrict=restrict)
    except PermissionError as e:
        return ToolOperationResult(
            query=f"{operation}:{path}", result="", success=False,
            error=f"Permission denied: {e}",
        )

    try:
        if insert_line is not None:
            text = await _editor_insert(abs_path, int(insert_line), new_text, encoding)
        elif not os.path.exists(abs_path):
            text = await _editor_create(abs_path, new_text, encoding)
        elif old_text is None:
            return ToolOperationResult(
                query=f"{operation}:{path}", result="", success=False,
                error="old_text is required when editing an existing file (no insert_line)",
            )
        else:
            text = await _editor_replace(
                abs_path, old_text, new_text, encoding, max_diff_lines
            )
        return ToolOperationResult(query=f"{operation}:{path}", result=text, success=True)
    except Exception as e:  # noqa: BLE001
        return ToolOperationResult(
            query=f"{operation}:{path}", result="", success=False,
            error=f"Editor operation failed: {_format_error(e)}",
        )


# =============================================================================
# apply_patch (canonical freeform grammar)
# =============================================================================

PATCH_MARKERS = {
    "BEGIN": "*** Begin Patch",
    "END": "*** End Patch",
    "ADD": "*** Add File: ",
    "UPDATE": "*** Update File: ",
    "DELETE": "*** Delete File: ",
    "MOVE": "*** Move to: ",
    "END_FILE": "*** End of File",
}
BASH_WRAPPERS = ("%%bash", "apply_patch", "EOF", "```")
PatchActionType = {"ADD": "add", "DELETE": "delete", "UPDATE": "update"}


def _canonicalize(text: str) -> str:
    """Map fancy Unicode dashes/quotes/spaces to their ASCII counterparts."""
    punct = {
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
        "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
        "‘": "'", "’": "'", "‛": "'",
        " ": " ", " ": " ",
    }
    out = unicodedata.normalize("NFC", text)
    return "".join(punct.get(c, c) for c in out)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            )
        prev = curr
    return prev[-1]


def _similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    longer = a if len(a) > len(b) else b
    shorter = b if len(a) > len(b) else a
    if not longer:
        return 1.0
    return (len(longer) - _levenshtein(shorter, longer)) / len(longer)


def _peek(lines: List[str], index: int) -> Tuple[List[str], List[Dict[str, Any]], int, bool]:
    """Walk a chunk until the next sentinel. Returns (context_lines, chunks, end_index, eof)."""
    old: List[str] = []
    del_lines: List[str] = []
    ins_lines: List[str] = []
    chunks: List[Dict[str, Any]] = []
    mode = "keep"
    stop_markers = (
        "@@", PATCH_MARKERS["END"], PATCH_MARKERS["UPDATE"],
        PATCH_MARKERS["DELETE"], PATCH_MARKERS["ADD"], PATCH_MARKERS["END_FILE"],
    )
    while index < len(lines):
        src = lines[index]
        if src is None:
            break
        if any(src.startswith(m) for m in stop_markers):
            break
        if src == "***":
            break
        if src.startswith("***"):
            raise DiffError_(f"Invalid line: {src}")
        index += 1
        previous_mode = mode
        if src.startswith("+"):
            mode = "add"
            line = src[1:]
        elif src.startswith("-"):
            mode = "delete"
            line = src[1:]
        elif src.startswith(" "):
            mode = "keep"
            line = src[1:]
        else:
            mode = "keep"
            line = " " + src
        if mode == "keep" and previous_mode != mode:
            if ins_lines or del_lines:
                chunks.append({
                    "origIndex": len(old) - len(del_lines),
                    "delLines": del_lines,
                    "insLines": ins_lines,
                })
            del_lines = []
            ins_lines = []
        if mode == "delete":
            del_lines.append(line)
            old.append(line)
        elif mode == "add":
            ins_lines.append(line)
        else:
            old.append(line)
    if ins_lines or del_lines:
        chunks.append({
            "origIndex": len(old) - len(del_lines),
            "delLines": del_lines,
            "insLines": ins_lines,
        })
    eof = False
    if index < len(lines) and lines[index] == PATCH_MARKERS["END_FILE"]:
        index += 1
        eof = True
    return old, chunks, index, eof


def _find_context(lines: List[str], context: List[str], start: int, eof: bool):
    """
    Locate a context block in `lines`. Returns (index, fuzz, similarity).
    `fuzz` is a confidence penalty (0 = exact, 100 = whitespace, 1000 = fuzzy, 10000 = at EOF).
    """
    if not context:
        return start, 0, 1.0
    canonical = _canonicalize("\n".join(context))
    end = len(lines)

    def _scan_at(idx: int) -> Tuple[int, int]:
        for i in range(idx, end - len(context) + 1):
            if _canonicalize("\n".join(lines[i : i + len(context)])) == canonical:
                return i, 0
        # trim-trailing whitespace
        trimmed = _canonicalize("\n".join(l.rstrip() for l in context))
        for i in range(idx, end - len(context) + 1):
            if _canonicalize("\n".join(lines[i : i + len(context)]).rstrip()) == trimmed:
                return i, 1
        return -1, 0

    found, fuzz = _scan_at(start)
    if found != -1:
        return found, fuzz, 1.0
    if eof:
        found, fuzz = _scan_at(max(0, end - len(context)))
        if found != -1:
            return found, fuzz + 10000, 1.0
    # fuzzy: best similarity
    best_sim = 0.0
    best_idx = -1
    for i in range(start, end - len(context) + 1):
        sim = _similarity(
            _canonicalize("\n".join(lines[i : i + len(context)])), canonical
        )
        if sim > best_sim:
            best_sim = sim
            best_idx = i
    return best_idx, 1000, best_sim


def _normalize_line_endings(text: str) -> List[str]:
    return [l.rstrip("\r") for l in text.split("\n")]


def _is_wrapper_line(line: str) -> bool:
    if not line.strip():
        return False
    return any(line.startswith(w) for w in BASH_WRAPPERS)


def _trim_wrapper_lines(lines: List[str]) -> List[str]:
    start, end = 0, len(lines)
    while start < end and _is_wrapper_line(lines[start]):
        start += 1
    while end > start and _is_wrapper_line(lines[end - 1]):
        end -= 1
    return lines[start:end]


def _normalize_patch_input(text: str) -> List[str]:
    raw = _normalize_line_endings(text)
    begin = next((i for i, l in enumerate(raw) if l.startswith(PATCH_MARKERS["BEGIN"])), -1)
    end = -1
    for i in range(len(raw) - 1, -1, -1):
        if raw[i].startswith(PATCH_MARKERS["END"]):
            end = i
            break
    if begin != -1 or end != -1:
        if begin == -1 or end == -1 or end < begin:
            raise DiffError_(
                "Invalid patch text - incomplete sentinels. "
                "Try breaking it into smaller patches."
            )
        return raw[begin : end + 1]
    stripped = _trim_wrapper_lines(raw)
    while stripped and stripped[0] == "":
        stripped.pop(0)
    while stripped and stripped[-1] == "":
        stripped.pop()
    return [PATCH_MARKERS["BEGIN"], *stripped, PATCH_MARKERS["END"]]


def _extract_files_for_operations(lines: List[str], markers: Sequence[str]) -> List[str]:
    files: List[str] = []
    seen: set = set()
    for line in lines:
        for marker in markers:
            if line.startswith(marker):
                name = line[len(marker):].strip()
                if name not in seen:
                    seen.add(name)
                    files.append(name)
                break
    return files


def _apply_chunks(content: str, chunks: List[Dict[str, Any]], file_path: str) -> str:
    if not chunks:
        return content
    lines = content.split("\n")
    out: List[str] = []
    cur = 0
    for chunk in chunks:
        if chunk["origIndex"] > len(lines):
            raise DiffError_(
                f"{file_path}: chunk.origIndex {chunk['origIndex']} > lines.length {len(lines)}"
            )
        if cur > chunk["origIndex"]:
            raise DiffError_(
                f"{file_path}: currentIndex {cur} > chunk.origIndex {chunk['origIndex']}"
            )
        out.extend(lines[cur : chunk["origIndex"]])
        out.extend(chunk["insLines"])
        cur = chunk["origIndex"] + len(chunk["delLines"])
    out.extend(lines[cur:])
    return "\n".join(out)


def _load_files(lines: List[str], root: str, encoding: str) -> Dict[str, str]:
    files = _extract_files_for_operations(lines, (PATCH_MARKERS["UPDATE"], PATCH_MARKERS["DELETE"]))
    out: Dict[str, str] = {}
    for p in files:
        abs_p = safe_resolve(p, restrict=True)
        try:
            with open(abs_p, "r", encoding=encoding) as fh:
                out[p] = fh.read().replace("\r\n", "\n")
        except FileNotFoundError as e:
            raise DiffError_(f"File not found: {p}") from e
        except OSError as e:
            raise DiffError_(f"Cannot read {p}: {e}") from e
    return out


def _parse_patch(lines: List[str], current_files: Dict[str, str]) -> Tuple[Dict[str, Any], int]:
    actions: Dict[str, Any] = {}
    warnings: List[Dict[str, Any]] = []
    idx = 0
    fuzz = 0
    if idx < len(lines) and lines[idx].startswith(PATCH_MARKERS["BEGIN"]):
        idx += 1
    while idx < len(lines) and not lines[idx].startswith(PATCH_MARKERS["END"]):
        line = lines[idx]
        if line.startswith(PATCH_MARKERS["UPDATE"]):
            file_path = line[len(PATCH_MARKERS["UPDATE"]):].strip()
            if file_path in actions:
                raise DiffError_(f"Duplicate update for file: {file_path}")
            if file_path not in current_files:
                raise DiffError_(f"Update File Error: Missing File: {file_path}")
            idx += 1
            move_path: Optional[str] = None
            if idx < len(lines) and lines[idx].startswith(PATCH_MARKERS["MOVE"]):
                move_path = lines[idx][len(PATCH_MARKERS["MOVE"]):].strip()
                idx += 1
            action = {"type": PatchActionType["UPDATE"], "chunks": [], "movePath": move_path}
            file_lines = current_files[file_path].split("\n")
            file_idx = 0
            stop_markers = (
                PATCH_MARKERS["END"], PATCH_MARKERS["UPDATE"],
                PATCH_MARKERS["DELETE"], PATCH_MARKERS["ADD"],
                PATCH_MARKERS["END_FILE"],
            )
            while idx < len(lines) and not any(lines[idx].startswith(m) for m in stop_markers):
                cur = lines[idx]
                if cur.startswith("@@ "):
                    defn = cur[3:].strip()
                    idx += 1
                    if defn:
                        canon = _canonicalize(defn)
                        for k in range(file_idx, len(file_lines)):
                            if (
                                file_lines[k]
                                and (
                                    _canonicalize(file_lines[k]) == canon
                                    or _canonicalize(file_lines[k].strip()) == canon
                                )
                            ):
                                file_idx = k + 1
                                break
                elif cur == "@@":
                    idx += 1
                elif file_idx != 0:
                    raise DiffError_(f"Invalid Line:\n{cur}")
                else:
                    idx += 1  # first chunk: no preceding context marker
                # peek next chunk
                ctx, chunks, end_patch_idx, eof = _peek(lines, idx)
                found, cfuzz, sim = _find_context(file_lines, ctx, file_idx, eof)
                if found == -1:
                    warnings.append({
                        "path": file_path,
                        "chunkIndex": len(action["chunks"]),
                        "message": (
                            f"Could not find matching context "
                            f"(similarity: {sim:.2f}). Chunk skipped."
                        ),
                        "context": "\n".join(ctx)[:200],
                    })
                    idx = end_patch_idx
                else:
                    fuzz += cfuzz
                    for ch in chunks:
                        ch["origIndex"] += found
                        action["chunks"].append(ch)
                    file_idx = found + len(ctx)
                    idx = end_patch_idx
            actions[file_path] = action
        elif line.startswith(PATCH_MARKERS["DELETE"]):
            file_path = line[len(PATCH_MARKERS["DELETE"]):].strip()
            if file_path in actions:
                raise DiffError_(f"Duplicate delete for file: {file_path}")
            if file_path not in current_files:
                raise DiffError_(f"Delete File Error: Missing File: {file_path}")
            actions[file_path] = {"type": PatchActionType["DELETE"]}
            idx += 1
        elif line.startswith(PATCH_MARKERS["ADD"]):
            file_path = line[len(PATCH_MARKERS["ADD"]):].strip()
            if file_path in actions:
                raise DiffError_(f"Duplicate add for file: {file_path}")
            if file_path in current_files:
                raise DiffError_(f"Add File Error: File already exists: {file_path}")
            idx += 1
            new_lines: List[str] = []
            stop_markers = (
                PATCH_MARKERS["END"], PATCH_MARKERS["UPDATE"],
                PATCH_MARKERS["DELETE"], PATCH_MARKERS["ADD"],
            )
            while idx < len(lines) and not any(lines[idx].startswith(m) for m in stop_markers):
                cur = lines[idx]
                idx += 1
                if not cur.startswith("+"):
                    raise DiffError_(f"Invalid Add File line (missing '+'): {cur}")
                new_lines.append(cur[1:])
            actions[file_path] = {
                "type": PatchActionType["ADD"],
                "newFile": "\n".join(new_lines),
            }
        else:
            raise DiffError_(f"Unknown line while parsing: {line}")
    if warnings:
        return {"actions": actions, "warnings": warnings}, fuzz
    return {"actions": actions}, fuzz


def _apply_patch_changes(changes: Dict[str, Any], root: str, encoding: str) -> List[str]:
    touched: List[str] = []
    for file_path, change in changes.items():
        abs_path = safe_resolve(file_path, restrict=True)
        if change["type"] == PatchActionType["DELETE"]:
            try:
                os.remove(abs_path)
            except FileNotFoundError:
                pass
            touched.append(f"{file_path}: [deleted]")
        elif change["type"] == PatchActionType["ADD"]:
            parent = os.path.dirname(abs_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(abs_path, "w", encoding=encoding) as fh:
                fh.write(change["newFile"])
            touched.append(file_path)
        elif change["type"] == PatchActionType["UPDATE"]:
            new_content = _apply_chunks(
                _load_files([f"*** Update File: {file_path}"], root, encoding).get(
                    file_path, ""
                ),
                change["chunks"],
                file_path,
            )
            if change.get("movePath"):
                move_abs = safe_resolve(change["movePath"], restrict=True)
                move_parent = os.path.dirname(move_abs)
                if move_parent:
                    os.makedirs(move_parent, exist_ok=True)
                with open(move_abs, "w", encoding=encoding) as fh:
                    fh.write(new_content)
                try:
                    os.remove(abs_path)
                except FileNotFoundError:
                    pass
                touched.append(f"{file_path} -> {change['movePath']}")
            else:
                with open(abs_path, "w", encoding=encoding) as fh:
                    fh.write(new_content)
                touched.append(file_path)
    return touched


async def execute_apply_patch(input_data: Any, *, workspace: Optional[str] = None,
                             options: Optional[Dict[str, Any]] = None) -> ToolOperationResult:
    opts = {**DEFAULT_APPLY_PATCH_OPTIONS, **(options or {})}
    encoding = str(opts["encoding"])
    root = os.path.abspath(workspace) if workspace else _workspace_root()
    if isinstance(input_data, dict):
        text = input_data.get("input", "")
    else:
        text = str(input_data or "")
    if not text or not isinstance(text, str):
        return ToolOperationResult(
            query="apply_patch", result="", success=False,
            error="apply_patch requires non-empty 'input' string",
        )
    try:
        lines = _normalize_patch_input(text)
        current = _load_files(lines, root, encoding)
        patch, fuzz = _parse_patch(lines, current)
        if patch.get("warnings"):
            warn_lines = [
                f"Patch could not be applied because {len(patch['warnings'])} "
                f"chunk(s) did not match the current file content."
            ]
            for w in patch["warnings"]:
                idx = w.get("chunkIndex", -1)
                hunk = str(idx + 1) if idx >= 0 else "unknown"
                warn_lines.append(f"{w['path']}: hunk {hunk}: {w['message']}")
                if w.get("context"):
                    warn_lines.append(f"Context:\n{w['context']}")
            return ToolOperationResult(
                query="apply_patch", result="", success=False,
                error="\n".join(warn_lines),
            )
        touched = _apply_patch_changes(patch["actions"], root, encoding)
        msg = "Successfully applied patch to the following files:\n" + "\n".join(touched)
        if fuzz:
            msg += f"\nNote: Patch applied with fuzz factor {fuzz}"
        return ToolOperationResult(query="apply_patch", result=msg, success=True)
    except DiffError_ as e:
        return ToolOperationResult(
            query="apply_patch", result="", success=False,
            error=f"apply_patch failed: {_format_error(e)}",
        )
    except PermissionError as e:
        return ToolOperationResult(
            query="apply_patch", result="", success=False,
            error=f"Permission denied: {e}",
        )
    except Exception as e:  # noqa: BLE001
        return ToolOperationResult(
            query="apply_patch", result="", success=False,
            error=f"apply_patch failed: {_format_error(e)}",
        )


# =============================================================================
# Public dispatch helper
# =============================================================================

EXECUTORS = {
    "read_files": execute_read_files,
    "search_codebase": execute_search_codebase,
    "run_commands": execute_run_commands,
    "fetch_web_content": execute_fetch_web_content,
    "editor": execute_editor,
    "apply_patch": execute_apply_patch,
}


async def dispatch(name: str, arguments: Any, *, workspace: Optional[str] = None,
                  options: Optional[Dict[str, Any]] = None,
                  auto_approve: bool = False) -> List[ToolOperationResult]:
    """
    Route a tool call by name to the matching executor.

    Returns a list of `ToolOperationResult` so callers can format tool
    responses uniformly. `run_commands` is the only one that supports
    `auto_approve` (intended for tests).

    Note: `editor` and `apply_patch` are single-input tools and natively
    return ONE `ToolOperationResult`. We wrap them into a list here so
    every caller (manager, base agent loop) can iterate uniformly.
    """
    fn = EXECUTORS.get(name)
    if fn is None:
        return [ToolOperationResult(
            query=name, result="", success=False,
            error=f"Unknown tool: {name}",
        )]
    if name == "run_commands":
        return await fn(arguments, workspace=workspace, options=options, auto_approve=auto_approve)
    result = await fn(arguments, workspace=workspace, options=options)
    # Normalize single-result executors (editor, apply_patch) to a list.
    if isinstance(result, ToolOperationResult):
        return [result]
    return result
