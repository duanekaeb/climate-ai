// Generates src/api/types.ts from the backend's JSON Schema (api/scripts/export_schema.py).
// Usage: python api/scripts/export_schema.py > web/src/api/schema.json && npm run gen:types
import { readFileSync, writeFileSync } from 'node:fs'
import { compile } from 'json-schema-to-typescript'

const schema = JSON.parse(readFileSync(new URL('../src/api/schema.json', import.meta.url), 'utf8'))
const ts = await compile(schema, 'ApiTypes', {
  bannerComment:
    '/* Generated from api/climate/api/schemas.py by web/scripts/gen-types.mjs. Do not edit by hand. */',
  additionalProperties: false,
  unreachableDefinitions: true,
  declareExternallyReferenced: true,
  strictIndexSignatures: false,
  format: true,
  style: { singleQuote: true, semi: false, printWidth: 110 },
})
writeFileSync(new URL('../src/api/types.ts', import.meta.url), ts)
console.log('wrote src/api/types.ts')
