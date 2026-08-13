/**
 * jarvis/tools/fileTools.ts — File I/O with LRU Read Cache
 *
 * Upgrades over v1:
 *  - Reads now go through getCachedFile() → huge speedup on repeated reads
 *  - Writes invalidate the cache for the written file
 *  - Edits invalidate after patching
 *  - LS output is enriched with sizes and type indicators
 *  - Bash uses platform-correct shell (powershell on Windows)
 */

import { execSync } from 'child_process'
import { mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync, statSync } from 'fs'
import { dirname, relative, resolve, basename } from 'path'
import { getCachedFile, setCachedFile, invalidateCachedFile } from '../cache.js'
import type { JarvisToolDefinition, JarvisToolEntry } from './index.js'

function createDefinition(name: string, description: string, required: string[]) {
  return {
    type: 'function' as const,
    function: {
      name,
      description,
      parameters: {
        type: 'object' as const,
        properties: {
          file_path: { type: 'string', description: 'Path to the file' },
          content: { type: 'string', description: 'Full file content to write' },
          old_string: { type: 'string', description: 'Exact text to replace' },
          new_string: { type: 'string', description: 'Replacement text' },
          start_line: { type: 'number', description: '1-based start line' },
          end_line: { type: 'number', description: '1-based end line' },
          replace_all: { type: 'boolean', description: 'Replace all matches if true' },
          command: { type: 'string', description: 'Shell command to run' },
          timeout: { type: 'number', description: 'Timeout in ms' },
          path: { type: 'string', description: 'Directory path to list' },
        },
        required,
      },
    },
  }
}

// ─── Read (with LRU cache) ────────────────────────────────────────────────────

function readFileHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const fp = resolve(cwd, args.file_path as string)

  const startLine = (args.start_line as number) ?? 1
  const endLine = args.end_line as number | undefined
  const isFullRead = startLine === 1 && !endLine

  // Use cache only for full reads
  if (isFullRead) {
    const cached = getCachedFile(fp)
    if (cached) {
      const lines = cached.split('\n')
      return lines.map((line, idx) => `${String(idx + 1).padStart(4)} │ ${line}`).join('\n')
    }
  }

  let content: string
  try {
    content = readFileSync(fp, 'utf8')
  } catch (e: unknown) {
    return `ERROR: Cannot read file: ${(e as Error).message}`
  }

  if (isFullRead) {
    setCachedFile(fp, content)
  }

  const lines = content.split('\n')
  const start = Math.max(0, startLine - 1)
  const end = endLine ?? lines.length

  return lines.slice(start, end).map((line, idx) =>
    `${String(start + idx + 1).padStart(4)} │ ${line}`
  ).join('\n')
}

// ─── Write (invalidates cache) ────────────────────────────────────────────────

function writeFileHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const fp = resolve(cwd, args.file_path as string)
  mkdirSync(dirname(fp), { recursive: true })
  writeFileSync(fp, args.content as string, 'utf8')
  invalidateCachedFile(fp)
  const lines = (args.content as string).split('\n').length
  return `Wrote ${lines} lines to ${relative(cwd, fp)}`
}

// ─── Edit (read → patch → write, cache-aware) ─────────────────────────────────

function editFileHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const fp = resolve(cwd, args.file_path as string)
  if (!existsSync(fp)) return `ERROR: File does not exist: ${relative(cwd, fp)}`

  // Prefer cached content to avoid disk read
  const src = getCachedFile(fp) ?? readFileSync(fp, 'utf8')
  const oldValue = args.old_string as string

  if (!src.includes(oldValue)) {
    const lines = src.split('\n')
    const firstLine = oldValue.split('\n')[0]?.trim() ?? ''
    const near = lines.findIndex(l => l.includes(firstLine))
    return `ERROR: old_string not found in ${basename(fp)}.${near >= 0 ? ` (Closest match near line ${near + 1})` : ' Check whitespace/indentation.'}`
  }

  const updated = args.replace_all
    ? src.replaceAll(oldValue, args.new_string as string)
    : src.replace(oldValue, args.new_string as string)

  writeFileSync(fp, updated, 'utf8')
  invalidateCachedFile(fp)

  const changedLines = (args.new_string as string).split('\n').length
  return `Edited ${relative(cwd, fp)} (${changedLines} lines updated)`
}

// ─── Bash (platform-aware shell) ─────────────────────────────────────────────

function bashHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const cmd = args.command as string
  const to = (args.timeout as number) ?? 30_000
  const shell = process.platform === 'win32' ? 'powershell.exe' : '/bin/bash'
  try {
    const out = execSync(cmd, { cwd, timeout: to, maxBuffer: 10 << 20, shell })
    return out.toString().trim() || '(command succeeded, no output)'
  } catch (e: unknown) {
    const err = e as Error & { stdout?: Buffer; stderr?: Buffer; status?: number }
    const parts = [err.stdout?.toString().trim(), err.stderr?.toString().trim()].filter(Boolean)
    return parts.join('\n') || `Exit ${err.status ?? 1}: ${err.message}`
  }
}

// ─── LS (enriched with sizes and type indicators) ─────────────────────────────

function lsHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const dir = resolve(cwd, (args.path as string) ?? '.')
  try {
    const entries = readdirSync(dir).sort().map(entry => {
      try {
        const st = statSync(resolve(dir, entry))
        if (st.isDirectory()) return `${entry}/`
        const kb = (st.size / 1024).toFixed(1)
        return `${entry} (${kb} KB)`
      } catch {
        return entry
      }
    })
    return `${relative(cwd, dir) || '.'}:\n${entries.join('\n')}`
  } catch {
    return `Cannot list: ${dir}`
  }
}

// ─── Exports ──────────────────────────────────────────────────────────────────

export function createFileToolEntries(): JarvisToolEntry[] {
  return [
    {
      definition: createDefinition('Read', 'Read a file from disk. Uses an LRU cache — repeated reads are instant. Supports start_line/end_line for large files.', ['file_path']) as JarvisToolDefinition,
      handler: readFileHandler,
    },
    {
      definition: createDefinition('Write', 'Write a complete file to disk (creates parent dirs). Invalidates read cache.', ['file_path', 'content']) as JarvisToolDefinition,
      handler: writeFileHandler,
    },
    {
      definition: createDefinition('FileWrite', 'Write a complete file to disk (alias for Write).', ['file_path', 'content']) as JarvisToolDefinition,
      handler: writeFileHandler,
    },
    {
      definition: createDefinition('Edit', 'Replace an EXACT string in a file. old_string must match exactly including whitespace. Returns error with nearest match hint if not found.', ['file_path', 'old_string', 'new_string']) as JarvisToolDefinition,
      handler: editFileHandler,
    },
    {
      definition: createDefinition('FileEdit', 'Replace an exact string in a file (alias for Edit).', ['file_path', 'old_string', 'new_string']) as JarvisToolDefinition,
      handler: editFileHandler,
    },
    {
      definition: createDefinition('Bash', 'Execute a shell command and return output. Platform-aware (PowerShell on Windows, bash on Linux/macOS).', ['command']) as JarvisToolDefinition,
      handler: bashHandler,
    },
    {
      definition: createDefinition('LS', 'List directory contents with file sizes and type indicators.', []) as JarvisToolDefinition,
      handler: lsHandler,
    },
  ]
}
