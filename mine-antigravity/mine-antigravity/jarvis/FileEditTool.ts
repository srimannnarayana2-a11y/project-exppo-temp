import { readFileSync, writeFileSync, existsSync } from 'fs'
import { resolve, relative } from 'path'

export const FileEditToolDef = {
  type: 'function' as const,
  function: {
    name: 'Edit',
    description: 'Replace an exact string in a file with a new string. Preserves indentation and formatting.',
    parameters: {
      type: 'object',
      properties: {
        file_path: { 
          type: 'string', 
          description: 'Path to the file to edit (relative or absolute)' 
        },
        old_string: { 
          type: 'string', 
          description: 'Exact text snippet to replace' 
        },
        new_string: { 
          type: 'string', 
          description: 'New replacement text' 
        },
        replace_all: { 
          type: 'boolean', 
          description: 'If true, replaces all occurrences instead of just the first match' 
        }
      },
      required: ['file_path', 'old_string', 'new_string']
    }
  }
}

export interface FileEditArgs {
  file_path: string
  old_string: string
  new_string: string
  replace_all?: boolean
}

export function executeFileEdit(args: FileEditArgs, cwd: string = process.cwd()): string {
  const { file_path, old_string, new_string, replace_all = false } = args
  const fullPath = resolve(cwd, file_path)

  if (!existsSync(fullPath)) {
    return `ERROR: File does not exist at path: ${relative(cwd, fullPath)}`
  }

  try {
    const fileContent = readFileSync(fullPath, 'utf8')

    if (old_string === new_string) {
      return `No changes made: old_string and new_string are identical.`
    }

    if (!fileContent.includes(old_string)) {
      const lines = fileContent.split('\n')
      const firstLine = old_string.split('\n')[0]?.trim() ?? ''
      const nearIndex = lines.findIndex(l => l.includes(firstLine))
      
      let hint = ''
      if (nearIndex >= 0) {
        hint = ` (Closest match found around line ${nearIndex + 1})`
      }
      return `ERROR: Target old_string not found in ${relative(cwd, fullPath)}.${hint}`
    }

    const matchesCount = fileContent.split(old_string).length - 1
    if (matchesCount > 1 && !replace_all) {
      return `ERROR: Found ${matchesCount} occurrences of old_string. Set replace_all=true or provide additional surrounding lines for uniqueness.`
    }

    const updatedContent = replace_all 
      ? fileContent.replaceAll(old_string, new_string)
      : fileContent.replace(old_string, new_string)

    writeFileSync(fullPath, updatedContent, 'utf8')
    return `Successfully updated ${relative(cwd, fullPath)} (${replace_all ? `replaced ${matchesCount} instances` : '1 replacement made'}).`
  } catch (err: unknown) {
    return `ERROR editing file: ${(err as Error).message}`
  }
}