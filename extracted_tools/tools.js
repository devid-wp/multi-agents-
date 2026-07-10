/**
 * Extracted Core Tools
 *
 * Pure JavaScript implementations of the core file system, search, terminal,
 * and web fetch tools. Extracted from the Cline SDK and stripped of:
 *   - VS Code extension-specific APIs (no `vscode` module references)
 *   - Anthropic-specific wrappers (no `createTool`, `validateWithZod`, etc.)
 *   - State management (no `AgentToolContext`, telemetry, or session IDs)
 *   - Zod schemas (pure JSON schemas, no additionalProperties for Gemini)
 *
 * Each tool exports a JSON schema (for use with the Gemini API function
 * calling format) and a pure async `execute` function. The input shape of
 * each `execute` matches the schema and the output is a plain string
 * (matching the original tool result format).
 *
 * Tools included:
 *   - read_files          : Read text/image file contents with line ranges
 *   - search_codebase     : Regex search across files (ripgrep + fallback)
 *   - run_commands        : Non-interactive shell command execution
 *   - fetch_web_content   : HTTP fetch with HTML-to-text conversion
 *   - editor              : Create / replace / insert text in files
 *   - apply_patch         : Apply unified diffs using canonical grammar
 */

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const readline = require("node:readline");
const { spawn } = require("node:child_process");
const { StringDecoder } = require("node:string_decoder");
const { TextDecoder } = require("node:util");

// =============================================================================
// Output limits
// =============================================================================

const MAX_COMMAND_OUTPUT_CHARS = 48_000;
const MAX_READ_LINES = 2_000;
const MAX_LINE_CHARS = 2_000;
const MAX_READ_OUTPUT_CHARS = 48_000;
const MAX_SEARCH_OUTPUT_CHARS = 48_000;
const INPUT_ARG_CHAR_LIMIT = 6000;

function truncateCommandOutput(text, options = {}) {
  const maxChars = options.maxChars ?? MAX_COMMAND_OUTPUT_CHARS;
  const totalChars = options.totalChars ?? text.length;
  if (text.length <= maxChars && totalChars <= maxChars) {
    return text;
  }
  const headLimit = Math.ceil(maxChars / 2);
  const tailLimit = Math.max(1, maxChars - headLimit);
  return (
    `${text.slice(0, headLimit)}\n` +
    `[... output truncated: ${totalChars} chars total. ` +
    "Refine the command (grep, head, tail) to view the elided middle ...]\n" +
    text.slice(-tailLimit)
  );
}

function capSearchOutput(text) {
  if (text.length <= MAX_SEARCH_OUTPUT_CHARS) {
    return text;
  }
  const headLimit = Math.ceil(MAX_SEARCH_OUTPUT_CHARS / 2);
  const tailLimit = Math.max(1, MAX_SEARCH_OUTPUT_CHARS - headLimit);
  return (
    `${text.slice(0, headLimit)}\n` +
    `[... search output truncated: ${text.length} chars total. ` +
    "Narrow the pattern or scope to view the elided matches ...]\n" +
    text.slice(-tailLimit)
  );
}

// =============================================================================
// Shared helpers
// =============================================================================

class TimeoutError extends Error {
  constructor(message, timeoutMs) {
    super(message);
    this.name = "TimeoutError";
    this.timeoutMs = timeoutMs;
  }
}

class CommandExitError extends Error {
  constructor(exitCode, output) {
    super(`Command exited with code ${exitCode}`);
    this.name = "CommandExitError";
    this.exitCode = exitCode;
    this.output = output;
  }
}

function withTimeout(promise, ms, message) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      setTimeout(() => reject(new TimeoutError(message, ms)), ms);
    }),
  ]);
}

function getDefaultShell(platform) {
  if (platform === "win32") return "powershell";
  if (platform === "darwin") return "/bin/zsh";
  return "/bin/bash";
}

function getShellArgs(shell, command) {
  if (shell === "powershell" || shell === "pwsh") {
    return ["-NoProfile", "-NonInteractive", "-Command", command];
  }
  return ["-c", command];
}

const IMAGE_MEDIA_TYPES = new Map([
  [".gif", "image/gif"],
  [".png", "image/png"],
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".webp", "image/webp"],
]);

function resolveFilePath(cwd, inputPath, restrictToCwd) {
  const isAbsoluteInput = path.isAbsolute(inputPath);
  const resolved = isAbsoluteInput
    ? path.normalize(inputPath)
    : path.resolve(cwd, inputPath);
  if (!restrictToCwd || isAbsoluteInput) {
    return resolved;
  }
  const rel = path.relative(cwd, resolved);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new Error(`Path must stay within cwd: ${inputPath}`);
  }
  return resolved;
}

function formatError(error) {
  if (error instanceof Error) return error.message;
  return String(error);
}

// =============================================================================
// read_files
// =============================================================================

async function readTextWindow(filePath, encoding, includeLineNumbers, startLine, endLine, signal) {
  if (signal?.aborted) {
    throw new Error("File read was aborted");
  }

  const requestedStartLine = Math.max(startLine ?? 1, 1);
  const requestedEndLine = endLine ?? Number.POSITIVE_INFINITY;
  const hasFiniteEndLine = Number.isFinite(requestedEndLine);
  const maxCapturedLineNumber = Number.isFinite(requestedEndLine)
    ? Math.min(requestedEndLine, requestedStartLine + MAX_READ_LINES - 1)
    : requestedStartLine + MAX_READ_LINES - 1;
  const lineNumberPrefixChars = includeLineNumbers
    ? String(maxCapturedLineNumber).length + 3
    : 0;

  const stream = fs.createReadStream(filePath, { encoding });
  const reader = readline.createInterface({ input: stream, crlfDelay: Number.POSITIVE_INFINITY });
  const abortHandler = signal
    ? () => stream.destroy(new Error("File read was aborted"))
    : undefined;

  if (signal && abortHandler) {
    signal.addEventListener("abort", abortHandler, { once: true });
  }

  const captured = [];
  let chars = 0;
  let totalLines = 0;
  let capped = false;
  let approximateTotalLines = false;
  const maxScannedLine = hasFiniteEndLine
    ? requestedEndLine
    : requestedStartLine + 50_000 - 1;

  try {
    for await (const rawLine of reader) {
      totalLines += 1;
      if (totalLines > requestedEndLine) {
        totalLines = requestedEndLine;
        break;
      }
      if (!hasFiniteEndLine && capped && totalLines >= maxScannedLine) {
        approximateTotalLines = true;
        break;
      }
      if (totalLines < requestedStartLine || capped) {
        continue;
      }
      if (captured.length >= MAX_READ_LINES) {
        capped = true;
        continue;
      }

      let line = rawLine;
      if (line.length > MAX_LINE_CHARS) {
        line = `${line.slice(0, MAX_LINE_CHARS)} [line truncated]`;
      }
      const nextChars = chars + line.length + lineNumberPrefixChars + 1;
      if (nextChars > MAX_READ_OUTPUT_CHARS && captured.length > 0) {
        capped = true;
        continue;
      }
      captured.push({ lineNumber: totalLines, text: line });
      chars = nextChars;
    }
  } finally {
    if (signal && abortHandler) {
      signal.removeEventListener("abort", abortHandler);
    }
    reader.close();
    stream.destroy();
  }

  const maxLineNumWidth = String(
    captured[captured.length - 1]?.lineNumber ?? totalLines,
  ).length;
  const body = captured
    .map(({ lineNumber, text }) =>
      includeLineNumbers
        ? `${String(lineNumber).padStart(maxLineNumWidth, " ")} | ${text}`
        : text,
    )
    .join("\n");
  const lastCapturedLine = captured[captured.length - 1]?.lineNumber;
  if (lastCapturedLine === undefined) {
    return body;
  }
  const effectiveEndLine = Math.min(requestedEndLine, totalLines);
  if (lastCapturedLine >= effectiveEndLine) {
    return body;
  }
  const totalLineText = approximateTotalLines
    ? `${totalLines}+ lines`
    : effectiveEndLine;
  return (
    `${body}\n\n` +
    `[Showing lines ${requestedStartLine}-${lastCapturedLine} of ${totalLineText}. ` +
    "Use start_line/end_line to read other sections.]"
  );
}

