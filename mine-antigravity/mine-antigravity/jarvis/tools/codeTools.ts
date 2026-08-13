/**
 * jarvis/tools/codeTools.ts — Code Analysis, Test Runner, Formatter
 *
 * New tools specifically for code-heavy tasks:
 *  - CodeAnalyze: static analysis, imports/exports/deps
 *  - RunTests:    runs test suite and formats results
 *  - FormatCode:  lints/formats a file using available formatters
 */

import { execSync, spawnSync } from 'child_process'
import { existsSync, readFileSync } from 'fs'
import { extname, relative, resolve } from 'path'
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
          file_path: { type: 'string', description: 'Path to the file to analyze or format' },
          path: { type: 'string', description: 'Directory or file path' },
          command: { type: 'string', description: 'Custom test command override' },
          timeout: { type: 'number', description: 'Timeout in ms (default: 60000)' },
          include: { type: 'string', description: 'File glob filter' },
        },
        required,
      },
    },
  }
}

// ─── CodeAnalyze ─────────────────────────────────────────────────────────────

function codeAnalyzeHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const filePath = resolve(cwd, (args.file_path ?? args.path) as string)
  if (!existsSync(filePath)) return `ERROR: File not found: ${relative(cwd, filePath)}`

  const src = readFileSync(filePath, 'utf8')
  const ext = extname(filePath)
  const lines = src.split('\n')
  const loc = lines.length

  const results: string[] = [`Code Analysis: ${relative(cwd, filePath)}`, '─'.repeat(50)]

  results.push(`Lines of code: ${loc}`)
  results.push(`File size: ${(src.length / 1024).toFixed(1)} KB`)

  // Imports / dependencies
  if (['.ts', '.tsx', '.js', '.mjs', '.jsx'].includes(ext)) {
    const imports = lines.filter(l => /^\s*(import|require\s*\()/.test(l))
    const exports = lines.filter(l => /^\s*export\s/.test(l))
    const functions = lines.filter(l => /^\s*(export\s+)?(async\s+)?function\s+\w+/.test(l) || /^\s*(export\s+)?const\s+\w+\s*=\s*(async\s+)?\(/.test(l))
    const classes = lines.filter(l => /^\s*(export\s+)?(abstract\s+)?class\s+\w+/.test(l))
    const todos = lines.filter(l => /\bTODO\b|\bFIXME\b|\bHACK\b/i.test(l))

    results.push('')
    results.push(`Imports (${imports.length}):`)
    imports.slice(0, 12).forEach(l => results.push(`  ${l.trim()}`))
    if (imports.length > 12) results.push(`  … and ${imports.length - 12} more`)

    results.push('')
    results.push(`Exports (${exports.length}):`)
    exports.slice(0, 8).forEach(l => results.push(`  ${l.trim()}`))

    results.push('')
    results.push(`Functions/Consts (${functions.length}):`)
    functions.slice(0, 10).forEach(l => results.push(`  ${l.trim()}`))

    if (classes.length > 0) {
      results.push('')
      results.push(`Classes (${classes.length}):`)
      classes.forEach(l => results.push(`  ${l.trim()}`))
    }

    if (todos.length > 0) {
      results.push('')
      results.push(`TODOs/FIXMEs (${todos.length}):`)
      todos.forEach((l, i) => {
        const lineNum = lines.indexOf(l) + 1
        results.push(`  Line ${lineNum}: ${l.trim()}`)
        if (i >= 6) { results.push(`  … and ${todos.length - 7} more`); return false }
      })
    }

    // Complexity hint: long functions
    let maxFnLength = 0
    let inFnStart = -1
    let braceDepth = 0
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]
      if (/^\s*(export\s+)?(async\s+)?function\s+/.test(line) && braceDepth === 0) {
        inFnStart = i
      }
      braceDepth += (line.match(/\{/g) ?? []).length
      braceDepth -= (line.match(/\}/g) ?? []).length
      if (inFnStart >= 0 && braceDepth === 0) {
        const len = i - inFnStart + 1
        if (len > maxFnLength) maxFnLength = len
        inFnStart = -1
      }
    }
    if (maxFnLength > 0) {
      results.push('')
      results.push(`Longest function: ~${maxFnLength} lines ${maxFnLength > 50 ? '⚠ Consider splitting' : '✓'}`)
    }
  }

  // Python analysis
  if (ext === '.py') {
    const imports = lines.filter(l => /^\s*(import|from)\s/.test(l))
    const defs = lines.filter(l => /^\s*def\s+/.test(l))
    const classes = lines.filter(l => /^\s*class\s+/.test(l))
    results.push(`\nImports: ${imports.length} | Functions: ${defs.length} | Classes: ${classes.length}`)
    defs.slice(0, 10).forEach(l => results.push(`  ${l.trim()}`))
  }

  return results.join('\n')
}

