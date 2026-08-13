/**
 * jarvis/WebFetchTool.ts — Standalone, efficient Web Fetch & Scraping Tool
 */

export const WebFetchToolDef = {
  type: 'function' as const,
  function: {
    name: 'WebFetch',
    description: 'Fetch and extract clean Markdown/text content from any public web page or URL.',
    parameters: {
      type: 'object',
      properties: {
        url: {
          type: 'string',
          description: 'The HTTP/HTTPS URL to fetch and scrape content from.'
        },
        max_chars: {
          type: 'number',
          description: 'Maximum characters of text content to return (default: 40000).'
        }
      },
      required: ['url']
    }
  }
}

export interface WebFetchArgs {
  url: string
  max_chars?: number
}

/**
 * Strips scripts, styles, and HTML tags, turning raw HTML into clean Markdown/text.
 */
function htmlToCleanText(html: string): string {
  let text = html
    // Remove unwanted non-content blocks
    .replace(/<(script|style|svg|nav|footer|header|noscript|iframe|form)[^>]*>[\s\S]*?<\/\1>/gi, '')
    
    // Convert headers
    .replace(/<h1[^>]*>(.*?)<\/h1>/gi, '\n# $1\n')
    .replace(/<h2[^>]*>(.*?)<\/h2>/gi, '\n## $1\n')
    .replace(/<h3[^>]*>(.*?)<\/h3>/gi, '\n### $1\n')
    .replace(/<h[4-6][^>]*>(.*?)<\/h[4-6]>/gi, '\n#### $1\n')
    
    // Convert paragraphs, linebreaks, and code blocks
    .replace(/<code[^>]*>(.*?)<\/code>/gi, '`$1`')
    .replace(/<pre[^>]*>(.*?)<\/pre>/gi, '\n```\n$1\n```\n')
    .replace(/<p[^>]*>(.*?)<\/p>/gi, '\n$1\n')
    .replace(/<br\s*\/?>/gi, '\n')
    
    // Convert links & lists
    .replace(/<a\s+[^>]*href=["']([^"']+)["'][^>]*>(.*?)<\/a>/gi, '[$2]($1)')
    .replace(/<li[^>]*>(.*?)<\/li>/gi, '\n* $1')
    
    // Strip all remaining HTML tags
    .replace(/<[^>]+>/g, '')
    
    // Decode common HTML entities
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    
    // Normalize excessive newlines and spaces
    .replace(/[ \t]+/g, ' ')
    .replace(/\n\s*\n\s*\n+/g, '\n\n')
    .trim()

  return text
}

export async function executeWebFetch(args: WebFetchArgs): Promise<string> {
  const { url, max_chars = 40_000 } = args
  const startTime = Date.now()

  try {
    const parsedUrl = new URL(url)
    if (!['http:', 'https:'].includes(parsedUrl.protocol)) {
      return `ERROR: Invalid protocol "${parsedUrl.protocol}". Only http and https URLs are supported.`
    }

    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
      },
      redirect: 'follow'
    })

    const duration = Date.now() - startTime
    const statusCode = response.status
    const statusText = response.statusText

    if (!response.ok) {
      return `HTTP ${statusCode} ${statusText} when fetching ${url}`
    }

    const contentType = response.headers.get('content-type') || ''
    const rawBody = await response.text()

    let processedText = ''

    if (contentType.includes('application/json')) {
      // Format JSON output nicely
      try {
        processedText = JSON.stringify(JSON.parse(rawBody), null, 2)
      } catch {
        processedText = rawBody
      }
    } else if (contentType.includes('text/html')) {
      // Process HTML content into clean text
      processedText = htmlToCleanText(rawBody)
    } else {
      // Raw plain text or markdown
      processedText = rawBody
    }

    if (processedText.length === 0) {
      return `Fetched ${url} successfully (HTTP ${statusCode}), but no readable text content was found.`
    }

    let truncatedNote = ''
    if (processedText.length > max_chars) {
      processedText = processedText.slice(0, max_chars)
      truncatedNote = `\n\n[Content truncated at ${max_chars.toLocaleString()} characters]`
    }

    return `[Fetched ${url} | HTTP ${statusCode} | ${duration}ms]\n\n${processedText}${truncatedNote}`

  } catch (err: unknown) {
    return `ERROR fetching ${url}: ${(err as Error).message}`
  }
}