const DEFAULT_READ_OPTIONS = {
  maxFileSizeBytes: 10_000_000,
  encoding: "utf-8",
  includeLineNumbers: true,
};
const MAX_TEXT_STREAM_BYTES = 100_000_000;

function createReadFilesExecutor(options = {}) {
  const { maxFileSizeBytes, encoding, includeLineNumbers } = {
    ...DEFAULT_READ_OPTIONS,
    ...options,
  };

  return async function readFile(request, context = {}) {
    const signal = context.signal;
    const { path: filePath, start_line, end_line } = request;
    const initialPath = path.isAbsolute(filePath)
      ? path.normalize(filePath)
      : path.resolve(process.cwd(), filePath);
    const resolvedPath = initialPath;
    const extension = path.extname(resolvedPath).toLowerCase();
    const imageMediaType = IMAGE_MEDIA_TYPES.get(extension);

    const stat = await fsp.stat(resolvedPath);
    if (!stat.isFile()) {
      throw new Error(`Path is not a file: ${resolvedPath}`);
    }

    if (imageMediaType) {
      if (stat.size > maxFileSizeBytes) {
        throw new Error(
          `Image file too large: ${stat.size} bytes (max: ${maxFileSizeBytes} bytes).`,
        );
      }
      const data = await fsp.readFile(resolvedPath);
      return [
        { type: "text", text: "Successfully read image" },
        { type: "image", data: data.toString("base64"), mediaType: imageMediaType },
      ];
    }

    if (stat.size > MAX_TEXT_STREAM_BYTES) {
      throw new Error(
        `Text file too large to stream safely: ${stat.size} bytes (max: ${MAX_TEXT_STREAM_BYTES} bytes). Use a targeted command such as sed, grep, head, or tail to inspect specific sections.`,
      );
    }

    return readTextWindow(resolvedPath, encoding, includeLineNumbers, start_line, end_line, signal);
  };
}

async function executeReadFiles(input, context = {}) {
  const options = { ...DEFAULT_READ_OPTIONS, ...(context.readOptions ?? {}) };
  const readFile = createReadFilesExecutor(options);
  const timeoutMs = context.fileReadTimeoutMs ?? 10_000;

  // Normalize input — accept { files: [...] }, { paths: [...] }, an array, or a single string
  let requests;
  if (typeof input === "string") {
    requests = [{ path: input }];
  } else if (Array.isArray(input)) {
    requests = input.map((v) => (typeof v === "string" ? { path: v } : v));
  } else if (input && Array.isArray(input.files)) {
    requests = input.files.map((f) => (typeof f === "string" ? { path: f } : f));
  } else if (input && Array.isArray(input.paths)) {
    requests = input.paths.map((p) => (typeof p === "string" ? { path: p } : p));
  } else if (input && Array.isArray(input.file_paths)) {
    requests = input.file_paths.map((p) => ({ path: p }));
  } else if (input && typeof input === "object") {
    requests = [input];
  } else {
    throw new Error("Invalid read_files input");
  }

  const results = await Promise.all(
    requests.map(async (request) => {
      const query = formatReadFileQuery(request);
      const start = request.start_line ?? null;
      const end = request.end_line ?? null;
      if (start != null && end != null && start > end) {
        return {
          query,
          result: "",
          error: `start_line must be less than or equal to end_line (received start_line: ${start}, end_line: ${end})`,
          success: false,
        };
      }
      try {
        const content = await withTimeout(
          readFile(request, context),
          timeoutMs,
          `File read timed out after ${timeoutMs}ms`,
        );
        return { query, result: content, success: true };
      } catch (error) {
        return {
          query,
          result: "",
          error: `Error reading file: ${formatError(error)}`,
          success: false,
        };
      }
    }),
  );
  return results;
}

function formatReadFileQuery(request) {
  const { path, start_line, end_line } = request;
  if (start_line == null && end_line == null) return path;
  const start = start_line ?? 1;
  const end = end_line ?? "EOF";
  return `${path}:${start}-${end}`;
}

// =============================================================================
// search_codebase
// =============================================================================

let rgAvailable = null;

function checkRipgrepAvailable() {
  if (rgAvailable !== null) return Promise.resolve(rgAvailable);
  return new Promise((resolve) => {
    const child = spawn("rg", ["--version"], {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    child.on("close", (code) => {
      rgAvailable = code === 0;
      resolve(rgAvailable);
    });
    child.on("error", () => {
      rgAvailable = false;
      resolve(false);
    });
    setTimeout(() => {
      if (!child.killed) child.kill("SIGTERM");
      if (rgAvailable === null) {
        rgAvailable = false;
        resolve(false);
      }
    }, 1000);
  });
}

function searchWithRipgrep(query, cwd, maxResults, contextLines, timeoutMs = 5000, signal) {
  return new Promise((resolve) => {
    const child = spawn(
      "rg",
      ["--json", `--context=${contextLines}`, "--max-count=1", "-i", query],
      { cwd, stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
    );
    let stdout = "";
    let resolvedFlag = false;
    const cleanup = () => {
      if (!child.killed) child.kill("SIGTERM");
    };
    const timeout = setTimeout(() => {
      if (!resolvedFlag) {
        resolvedFlag = true;
        cleanup();
        resolve(null);
      }
    }, timeoutMs);

    const finalize = (result) => {
      if (!resolvedFlag) {
        resolvedFlag = true;
        clearTimeout(timeout);
        cleanup();
        resolve(result);
      }
    };

    if (signal?.aborted) {
      cleanup();
      resolve(null);
      return;
    }
    signal?.addEventListener("abort", () => finalize(null));

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", () => {});

    child.on("close", (code) => {
      if (code === 0 || code === 1) {
        try {
          const matches = [];
          const lines = stdout.split("\n").filter((line) => line.trim());
          for (const line of lines) {
            if (matches.length >= maxResults) break;
            const json = JSON.parse(line);
            if (json.type === "match") {
              const matchData = json.data;
              if (json.data.submatches?.length > 0) {
                const submatch = json.data.submatches[0];
                matches.push({
                  file: matchData.path.text,
                  line: matchData.line_number,
                  column: (submatch?.start ?? 0) + 1,
                  match: submatch?.match?.text ?? "",
                  context: [],
                });
              }
            } else if (json.type === "context" && matches.length > 0) {
              const lastMatch = matches[matches.length - 1];
              const prefix = json.data.line_number === lastMatch.line ? ">" : " ";
              lastMatch.context.push(
                `${prefix} ${json.data.line_number}: ${json.data.lines?.text ?? json.data.line?.text ?? ""}`,
              );
            }
          }
          finalize(matches.length > 0 ? matches : null);
        } catch {
          finalize(null);
        }
        return;
      }
      finalize(null);
    });
    child.on("error", () => finalize(null));
  });
}

function shouldIncludeFile(relativePath, excludeDirs, includeExtensions, maxDepth) {
  const segments = relativePath.split("/");
  const fileName = segments[segments.length - 1] ?? "";
  const directoryDepth = segments.length - 1;
  if (directoryDepth > maxDepth) return false;
  for (let i = 0; i < segments.length - 1; i++) {
    if (excludeDirs.has(segments[i] ?? "")) return false;
  }
  const ext = path.posix.extname(fileName).slice(1).toLowerCase();
  return includeExtensions.has(ext) || (!ext && !fileName.startsWith("."));
}

const DEFAULT_INCLUDE_EXTENSIONS = [
  "ts", "tsx", "js", "jsx", "mjs", "cjs", "json", "md", "mdx", "txt",
  "yaml", "yml", "toml", "py", "rb", "go", "rs", "java", "kt", "swift",
  "c", "cpp", "h", "hpp", "css", "scss", "less", "html", "vue", "svelte",
  "sql", "sh", "bash", "zsh", "fish", "ps1", "env", "gitignore",
  "dockerignore", "editorconfig",
];

const DEFAULT_EXCLUDE_DIRS = [
  "node_modules", ".git", "dist", "build", ".next", "coverage",
  "__pycache__", ".venv", "venv", ".cache", ".turbo", ".output",
  "out", "target", "bin", "obj",
];

async function getFileIndex(cwd) {
  // Walk the directory tree returning a list of relative paths
  const result = [];
  const stack = [cwd];
  const exclude = new Set(DEFAULT_EXCLUDE_DIRS);
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (exclude.has(entry.name)) continue;
      const abs = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        stack.push(abs);
      } else if (entry.isFile()) {
        result.push(path.relative(cwd, abs));
      }
    }
  }
  return result;
}