// ─── RunTests ────────────────────────────────────────────────────────────────

function runTestsHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const timeout = (args.timeout as number) ?? 60_000
  const customCmd = args.command as string | undefined

  let cmd: string

  if (customCmd) {
    cmd = customCmd
  } else if (existsSync(resolve(cwd, 'package.json'))) {
    const pkg = JSON.parse(readFileSync(resolve(cwd, 'package.json'), 'utf8'))
    if (pkg.scripts?.test) {
      cmd = process.platform === 'win32' ? 'npm test' : 'npm test'
    } else if (pkg.scripts?.['test:unit']) {
      cmd = 'npm run test:unit'
    } else {
      cmd = 'bun test'
    }
  } else if (existsSync(resolve(cwd, 'pytest.ini')) || existsSync(resolve(cwd, 'pyproject.toml'))) {
    cmd = 'python -m pytest -v --tb=short'
  } else {
    cmd = 'bun test'
  }

  const start = Date.now()
  try {
    const shell = process.platform === 'win32' ? 'powershell.exe' : '/bin/bash'
    const out = execSync(cmd, { cwd, timeout, maxBuffer: 10 << 20, shell })
    const elapsed = Date.now() - start
    return `Tests passed ✓ (${elapsed}ms)\nCommand: ${cmd}\n\n${out.toString().trim()}`
  } catch (e: unknown) {
    const err = e as Error & { stdout?: Buffer; stderr?: Buffer; status?: number }
    const elapsed = Date.now() - start
    const output = [err.stdout?.toString().trim(), err.stderr?.toString().trim()].filter(Boolean).join('\n')
    return `Tests failed ✗ (${elapsed}ms, exit ${err.status ?? 1})\nCommand: ${cmd}\n\n${output || err.message}`
  }
}

// ─── FormatCode ──────────────────────────────────────────────────────────────

function formatCodeHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const filePath = resolve(cwd, args.file_path as string)
  if (!existsSync(filePath)) return `ERROR: File not found: ${relative(cwd, filePath)}`

  const ext = extname(filePath)
  const shell = process.platform === 'win32' ? 'powershell.exe' : '/bin/bash'
  const rel = relative(cwd, filePath)

  const formatters: Array<{ cmd: string; label: string; exts: string[] }> = [
    { label: 'Prettier', cmd: `npx prettier --write "${rel}"`, exts: ['.ts', '.tsx', '.js', '.jsx', '.json', '.css', '.md'] },
    { label: 'Biome', cmd: `npx @biomejs/biome format --write "${rel}"`, exts: ['.ts', '.tsx', '.js', '.jsx', '.json'] },
    { label: 'Black', cmd: `python -m black "${rel}"`, exts: ['.py'] },
    { label: 'Rustfmt', cmd: `rustfmt "${rel}"`, exts: ['.rs'] },
    { label: 'gofmt', cmd: `gofmt -w "${rel}"`, exts: ['.go'] },
  ]

  for (const fmt of formatters) {
    if (!fmt.exts.includes(ext)) continue
    try {
      execSync(fmt.cmd, { cwd, timeout: 15_000, shell })
      return `Formatted with ${fmt.label}: ${rel}`
    } catch {
      continue
    }
  }

  return `No formatter available for ${ext} files. Tried: Prettier, Biome, Black, rustfmt, gofmt.`
}

// ─── Exports ──────────────────────────────────────────────────────────────────

export function createCodeToolEntries(): JarvisToolEntry[] {
  return [
    {
      definition: createDefinition(
        'CodeAnalyze',
        'Analyze a source file: count LOC, list imports/exports/functions/classes, find TODOs, measure complexity.',
        ['file_path']
      ) as JarvisToolDefinition,
      handler: codeAnalyzeHandler,
    },
    {
      definition: createDefinition(
        'RunTests',
        'Run the project test suite (auto-detects bun/npm/pytest). Returns pass/fail summary with output.',
        []
      ) as JarvisToolDefinition,
      handler: runTestsHandler,
    },
    {
      definition: createDefinition(
        'FormatCode',
        'Format a source file using the best available formatter (Prettier, Biome, Black, rustfmt, gofmt).',
        ['file_path']
      ) as JarvisToolDefinition,
      handler: formatCodeHandler,
    },
  ]
}
