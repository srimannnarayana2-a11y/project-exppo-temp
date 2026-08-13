/**
 * jarvis/tools/sheetTools.ts — Sheet Tool Handler (v2)
 *
 * Delegates to builderTools.ts BuildSheet handler.
 * Kept as a separate file for registry symmetry.
 */

import { createBuilderToolEntries } from './builderTools.js'
import type { JarvisToolEntry } from './index.js'

// Re-export the BuildSheet entry from builderTools
// (avoids duplication — builderTools already creates it)
export function createSheetToolEntries(): JarvisToolEntry[] {
  // Return empty — BuildSheet is registered by createBuilderToolEntries()
  // This file is kept for backward compatibility with imports.
  return []
}