const DEFAULT_SEARCH_OPTIONS = {
  includeExtensions: DEFAULT_INCLUDE_EXTENSIONS,
  excludeDirs: DEFAULT_EXCLUDE_DIRS,
  maxResults: 100,
  contextLines: 2,
  maxDepth: 20,
};

function createSearchExecutor(options = {}) {
  const {
    includeExtensions = DEFAULT_INCLUDE_EXTENSIONS,
    excludeDirs = DEFAULT_EXCLUDE_DIRS,
    maxResults = 100,
    contextLines = 2,
    maxDepth = 20,
  } = options;
  const excludeDirsSet = new Set(excludeDirs);
  const includeExtensionsSet = new Set(includeExtensions.map((e) => e.toLowerCase()));

  return async function search(query, cwd, context = {}) {
    const signal = context.signal;
    if (signal?.aborted) throw new Error("Search operation aborted");

    const isRgAvailable = await checkRipgrepAvailable();
    let rgMatches = null;
    if (isRgAvailable) {
      rgMatches = await searchWithRipgrep(query, cwd, maxResults, contextLines, 5000, signal);
    }
    if (rgMatches) {
      const resultLines = [
        `Found ${rgMatches.length} result${rgMatches.length === 1 ? "" : "s"} for pattern: ${query}`,
        "",
      ];
      for (const match of rgMatches) {
        resultLines.push(`${match.file}:${match.line}:${match.column}`);
        resultLines.push(...match.context);
        resultLines.push("");
      }
      if (rgMatches.length >= maxResults) {
        resultLines.push(
          `(Showing first ${maxResults} results. Refine your search for more specific results.)`,
        );
      }
      return capSearchOutput(resultLines.join("\n"));
    }

    // Fallback: manual regex search
    let regex;
    try {
      regex = new RegExp(query, "gim");
    } catch (error) {
      throw new Error(
        `Invalid regex pattern: ${query}. ${error instanceof Error ? error.message : ""}`,
      );
    }

    const matches = [];
    let totalFilesSearched = 0;
    const fileList = await getFileIndex(cwd);
    for (const relativePath of fileList) {
      if (signal?.aborted) throw new Error("Search operation aborted");
      if (!shouldIncludeFile(relativePath, excludeDirsSet, includeExtensionsSet, maxDepth)) continue;
      if (matches.length >= maxResults) break;
      totalFilesSearched++;
      const filePath = path.join(cwd, relativePath);
      try {
        const content = await fsp.readFile(filePath, "utf-8");
        const lines = content.split("\n");
        for (let lineIdx = 0; lineIdx < lines.length; lineIdx++) {
          const line = lines[lineIdx];
          regex.lastIndex = 0;
          let match = regex.exec(line);
          while (match !== null) {
            if (matches.length >= maxResults) break;
            const contextStart = Math.max(0, lineIdx - contextLines);
            const contextEnd = Math.min(lines.length - 1, lineIdx + contextLines);
            const contextLinesArr = [];
            for (let i = contextStart; i <= contextEnd; i++) {
              const prefix = i === lineIdx ? ">" : " ";
              contextLinesArr.push(`${prefix} ${i + 1}: ${lines[i]}`);
            }
            matches.push({
              file: relativePath,
              line: lineIdx + 1,
              column: match.index + 1,
              match: match[0],
              context: contextLinesArr,
            });
            if (match.index === regex.lastIndex) regex.lastIndex++;
            match = regex.exec(line);
          }
        }
      } catch {}
    }

    if (matches.length === 0) {
      return `No results found for pattern: ${query}\nSearched ${totalFilesSearched} files.`;
    }
    const resultLines = [
      `Found ${matches.length} result${matches.length === 1 ? "" : "s"} for pattern: ${query}`,
      `Searched ${totalFilesSearched} files.`,
      "",
    ];
    for (const match of matches) {
      resultLines.push(`${match.file}:${match.line}:${match.column}`);
      resultLines.push(...match.context);
      resultLines.push("");
    }
    if (matches.length >= maxResults) {
      resultLines.push(
        `(Showing first ${maxResults} results. Refine your search for more specific results.)`,
      );
    }
    return capSearchOutput(resultLines.join("\n"));
  };
}

async function executeSearchCodebase(input, context = {}) {
  const opts = { ...DEFAULT_SEARCH_OPTIONS, ...(context.searchOptions ?? {}) };
  const search = createSearchExecutor(opts);
  const timeoutMs = context.searchTimeoutMs ?? 30_000;
  const cwd = context.cwd ?? process.cwd();

  let queries;
  if (typeof input === "string") queries = [input];
  else if (Array.isArray(input)) queries = input;
  else if (input && Array.isArray(input.queries))
    queries = typeof input.queries === "string" ? [input.queries] : input.queries;
  else throw new Error("Invalid search_codebase input");

  return Promise.all(
    queries.map(async (query) => {
      try {
        const results = await withTimeout(
          search(query, cwd, context),
          timeoutMs,
          `Search timed out after ${timeoutMs}ms`,
        );
        return { query, result: results, success: true };
      } catch (error) {
        return {
          query,
          result: "",
          error: `Search failed: ${formatError(error)}`,
          success: false,
        };
      }
    }),
  );
}

// =============================================================================
// run_commands
// =============================================================================

function createRollingCollector(maxChars) {
  const headLimit = Math.ceil(maxChars / 2);
  const tailLimit = Math.max(1, maxChars - headLimit);
  const decoder = new StringDecoder("utf8");
  let head = "";
  let tail = "";
  let totalChars = 0;

  const appendText = (text) => {
    if (!text) return;
    totalChars += text.length;
    const headRoom = headLimit - head.length;
    if (headRoom > 0) {
      head += text.slice(0, headRoom);
      tail = (tail + text.slice(headRoom)).slice(-tailLimit);
      return;
    }
    tail = (tail + text).slice(-tailLimit);
  };

  return {
    append(data) {
      appendText(decoder.write(data));
    },
    snapshot() {
      appendText(decoder.end());
      return {
        text: head + tail,
        totalChars,
        dropped: totalChars > head.length + tail.length,
      };
    },
  };
}

