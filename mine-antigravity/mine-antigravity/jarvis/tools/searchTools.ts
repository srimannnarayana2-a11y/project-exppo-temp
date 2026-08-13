/**
 * jarvis/tools/searchTools.ts — Search Tools with Web Cache
 *
 * Upgrades over v1:
 *  - WebFetch results cached for 5 minutes
 *  - WebSearch uses DuckDuckGo with better result extraction + links
 *  - Grep/Glob use platform-native commands with proper Windows fallback
 *  - All search tools return structured, readable output
 */

import { execSync } from 'child_process'
import { basename, resolve } from 'path'
import { getCachedWeb, setCachedWeb } from '../cache.js'
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
          pattern: { type: 'string', description: 'Regex or literal pattern to search' },
          path: { type: 'string', description: 'Path to search (file or directory)' },
          include: { type: 'string', description: 'File glob filter (e.g. "*.ts")' },
          case_sensitive: { type: 'boolean', description: 'Case-sensitive search (default: false)' },
          query: { type: 'string', description: 'Search query' },
          url: { type: 'string', description: 'URL to fetch' },
          max_chars: { type: 'number', description: 'Maximum characters to return (default: 40000)' },
          top_k: { type: 'number', description: 'Number of results' },
        },
        required,
      },
    },
  }
}

// ─── Grep (ripgrep with Windows fallback) ─────────────────────────────────────

function grepHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const pattern = args.pattern as string
  const target = resolve(cwd, args.path as string)
  const include = args.include ? (process.platform === 'win32' ? `-g "${args.include}"` : `--include "${args.include}"`) : ''
  const caseFlag = args.case_sensitive ? '' : '-i'
  const shell = process.platform === 'win32' ? 'powershell.exe' : '/bin/bash'

  // Try ripgrep first (fastest), then fall back to platform grep
  const cmds = process.platform === 'win32'
    ? [`rg -n ${caseFlag} ${include} "${pattern}" "${target}"`]
    : [
        `rg -n ${caseFlag} ${include} "${pattern}" "${target}"`,
        `grep -rn ${caseFlag} "${pattern}" "${target}" ${args.include ? `--include="${args.include}"` : ''}`,
      ]

  for (const cmd of cmds) {
    try {
      const out = execSync(cmd, { cwd, maxBuffer: 5 << 20, shell })
      const lines = out.toString().trim()
      if (!lines) return '(no matches)'
      const count = lines.split('\n').length
      return `${count} match${count === 1 ? '' : 'es'} for "${pattern}":\n\n${lines}`
    } catch {
      continue
    }
  }
  return `(no matches for "${pattern}")`
}

// ─── Glob (find files matching a pattern) ─────────────────────────────────────

function globHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const pattern = args.pattern as string
  const shell = process.platform === 'win32' ? 'powershell.exe' : '/bin/bash'
  const name = basename(pattern)  // Always use the final filename component

  try {
    let out: string
    if (process.platform === 'win32') {
      // Windows: Get-ChildItem recursive search
      out = execSync(
        `Get-ChildItem -Recurse -Path "." -Filter "${name}" | ForEach-Object { $_.FullName.Replace((Get-Location).Path + '\\', '').Replace('\\', '/') }`,
        { cwd, shell, maxBuffer: 5 << 20 }
      ).toString().trim()
    } else {
      // Unix: always search from cwd recursively — handles **, *.ts, plain filenames
      // IMPORTANT: never use the path prefix for glob patterns like **/foo or *.ts
      // because `find "**"` tries to find a literal dir named "**"
      out = execSync(
        `find . -name "${name}" 2>/dev/null | sort`,
        { cwd, shell, maxBuffer: 5 << 20 }
      ).toString().trim()
    }

    if (!out) return '(no files found)'
    const files = out.split('\n').filter(Boolean)
    return `${files.length} file${files.length === 1 ? '' : 's'} matching "${pattern}":\n\n${files.join('\n')}`
  } catch {
    return '(no files found)'
  }
}

// ─── WebFetch (cached for 5 minutes) ─────────────────────────────────────────

