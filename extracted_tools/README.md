# Extracted Tools

Pure implementations of the core Cline SDK tools, stripped of:

- **VS Code extension APIs** — no `vscode` module references
- **Anthropic-specific wrappers** — no `createTool`, `validateWithZod`, `AgentToolContext`
- **State management** — no telemetry, session IDs, agent IDs, or run IDs
- **Zod schemas** — replaced with pure JSON schemas (Gemini-compatible)
- **`additionalProperties`** — removed throughout (Gemini's strict schema mode rejects it)

## Files

| File | Purpose |
|------|---------|
| `schemas.json` | Pure JSON schemas for all six tools. Use as `tools` for Gemini's `functionDeclarations`. |
| `tools.js` | Pure Node.js implementations. Each `executeX(input, context?)` returns an array of `ToolOperationResult` or a single result object. |

## Tools

| Tool | Schema name | What it does |
|------|------------|--------------|
| `read_files` | `read_files` | Read text/image files with optional line ranges |
| `search_codebase` | `search_codebase` | Regex search (ripgrep with Node fallback) |
| `run_commands` | `run_commands` | Non-interactive shell command execution |
| `fetch_web_content` | `fetch_web_content` | HTTP fetch with HTML→text and JSON pretty-print |
| `editor` | `editor` | Create / replace / insert text in files |
| `apply_patch` | `apply_patch` | Apply canonical freeform patch grammar to files |

## Schema format

Each entry in `schemas.json` is the tool's JSON schema, suitable for passing directly to the Gemini API as a function declaration. None of the schemas include `additionalProperties` (Gemini strict mode rejects it).

## Runtime API

```js
const { tools, executeReadFiles, executeSearchCodebase, ... } = require('./tools.js');

// All executors are pure: (input, context?) => Promise<result[]>
const results = await executeReadFiles({ files: [{ path: "/abs/path.ts" }] });
//   => [{ query: "/abs/path.ts", result: "1 | ...", success: true }, ...]

const commands = await executeRunCommands({ commands: ["ls -la"] }, { cwd: "/tmp" });
//   => [{ query: "ls -la", result: "...", success: true }, ...]
```

The optional `context` parameter is a plain object that can carry:
- `cwd` — current working directory
- `signal` — AbortSignal for cancellation
- `*TimeoutMs` — per-tool timeout override
- `*Options` — per-tool options bag (passed to that tool's factory)

## Differences from the source

1. **No `createTool` wrapper.** Tools are plain async functions, not `AgentTool` objects.
2. **No zod.** Input is taken as `unknown` and the executor normalizes flexible input shapes (string / array / object with various key names).
3. **No telemetry.** Removed `captureRunCommandsTimeout`, `getToolContextTelemetry`, `getStringMetadata`.
4. **No Anthropic-specific shell selection.** Uses `getDefaultShell(platform)` directly. No `ClineShellService` or platform-specific prompt switching — the description is single-platform.
5. **No VS Code coupling.** No `vscode.workspace.fs`, no `Uri` parsing, no `os.tmpdir()` quirks removed.
6. **Pure CommonJS** for portability — works without a TypeScript compiler.

## What was removed (not core)

- `apply_patch` parser and its tests (kept)
- `agent_id`, `session_id`, `run_id`, `tool_call_id` plumbing
- `getCoreAcpToolNames`, `getCoreHeadlessToolNames` (ACP/headless filtering)
- `getCoreBuiltinToolCatalog` (UI catalog metadata)
- `ToolRoutingRule` (Anthropic- and OpenAI-specific model routing)
- `ToolPresets` (preset configuration objects)
- `TeamSpawnAgent` / `TeamConfiguredAgent` (subagent orchestration)
- `ask_question`, `submit_and_exit`, `skills` (interaction-level tools, not file/terminal core)
- `MCP` (Model Context Protocol — extensible external tools, not a core tool)
- `Lifecyle.completesRun` (Anthropic-orchestrator convention)
- All `.test.ts` files
