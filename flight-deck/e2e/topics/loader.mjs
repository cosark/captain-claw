import { existsSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
export async function resolve(specifier, context, next) {
  if ((specifier.startsWith('.') || specifier.startsWith('/')) && context.parentURL && context.parentURL.startsWith('file:')) {
    const p = path.resolve(path.dirname(fileURLToPath(context.parentURL)), specifier)
    for (const ext of ['', '.ts', '.tsx', '/index.ts']) {
      if (existsSync(p + ext) && !(ext === '' && !/\.[cm]?[jt]sx?$/.test(p))) return next(pathToFileURL(p + ext).href, context)
    }
  }
  return next(specifier, context)
}
