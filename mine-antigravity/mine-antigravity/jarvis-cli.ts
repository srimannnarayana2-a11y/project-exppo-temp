/**
 * jarvis-cli.ts — Tool Execution Gateway
 *
 * This CLI allows other programs (Python, Node.js, etc.) to call Jarvis tools
 * via subprocess. It's designed for IPC communication: tool invocation by name
 * with JSON args, returning JSON results.
 *
 * Usage:
 *   bun run jarvis-cli.ts <tool_name> <args_json> [cwd]
 *
 * Example:
 *   bun run jarvis-cli.ts Read '{"file_path":"package.json"}'
 *   bun run jarvis-cli.ts WebSearch '{"query":"climate change"}'
 *   bun run jarvis-cli.ts BuildDeck '{"spec":{"title":"My Deck"}}'
 *
 * Output:
 *   Exits with 0 on tool success, 1 on error. Outputs JSON result.
 *   {
 *     "success": true|false,
 *     "output": "...",
 *     "error": "...",
 *     "files_created": [...],
 *     "files_modified": [...]
 *   }
 */

import { executeJarvisToolByName } from './jarvis/index.js'

async function main() {
  // Parse CLI args: tool_name, args_json, [cwd]
  const args = process.argv.slice(2)
  
  if (args.length < 2) {
    const result = {
      success: false,
      error: 'Usage: bun run jarvis-cli.ts <tool_name> <args_json> [cwd]',
    }
    console.log(JSON.stringify(result))
    process.exit(1)
  }

  const toolName = args[0]
  const argsJson = args[1]
  const cwd = args[2] || process.cwd()

  let parsedArgs: Record<string, unknown>
  try {
    parsedArgs = JSON.parse(argsJson)
  } catch (e) {
    const result = {
      success: false,
      error: `Invalid JSON args: ${(e as Error).message}`,
    }
    console.log(JSON.stringify(result))
    process.exit(1)
  }

  try {
    // Call the tool
    const output = await executeJarvisToolByName(toolName, parsedArgs, cwd)

    // Try to parse output as JSON (tool may return structured result)
    let result
    try {
      result = JSON.parse(output)
    } catch {
      // Fallback: wrap string output
      result = {
        success: !output.startsWith('Unknown tool:') && !output.startsWith('Tool error:'),
        output,
        error: output.startsWith('Unknown tool:') || output.startsWith('Tool error:') ? output : '',
      }
    }

    console.log(JSON.stringify(result))
    process.exit(result.success ? 0 : 1)
  } catch (e) {
    const result = {
      success: false,
      error: `Tool execution failed: ${(e as Error).message}`,
    }
    console.log(JSON.stringify(result))
    process.exit(1)
  }
}

main()