async function webFetchHandler(args: Record<string, unknown>): Promise<string> {
  const url = args.url as string
  const maxChars = (args.max_chars as number) ?? 40_000

  // Cache hit
  const cached = getCachedWeb(url)
  if (cached) {
    const trimmed = cached.length > maxChars ? cached.slice(0, maxChars) + '\n\n… [content truncated]' : cached
    return `[Cache] ${trimmed}`
  }

  try {
    const response = await fetch(url, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (compatible; Jarvis/2.0; +https://nvidia.com)',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5',
      },
      redirect: 'follow',
      signal: AbortSignal.timeout(15_000),
    })

    if (!response.ok) return `HTTP ${response.status} ${response.statusText} — ${url}`

    const contentType = response.headers.get('content-type') ?? ''
    const text = await response.text()

    // Basic HTML to text stripping for readability
    let content = text
    if (contentType.includes('html')) {
      content = text
        .replace(/<script[\s\S]*?<\/script>/gi, '')
        .replace(/<style[\s\S]*?<\/style>/gi, '')
        .replace(/<[^>]+>/g, ' ')
        .replace(/&nbsp;/g, ' ')
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/\s{3,}/g, '\n\n')
        .trim()
    }

    setCachedWeb(url, content)

    const trimmed = content.length > maxChars ? content.slice(0, maxChars) + '\n\n… [content truncated]' : content
    return `[Fetched ${url}]\n\n${trimmed}`
  } catch (e: unknown) {
    return `ERROR fetching ${url}: ${(e as Error).message}`
  }
}

// ─── WebSearch (DuckDuckGo with structured results) ───────────────────────────

async function webSearchHandler(args: Record<string, unknown>): Promise<string> {
  const query = args.query as string
  const topK = (args.top_k as number) ?? 8

  try {
    const response = await fetch(
      `https://lite.duckduckgo.com/lite/?q=${encodeURIComponent(query)}&kl=us-en`,
      {
        headers: {
          'User-Agent': 'Mozilla/5.0 (compatible; Jarvis/2.0)',
          'Accept': 'text/html',
        },
        signal: AbortSignal.timeout(10_000),
      }
    )

    if (!response.ok) return `Search unavailable (HTTP ${response.status})`

    const html = await response.text()

    // Extract result links and snippets
    const linkMatches = [...html.matchAll(/<a[^>]+href="([^"]*)"[^>]*>([^<]+)<\/a>/gi)]
    const snippetMatches = [...html.matchAll(/<td class="result-snippet"[^>]*>([\s\S]*?)<\/td>/gi)]

    const results: string[] = []
    let linkIdx = 0

    for (const [, href, text] of linkMatches) {
      if (results.length >= topK) break
      // Filter out DDG internal links
      if (!href.startsWith('http') || href.includes('duckduckgo.com')) continue
      const cleanText = text.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim()
      if (!cleanText || cleanText.length < 5) continue

      const snippet = snippetMatches[linkIdx]?.[1]
        ?.replace(/<[^>]+>/g, '')
        .replace(/\s+/g, ' ')
        .trim() ?? ''

      results.push(`${results.length + 1}. ${cleanText}\n   ${href}${snippet ? `\n   ${snippet.slice(0, 200)}` : ''}`)
      linkIdx++
    }

    if (results.length === 0) {
      return `No results found for: "${query}"\n\nTry WebFetch with a specific documentation URL instead.`
    }

    return `Search results for: "${query}"\n\n${results.join('\n\n')}`
  } catch (e: unknown) {
    return `ERROR performing web search: ${(e as Error).message}`
  }
}

// ─── Exports ──────────────────────────────────────────────────────────────────

export function createSearchToolEntries(): JarvisToolEntry[] {
  return [
    {
      definition: createDefinition('Grep', 'Search files using ripgrep (falls back to grep). Returns match count and all matching lines.', ['pattern', 'path']) as JarvisToolDefinition,
      handler: grepHandler,
    },
    {
      definition: createDefinition('Glob', 'Find files matching a glob pattern. Works on Windows and Unix.', ['pattern']) as JarvisToolDefinition,
      handler: globHandler,
    },
    {
      definition: createDefinition('WebFetch', 'Fetch and extract text content from a URL. Results cached for 5 minutes. Strips HTML tags for readability.', ['url']) as JarvisToolDefinition,
      handler: webFetchHandler,
    },
    {
      definition: createDefinition('WebSearch', 'Search the web using DuckDuckGo. Returns structured results with titles, URLs, and snippets.', ['query']) as JarvisToolDefinition,
      handler: webSearchHandler,
    },
  ]
}