function spawnAndCollect(config, signal, timeoutMs, maxOutputChars, combineOutput) {
  return new Promise((resolve, reject) => {
    const isWindows = process.platform === "win32";
    const child = spawn(config.executable, config.args, {
      cwd: config.cwd,
      env: { ...process.env, ...(config.env ?? {}) },
      stdio: ["pipe", "pipe", "pipe"],
      detached: !isWindows,
      windowsHide: true,
    });
    const childPid = child.pid;
    const stdout = createRollingCollector(maxOutputChars);
    const stderr = createRollingCollector(maxOutputChars);
    let killed = false;
    let settled = false;
    const settle = (fn) => {
      if (settled) return;
      settled = true;
      fn();
    };
    const killProcessTree = () => {
      if (!childPid) return;
      if (isWindows) {
        const killer = spawn("taskkill", ["/pid", String(childPid), "/T", "/F"], {
          stdio: "ignore", shell: true, windowsHide: true,
        });
        killer.unref();
        return;
      }
      try {
        process.kill(-childPid, "SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    };
    const killAndReject = (error) => {
      killed = true;
      killProcessTree();
      settle(() => reject(error));
    };
    const timeout = setTimeout(
      () => killAndReject(new TimeoutError(`Command timed out after ${timeoutMs}ms`, timeoutMs)),
      timeoutMs,
    );
    const abortHandler = () => killAndReject(new Error("Command was aborted"));
    if (signal) signal.addEventListener("abort", abortHandler);
    const cleanup = () => {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abortHandler);
    };
    child.stdout?.on("data", (data) => stdout.append(data));
    child.stderr?.on("data", (data) => stderr.append(data));
    child.on("close", (code) => {
      cleanup();
      if (killed) return;
      const out = stdout.snapshot();
      const err = stderr.snapshot();
      if (code !== 0) {
        const exitCode = code ?? 1;
        let failureOutput = combineOutput
          ? out.text + (err.text ? `\n[stderr]\n${err.text}` : "")
          : out.text;
        const dropped = out.dropped || (combineOutput && err.dropped);
        const totalChars = combineOutput ? out.totalChars + err.totalChars : out.totalChars;
        if (dropped || failureOutput.length > maxOutputChars) {
          failureOutput = truncateCommandOutput(failureOutput, { maxChars: maxOutputChars, totalChars });
        }
        const result = failureOutput.length > 0
          ? `[Command exited with code ${exitCode}]\n${failureOutput}`
          : `[Command exited with code ${exitCode}]`;
        settle(() => reject(new CommandExitError(exitCode, result)));
      } else {
        let output = combineOutput
          ? out.text + (err.text ? `\n[stderr]\n${err.text}` : "")
          : out.text;
        const dropped = out.dropped || (combineOutput && err.dropped);
        if (dropped || output.length > maxOutputChars) {
          const totalChars = combineOutput ? out.totalChars + err.totalChars : out.totalChars;
          output = truncateCommandOutput(output, { maxChars: maxOutputChars, totalChars });
        }
        settle(() => resolve(output));
      }
    });
    child.on("error", (error) => {
      cleanup();
      settle(() => reject(new Error(`Failed to execute command: ${error.message}`)));
    });
  });
}

function createShellExecutor(options = {}) {
  const shell = options.shell ?? getDefaultShell(process.platform);
  const timeoutMs = options.timeoutMs ?? 30_000;
  const maxOutputChars = options.maxOutputChars ?? options.maxOutputBytes ?? MAX_COMMAND_OUTPUT_CHARS;
  const env = options.env ?? {};
  const combineOutput = options.combineOutput ?? true;

  return function exec(command, cwd, context = {}) {
    const isStructured = typeof command !== "string";
    return spawnAndCollect(
      {
        executable: isStructured ? command.command : shell,
        args: isStructured ? (command.args ?? []) : getShellArgs(shell, command),
        cwd,
        env,
      },
      context.signal,
      timeoutMs,
      maxOutputChars,
      combineOutput,
    );
  };
}

async function executeRunCommands(input, context = {}) {
  const exec = createShellExecutor(context.shellOptions ?? {});
  const timeoutMs = context.bashTimeoutMs ?? 30_000;
  const cwd = context.cwd ?? process.cwd();

  // Normalize input
  let commands;
  if (typeof input === "string") commands = [input];
  else if (Array.isArray(input)) {
    commands = input.map((c) => (typeof c === "string" ? c : c));
  } else if (input && Array.isArray(input.commands)) {
    commands = input.commands;
  } else if (input && typeof input.command === "string") {
    commands = [input.command];
  } else if (input && typeof input.cmd === "string") {
    commands = [input.cmd];
  } else {
    throw new Error("Invalid run_commands input");
  }

  return Promise.all(
    commands.map(async (command) => {
      const query = typeof command === "string" ? command : command.command;
      try {
        const output = await withTimeout(
          exec(command, cwd, context),
          timeoutMs,
          `Command timed out after ${timeoutMs}ms`,
        );
        return { query, result: output, success: true };
      } catch (error) {
        if (error instanceof CommandExitError) {
          return { query, result: error.output, error: error.message, success: false };
        }
        return {
          query,
          result: "",
          error: `Command failed: ${formatError(error)}`,
          success: false,
        };
      }
    }),
  );
}

// =============================================================================
// fetch_web_content
// =============================================================================

function htmlToText(html) {
  return (
    html
      .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
      .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/<(p|div|br|hr|h[1-6]|li|tr)[^>]*>/gi, "\n")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
      .replace(/\s+/g, " ")
      .replace(/\n\s+/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim()
  );
}

const DEFAULT_WEB_FETCH_OPTIONS = {
  timeoutMs: 30_000,
  maxResponseBytes: 5_000_000,
  userAgent: "Mozilla/5.0 (compatible; AgentBot/1.0)",
  headers: {},
  followRedirects: true,
};

function createWebFetchExecutor(options = {}) {
  const {
    timeoutMs = DEFAULT_WEB_FETCH_OPTIONS.timeoutMs,
    maxResponseBytes = DEFAULT_WEB_FETCH_OPTIONS.maxResponseBytes,
    userAgent = DEFAULT_WEB_FETCH_OPTIONS.userAgent,
    headers = {},
    followRedirects = true,
  } = options;

  return async function webFetch(url, prompt, context = {}) {
    let parsedUrl;
    try {
      parsedUrl = new URL(url);
    } catch {
      throw new Error(`Invalid URL: ${url}`);
    }
    if (!["http:", "https:"].includes(parsedUrl.protocol)) {
      throw new Error(`Invalid protocol: ${parsedUrl.protocol}. Only http and https are supported.`);
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    let contextAbortHandler;
    if (context.signal) {
      contextAbortHandler = () => controller.abort();
      context.signal.addEventListener("abort", contextAbortHandler);
    }
    try {
      const response = await fetch(url, {
        method: "GET",
        headers: {
          "User-Agent": userAgent,
          Accept:
            "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
          "Accept-Language": "en-US,en;q=0.9",
          ...headers,
        },
        redirect: followRedirects ? "follow" : "manual",
        signal: controller.signal,
      });
      clearTimeout(timeout);
      if (!followRedirects && response.status >= 300 && response.status < 400) {
        return `Redirect to: ${response.headers.get("location")}`;
      }
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      const contentType = response.headers.get("content-type") || "";
      const reader = response.body?.getReader();
      if (!reader) throw new Error("Failed to read response body");
      const chunks = [];
      let totalSize = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        totalSize += value.length;
        if (totalSize > maxResponseBytes) {
          reader.cancel();
          throw new Error(`Response too large: exceeded ${maxResponseBytes} bytes`);
        }
        chunks.push(value);
      }
      const buffer = new Uint8Array(totalSize);
      let offset = 0;
      for (const chunk of chunks) {
        buffer.set(chunk, offset);
        offset += chunk.length;
      }
      const text = new TextDecoder("utf-8").decode(buffer);
      let content;
      if (contentType.includes("text/html") || contentType.includes("application/xhtml")) {
        content = htmlToText(text);
      } else if (contentType.includes("application/json")) {
        try {
          content = JSON.stringify(JSON.parse(text), null, 2);
        } catch {
          content = text;
        }
      } else {
        content = text;
      }
      const outputLines = [
        `URL: ${url}`,
        `Content-Type: ${contentType}`,
        `Size: ${totalSize} bytes`,
        "",
        "--- Content ---",
        content.slice(0, 50_000),
      ];
      if (content.length > 50_000) {
        outputLines.push(
          `\n[Content truncated: showing first 50000 of ${content.length} characters]`,
        );
      }
      outputLines.push("", "--- Analysis Request ---", `Prompt: ${prompt}`);
      return outputLines.join("\n");
    } catch (error) {
      clearTimeout(timeout);
      if (error instanceof Error) {
        if (error.name === "AbortError") {
          throw new Error(`Request timed out after ${timeoutMs}ms`);
        }
        throw error;
      }
      throw new Error(`Fetch failed: ${String(error)}`);
    } finally {
      if (context.signal && contextAbortHandler) {
        context.signal.removeEventListener("abort", contextAbortHandler);
      }
    }
  };
}

async function executeFetchWebContent(input, context = {}) {
  const webFetch = createWebFetchExecutor(context.webFetchOptions ?? {});
  const timeoutMs = context.webFetchTimeoutMs ?? 30_000;

  if (!input || !Array.isArray(input.requests)) {
    throw new Error("fetch_web_content requires { requests: [{ url, prompt }, ...] }");
  }

  return Promise.all(
    input.requests.map(async (request) => {
      try {
        const content = await withTimeout(
          webFetch(request.url, request.prompt, context),
          timeoutMs,
          `Web fetch timed out after ${timeoutMs}ms`,
        );
        return { query: request.url, result: content, success: true };
      } catch (error) {
        return {
          query: request.url,
          result: "",
          error: `Error fetching web content: ${formatError(error)}`,
          success: false,
        };
      }
    }),
  );
}

// =============================================================================
// editor
// =============================================================================

function countOccurrences(content, needle) {
  if (needle.length === 0) return 0;
  return content.split(needle).length - 1;
}

function createLineDiff(oldContent, newContent, maxLines) {
  const oldLines = oldContent.split("\n");
  const newLines = newContent.split("\n");
  let start = 0;
  while (
    start < oldLines.length && start < newLines.length &&
    oldLines[start] === newLines[start]
  ) start++;
  let oldEnd = oldLines.length;
  let newEnd = newLines.length;
  while (
    oldEnd > start && newEnd > start &&
    oldLines[oldEnd - 1] === newLines[newEnd - 1]
  ) {
    oldEnd--;
    newEnd--;
  }
  const removedCount = oldEnd - start;
  const addedCount = newEnd - start;
  let removedBudget = removedCount;
  let addedBudget = addedCount;
  if (removedCount + addedCount > maxLines) {
    removedBudget = Math.min(removedCount, Math.max(Math.ceil(maxLines / 2), maxLines - addedCount));
    addedBudget = Math.min(addedCount, maxLines - removedBudget);
  }
  const out = ["```diff"];
  for (let i = start; i < start + removedBudget; i++) out.push(`-${i + 1}: ${oldLines[i]}`);
  for (let i = start; i < start + addedBudget; i++) out.push(`+${i + 1}: ${newLines[i]}`);
  const omittedRemoved = removedCount - removedBudget;
  const omittedAdded = addedCount - addedBudget;
  if (omittedRemoved > 0 || omittedAdded > 0) {
    out.push(`... diff truncated (${omittedRemoved} more removed, ${omittedAdded} more added lines) ...`);
  }
  out.push("```");
  return out.join("\n");
}

async function createFile(filePath, fileText, encoding) {
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  await fsp.writeFile(filePath, fileText, { encoding });
  return `File created successfully at: ${filePath}`;
}

async function fileExists(filePath) {
  try {
    await fsp.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function replaceInFile(filePath, oldStr, newStr, encoding, maxDiffLines) {
  const content = await fsp.readFile(filePath, encoding);
  const occurrences = countOccurrences(content, oldStr);
  if (occurrences === 0) {
    throw new Error(`No replacement performed: text not found in ${filePath}.`);
  }
  if (occurrences > 1) {
    throw new Error(`No replacement performed: multiple occurrences of text found in ${filePath}.`);
  }
  const updated = content.replace(oldStr, newStr ?? "");
  await fsp.writeFile(filePath, updated, { encoding });
  const diff = createLineDiff(content, updated, maxDiffLines);
  return `Edited ${filePath}\n${diff}`;
}

async function insertInFile(filePath, insertLineOneBased, newStr, encoding) {
  const content = await fsp.readFile(filePath, encoding);
  const lines = content.split("\n");
  const maxBoundaryLine = lines.length + 1;
  if (insertLineOneBased < 1 || insertLineOneBased > maxBoundaryLine) {
    throw new Error(
      `Invalid insert_line: ${insertLineOneBased}. insert_line must be a positive one-based boundary line in the range 1-${maxBoundaryLine}. Use ${maxBoundaryLine} to append at EOF.`,
    );
  }
  const insertLine = insertLineOneBased - 1;
  lines.splice(insertLine, 0, ...newStr.split("\n"));
  await fsp.writeFile(filePath, lines.join("\n"), { encoding });
  return `Inserted content at line ${insertLineOneBased} in ${filePath}.`;
}

const DEFAULT_EDITOR_OPTIONS = {
  encoding: "utf-8",
  restrictToCwd: true,
  maxDiffLines: 200,
};

function createEditorExecutor(options = {}) {
  const {
    encoding = DEFAULT_EDITOR_OPTIONS.encoding,
    restrictToCwd = DEFAULT_EDITOR_OPTIONS.restrictToCwd,
    maxDiffLines = DEFAULT_EDITOR_OPTIONS.maxDiffLines,
  } = options;

  return async function editor(input, cwd) {
    const filePath = resolveFilePath(cwd, input.path, restrictToCwd);
    if (input.insert_line != null) {
      return insertInFile(filePath, input.insert_line, input.new_text, encoding);
    }
    if (!(await fileExists(filePath))) {
      return createFile(filePath, input.new_text, encoding);
    }
    if (input.old_text == null) {
      throw new Error(
        "Parameter `old_text` is required when editing an existing file without `insert_line`",
      );
    }
    return replaceInFile(filePath, input.old_text, input.new_text, encoding, maxDiffLines);
  };
}

async function executeEditor(input, context = {}) {
  const opts = { ...DEFAULT_EDITOR_OPTIONS, ...(context.editorOptions ?? {}) };
  const editor = createEditorExecutor(opts);
  const timeoutMs = context.editorTimeoutMs ?? 30_000;
  const cwd = context.cwd ?? process.cwd();

  const operation = input.insert_line == null ? "edit" : "insert";
  const sizeError = getEditorSizeError(input);
  if (sizeError) {
    return { query: `${operation}:${input.path}`, result: "", error: sizeError, success: false };
  }
  try {
    const result = await withTimeout(
      editor(input, cwd),
      timeoutMs,
      `Editor operation timed out after ${timeoutMs}ms`,
    );
    return { query: `${operation}:${input.path}`, result, success: true };
  } catch (error) {
    return {
      query: `${operation}:${input.path}`,
      result: "",
      error: `Editor operation failed: ${formatError(error)}`,
      success: false,
    };
  }
}

function getEditorSizeError(input) {
  if (typeof input.old_text === "string" && input.old_text.length > INPUT_ARG_CHAR_LIMIT) {
    return `Editor input too large: old_text was ${input.old_text.length} characters, exceeding the recommended limit of ${INPUT_ARG_CHAR_LIMIT}. Split the edit into smaller tool calls so later tool calls are less likely to be truncated or time out.`;
  }
  if (input.new_text.length > INPUT_ARG_CHAR_LIMIT) {
    return `Editor input too large: new_text was ${input.new_text.length} characters, exceeding the recommended limit of ${INPUT_ARG_CHAR_LIMIT}. Split the edit into smaller tool calls so later tool calls are less likely to be truncated or time out.`;
  }
  return null;
}

// =============================================================================
// apply_patch
// =============================================================================

const PATCH_MARKERS = {
  BEGIN: "*** Begin Patch",
  END: "*** End Patch",
  ADD: "*** Add File: ",
  UPDATE: "*** Update File: ",
  DELETE: "*** Delete File: ",
  MOVE: "*** Move to: ",
  SECTION: "@@",
  END_FILE: "*** End of File",
};

const BASH_WRAPPERS = ["%%bash", "apply_patch", "EOF", "```"];

const PatchActionType = {
  ADD: "add",
  DELETE: "delete",
  UPDATE: "update",
};

class DiffError extends Error {
  constructor(message) {
    super(message);
    this.name = "DiffError";
  }
}

function canonicalize(input) {
  const punctuationMap = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‛": "'",
    " ": " ", " ": " ",
  };
  return input
    .normalize("NFC")
    .replace(/./gu, (char) => punctuationMap[char] ?? char)
    .replace(/\\`/g, "`")
    .replace(/\\'/g, "'")
    .replace(/\\"/g, '"');
}

function levenshteinDistance(s1, s2) {
  const rows = s2.length + 1;
  const cols = s1.length + 1;
  const matrix = new Array(rows * cols).fill(0);
  const at = (r, c) => matrix[r * cols + c] ?? 0;
  const set = (r, c, v) => { matrix[r * cols + c] = v; };
  for (let i = 0; i <= s2.length; i++) set(i, 0, i);
  for (let j = 0; j <= s1.length; j++) set(0, j, j);
  for (let i = 1; i <= s2.length; i++) {
    for (let j = 1; j <= s1.length; j++) {
      if (s2[i - 1] === s1[j - 1]) set(i, j, at(i - 1, j - 1));
      else set(i, j, 1 + Math.min(at(i - 1, j - 1), at(i, j - 1), at(i - 1, j)));
    }
  }
  return at(s2.length, s1.length);
}

function calculateSimilarity(s1, s2) {
  const longer = s1.length > s2.length ? s1 : s2;
  const shorter = s1.length > s2.length ? s2 : s1;
  if (longer.length === 0) return 1;
  return (longer.length - levenshteinDistance(shorter, longer)) / longer.length;
}

function findContext(lines, context, start, eof) {
  if (context.length === 0) return [start, 0, 1];
  let bestSimilarity = 0;
  const findCore = (startIdx) => {
    const canonicalContext = canonicalize(context.join("\n"));
    for (let i = startIdx; i < lines.length; i++) {
      const segment = canonicalize(lines.slice(i, i + context.length).join("\n"));
      if (segment === canonicalContext) return [i, 0, 1];
      const similarity = calculateSimilarity(segment, canonicalContext);
      if (similarity > bestSimilarity) bestSimilarity = similarity;
    }
    for (let i = startIdx; i < lines.length; i++) {
      const segment = canonicalize(lines.slice(i, i + context.length).map((l) => l.trimEnd()).join("\n"));
      const canonicalTrimmed = canonicalize(context.map((l) => l.trimEnd()).join("\n"));
      if (segment === canonicalTrimmed) return [i, 1, 1];
    }
    for (let i = startIdx; i < lines.length; i++) {
      const segment = canonicalize(lines.slice(i, i + context.length).map((l) => l.trim()).join("\n"));
      const canonicalTrimmed = canonicalize(context.map((l) => l.trim()).join("\n"));
      if (segment === canonicalTrimmed) return [i, 100, 1];
    }
    const threshold = 0.66;
    for (let i = startIdx; i < lines.length; i++) {
      const segment = canonicalize(lines.slice(i, i + context.length).join("\n"));
      const similarity = calculateSimilarity(segment, canonicalContext);
      if (similarity >= threshold) return [i, 1000, similarity];
      if (similarity > bestSimilarity) bestSimilarity = similarity;
    }
    return [-1, 0, bestSimilarity];
  };
  if (eof) {
    let [newIndex, fuzz, similarity] = findCore(lines.length - context.length);
    if (newIndex !== -1) return [newIndex, fuzz, similarity];
    [newIndex, fuzz, similarity] = findCore(start);
    return [newIndex, fuzz + 10000, similarity];
  }
  return findCore(start);
}

function peek(lines, initialIndex) {
  let index = initialIndex;
  const old = [];
  let delLines = [];
  let insLines = [];
  const chunks = [];
  let mode = "keep";
  const stopMarkers = [
    "@@", PATCH_MARKERS.END, PATCH_MARKERS.UPDATE,
    PATCH_MARKERS.DELETE, PATCH_MARKERS.ADD, PATCH_MARKERS.END_FILE,
  ];
  while (index < lines.length) {
    const sourceLine = lines[index];
    if (!sourceLine || stopMarkers.some((m) => sourceLine.startsWith(m.trim()))) break;
    if (sourceLine === "***") break;
    if (sourceLine.startsWith("***")) throw new DiffError(`Invalid line: ${sourceLine}`);
    index++;
    const previousMode = mode;
    let line = sourceLine;
    if (line[0] === "+") mode = "add";
    else if (line[0] === "-") mode = "delete";
    else if (line[0] === " ") mode = "keep";
    else { mode = "keep"; line = ` ${line}`; }
    line = line.slice(1);
    if (mode === "keep" && previousMode !== mode) {
      if (insLines.length || delLines.length) {
        chunks.push({ origIndex: old.length - delLines.length, delLines, insLines });
      }
      delLines = [];
      insLines = [];
    }
    if (mode === "delete") { delLines.push(line); old.push(line); }
    else if (mode === "add") { insLines.push(line); }
    else { old.push(line); }
  }
  if (insLines.length || delLines.length) {
    chunks.push({ origIndex: old.length - delLines.length, delLines, insLines });
  }
  if (index < lines.length && lines[index] === PATCH_MARKERS.END_FILE) {
    index++;
    return [old, chunks, index, true];
  }
  return [old, chunks, index, false];
}

class PatchParser {
  constructor(lines, currentFiles) {
    this.lines = lines;
    this.currentFiles = currentFiles;
    this.patch = { actions: {}, warnings: [] };
    this.index = 0;
    this.fuzz = 0;
    this.currentPath = undefined;
  }

  parse() {
    this.skipBeginSentinel();
    while (this.hasMoreLines() && !this.isEndMarker()) this.parseNextAction();
    if (this.patch.warnings?.length === 0) delete this.patch.warnings;
    return { patch: this.patch, fuzz: this.fuzz };
  }

  addWarning(warning) {
    if (!this.patch.warnings) this.patch.warnings = [];
    this.patch.warnings.push(warning);
  }

  skipBeginSentinel() {
    if (this.lines[this.index]?.startsWith(PATCH_MARKERS.BEGIN)) this.index++;
  }

  hasMoreLines() { return this.index < this.lines.length; }
  isEndMarker() { return this.lines[this.index]?.startsWith(PATCH_MARKERS.END) ?? false; }

  parseNextAction() {
    const line = this.lines[this.index];
    if (line?.startsWith(PATCH_MARKERS.UPDATE)) {
      this.parseUpdate(line.substring(PATCH_MARKERS.UPDATE.length).trim()); return;
    }
    if (line?.startsWith(PATCH_MARKERS.DELETE)) {
      this.parseDelete(line.substring(PATCH_MARKERS.DELETE.length).trim()); return;
    }
    if (line?.startsWith(PATCH_MARKERS.ADD)) {
      this.parseAdd(line.substring(PATCH_MARKERS.ADD.length).trim()); return;
    }
    throw new DiffError(`Unknown line while parsing: ${line}`);
  }

  checkDuplicate(p, op) {
    if (p in this.patch.actions) throw new DiffError(`Duplicate ${op} for file: ${p}`);
  }

  parseUpdate(filePath) {
    this.checkDuplicate(filePath, "update");
    this.currentPath = filePath;
    this.index++;
    const movePath = this.lines[this.index]?.startsWith(PATCH_MARKERS.MOVE)
      ? (this.lines[this.index++] ?? "").substring(PATCH_MARKERS.MOVE.length).trim()
      : undefined;
    if (!(filePath in this.currentFiles)) {
      throw new DiffError(`Update File Error: Missing File: ${filePath}`);
    }
    const text = this.currentFiles[filePath] ?? "";
    const action = this.parseUpdateFile(text, filePath);
    action.movePath = movePath;
    this.patch.actions[filePath] = action;
    this.currentPath = undefined;
  }

  parseUpdateFile(text, filePath) {
    const action = { type: PatchActionType.UPDATE, chunks: [] };
    const fileLines = text.split("\n");
    let index = 0;
    const stopMarkers = [
      PATCH_MARKERS.END, PATCH_MARKERS.UPDATE, PATCH_MARKERS.DELETE,
      PATCH_MARKERS.ADD, PATCH_MARKERS.END_FILE,
    ];
    while (!stopMarkers.some((m) => this.lines[this.index]?.startsWith(m.trim()))) {
      const currentLine = this.lines[this.index];
      const defStr = currentLine?.startsWith("@@ ") ? currentLine.substring(3) : undefined;
      const sectionStr = currentLine === "@@" ? currentLine : undefined;
      if (defStr !== undefined || sectionStr !== undefined) this.index++;
      else if (index !== 0) throw new DiffError(`Invalid Line:\n${this.lines[this.index]}`);
      if (defStr?.trim()) {
        const canonDefStr = canonicalize(defStr.trim());
        for (let i = index; i < fileLines.length; i++) {
          const fileLine = fileLines[i];
          if (fileLine && (canonicalize(fileLine) === canonDefStr || canonicalize(fileLine.trim()) === canonDefStr)) {
            index = i + 1;
            if (canonicalize(fileLine.trim()) === canonDefStr && canonicalize(fileLine) !== canonDefStr) {
              this.fuzz++;
            }
            break;
          }
        }
      }
      const [nextChunkContext, chunks, endPatchIndex, eof] = peek(this.lines, this.index);
      const [newIndex, fuzz, similarity] = findContext(fileLines, nextChunkContext, index, eof);
      if (newIndex === -1) {
        const contextText = nextChunkContext.join("\n");
        this.addWarning({
          path: this.currentPath || filePath,
          chunkIndex: action.chunks.length,
          message: `Could not find matching context (similarity: ${similarity.toFixed(2)}). Chunk skipped.`,
          context: contextText.length > 200 ? `${contextText.substring(0, 200)}...` : contextText,
        });
        this.index = endPatchIndex;
      } else {
        this.fuzz += fuzz;
        for (const chunk of chunks) {
          chunk.origIndex += newIndex;
          action.chunks.push(chunk);
        }
        index = newIndex + nextChunkContext.length;
        this.index = endPatchIndex;
      }
    }
    return action;
  }

  parseDelete(filePath) {
    this.checkDuplicate(filePath, "delete");
    if (!(filePath in this.currentFiles)) {
      throw new DiffError(`Delete File Error: Missing File: ${filePath}`);
    }
    this.patch.actions[filePath] = { type: PatchActionType.DELETE, chunks: [] };
    this.index++;
  }

  parseAdd(filePath) {
    this.checkDuplicate(filePath, "add");
    if (filePath in this.currentFiles) {
      throw new DiffError(`Add File Error: File already exists: ${filePath}`);
    }
    this.index++;
    const lines = [];
    const stopMarkers = [
      PATCH_MARKERS.END, PATCH_MARKERS.UPDATE, PATCH_MARKERS.DELETE, PATCH_MARKERS.ADD,
    ];
    while (
      this.hasMoreLines() &&
      !stopMarkers.some((m) => this.lines[this.index]?.startsWith(m.trim()))
    ) {
      const line = this.lines[this.index++];
      if (line === undefined) break;
      if (!line.startsWith("+")) throw new DiffError(`Invalid Add File line (missing '+'): ${line}`);
      lines.push(line.substring(1));
    }
    this.patch.actions[filePath] = { type: PatchActionType.ADD, newFile: lines.join("\n"), chunks: [] };
  }
}

function normalizeLineEndings(input) {
  return input.split("\n").map((line) => line.replace(/\r$/, ""));
}

function isWrapperLine(line) {
  if (line.trim() === "") return false;
  return BASH_WRAPPERS.some((w) => line.startsWith(w));
}

function trimWrapperLines(lines) {
  let start = 0, end = lines.length;
  while (start < end && isWrapperLine(lines[start] ?? "")) start++;
  while (end > start && isWrapperLine(lines[end - 1] ?? "")) end--;
  return lines.slice(start, end);
}

function normalizePatchInput(input) {
  const rawLines = normalizeLineEndings(input);
  const beginIndex = rawLines.findIndex((l) => l.startsWith(PATCH_MARKERS.BEGIN));
  let endIndex = -1;
  for (let i = rawLines.length - 1; i >= 0; i--) {
    if (rawLines[i]?.startsWith(PATCH_MARKERS.END)) { endIndex = i; break; }
  }
  if (beginIndex !== -1 || endIndex !== -1) {
    if (beginIndex === -1 || endIndex === -1 || endIndex < beginIndex) {
      throw new DiffError("Invalid patch text - incomplete sentinels. Try breaking it into smaller patches.");
    }
    return { lines: rawLines.slice(beginIndex, endIndex + 1) };
  }
  const stripped = trimWrapperLines(rawLines);
  while (stripped.length > 0 && stripped[0] === "") stripped.shift();
  while (stripped.length > 0 && stripped[stripped.length - 1] === "") stripped.pop();
  return { lines: [PATCH_MARKERS.BEGIN, ...stripped, PATCH_MARKERS.END] };
}

function extractFilesForOperations(lines, markers) {
  const files = new Set();
  for (const line of lines) {
    for (const marker of markers) {
      if (line.startsWith(marker)) { files.add(line.substring(marker.length).trim()); break; }
    }
  }
  return [...files];
}

function applyChunks(content, chunks, filePath) {
  if (chunks.length === 0) return content;
  const lines = content.split("\n");
  const result = [];
  let currentIndex = 0;
  for (const chunk of chunks) {
    if (chunk.origIndex > lines.length) {
      throw new DiffError(`${filePath}: chunk.origIndex ${chunk.origIndex} > lines.length ${lines.length}`);
    }
    if (currentIndex > chunk.origIndex) {
      throw new DiffError(`${filePath}: currentIndex ${currentIndex} > chunk.origIndex ${chunk.origIndex}`);
    }
    result.push(...lines.slice(currentIndex, chunk.origIndex));
    result.push(...chunk.insLines);
    currentIndex = chunk.origIndex + chunk.delLines.length;
  }
  result.push(...lines.slice(currentIndex));
  return result.join("\n");
}

async function loadFiles(lines, cwd, encoding, restrictToCwd) {
  const filesToLoad = extractFilesForOperations(lines, [PATCH_MARKERS.UPDATE, PATCH_MARKERS.DELETE]);
  const files = {};
  for (const filePath of filesToLoad) {
    const absolutePath = resolveFilePath(cwd, filePath, restrictToCwd);
    let fileContent;
    try {
      fileContent = await fsp.readFile(absolutePath, encoding);
    } catch {
      throw new DiffError(`File not found: ${filePath}`);
    }
    files[filePath] = fileContent.replace(/\r\n/g, "\n");
  }
  return files;
}

function patchToChanges(patch, originalFiles) {
  const changes = {};
  for (const [filePath, action] of Object.entries(patch.actions)) {
    switch (action.type) {
      case PatchActionType.DELETE:
        changes[filePath] = { type: PatchActionType.DELETE, oldContent: originalFiles[filePath] };
        break;
      case PatchActionType.ADD:
        if (action.newFile === undefined) throw new DiffError("ADD action without file content");
        changes[filePath] = { type: PatchActionType.ADD, newContent: action.newFile };
        break;
      case PatchActionType.UPDATE:
        changes[filePath] = {
          type: PatchActionType.UPDATE,
          oldContent: originalFiles[filePath],
          newContent: applyChunks(originalFiles[filePath] ?? "", action.chunks, filePath),
          movePath: action.movePath,
        };
        break;
    }
  }
  return changes;
}

function formatSkippedHunkFailure(warnings) {
  const lines = [`Patch could not be applied because ${warnings.length} hunk${warnings.length === 1 ? "" : "s"} did not match the current file content.`];
  for (const w of warnings) {
    const hunk = w.chunkIndex === undefined ? "unknown" : String(w.chunkIndex + 1);
    lines.push(`${w.path}: hunk ${hunk}: ${w.message}`);
    if (w.context) lines.push(`Context:\n${w.context}`);
  }
  return lines.join("\n");
}

async function applyChanges(changes, cwd, encoding, restrictToCwd) {
  const touched = [];
  for (const [filePath, change] of Object.entries(changes)) {
    const sourceAbsPath = resolveFilePath(cwd, filePath, restrictToCwd);
    switch (change.type) {
      case PatchActionType.DELETE:
        await fsp.rm(sourceAbsPath, { force: true });
        touched.push(`${filePath}: [deleted]`);
        break;
      case PatchActionType.ADD:
        if (change.newContent === undefined) throw new DiffError(`Cannot create ${filePath} with no content`);
        await fsp.mkdir(path.dirname(sourceAbsPath), { recursive: true });
        await fsp.writeFile(sourceAbsPath, change.newContent, { encoding });
        touched.push(filePath);
        break;
      case PatchActionType.UPDATE: {
        if (change.newContent === undefined) {
          throw new DiffError(`UPDATE change for ${filePath} has no new content`);
        }
        if (change.movePath) {
          const moveAbsPath = resolveFilePath(cwd, change.movePath, restrictToCwd);
          await fsp.mkdir(path.dirname(moveAbsPath), { recursive: true });
          await fsp.writeFile(moveAbsPath, change.newContent, { encoding });
          await fsp.rm(sourceAbsPath, { force: true });
          touched.push(`${filePath} -> ${change.movePath}`);
        } else {
          await fsp.writeFile(sourceAbsPath, change.newContent, { encoding });
          touched.push(filePath);
        }
        break;
      }
    }
  }
  return touched;
}

const DEFAULT_APPLY_PATCH_OPTIONS = {
  encoding: "utf-8",
  restrictToCwd: true,
};

function createApplyPatchExecutor(options = {}) {
  const { encoding = DEFAULT_APPLY_PATCH_OPTIONS.encoding, restrictToCwd = DEFAULT_APPLY_PATCH_OPTIONS.restrictToCwd } = options;

  return async function applyPatch(input, cwd) {
    const normalizedInput = normalizePatchInput(input.input);
    const currentFiles = await loadFiles(normalizedInput.lines, cwd, encoding, restrictToCwd);
    const parser = new PatchParser(normalizedInput.lines, currentFiles);
    const { patch, fuzz } = parser.parse();
    if (patch.warnings && patch.warnings.length > 0) {
      throw new DiffError(formatSkippedHunkFailure(patch.warnings));
    }
    const changes = patchToChanges(patch, currentFiles);
    const touched = await applyChanges(changes, cwd, encoding, restrictToCwd);
    const responseLines = ["Successfully applied patch to the following files:"];
    for (const file of touched) responseLines.push(file);
    if (fuzz > 0) responseLines.push(`Note: Patch applied with fuzz factor ${fuzz}`);
    return responseLines.join("\n");
  };
}

async function executeApplyPatch(input, context = {}) {
  const opts = { ...DEFAULT_APPLY_PATCH_OPTIONS, ...(context.applyPatchOptions ?? {}) };
  const applyPatch = createApplyPatchExecutor(opts);
  const timeoutMs = context.applyPatchTimeoutMs ?? 30_000;
  const cwd = context.cwd ?? process.cwd();

  const patchInput = typeof input === "string" ? input : input.input;
  try {
    const result = await withTimeout(
      applyPatch({ input: patchInput }, cwd),
      timeoutMs,
      `apply_patch timed out after ${timeoutMs}ms`,
    );
    return { query: "apply_patch", result, success: true };
  } catch (error) {
    return {
      query: "apply_patch",
      result: "",
      error: `apply_patch failed: ${formatError(error)}`,
      success: false,
    };
  }
}

// =============================================================================
// Public API
// =============================================================================

const tools = {
  read_files: { execute: executeReadFiles },
  search_codebase: { execute: executeSearchCodebase },
  run_commands: { execute: executeRunCommands },
  fetch_web_content: { execute: executeFetchWebContent },
  editor: { execute: executeEditor },
  apply_patch: { execute: executeApplyPatch },
};

module.exports = {
  tools,
  // Direct executors for advanced use:
  executeReadFiles,
  executeSearchCodebase,
  executeRunCommands,
  executeFetchWebContent,
  executeEditor,
  executeApplyPatch,
  // Factories for custom options:
  createReadFilesExecutor,
  createSearchExecutor,
  createShellExecutor,
  createWebFetchExecutor,
  createEditorExecutor,
  createApplyPatchExecutor,
  // Errors:
  TimeoutError,
  CommandExitError,
  DiffError,
};
