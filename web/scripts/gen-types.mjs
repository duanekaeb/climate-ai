// Generates src/api/types.ts from the backend's JSON Schema (api/scripts/export_schema.py), and
// src/components/model/policyParams.schema.json: just $defs.PolicyParams, which policyMeta.ts
// imports so the public bundle does not carry the whole schema (api/tests/test_web_policy_schema.py
// fails if the copy goes stale).
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

// Keys sorted at every level so a regeneration only shows real changes in the diff.
const sortKeys = (v) =>
  Array.isArray(v)
    ? v.map(sortKeys)
    : v && typeof v === 'object'
      ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])]))
      : v
const policyParams = JSON.stringify(sortKeys(schema.$defs.PolicyParams), null, 1) + '\n'
writeFileSync(new URL('../src/components/model/policyParams.schema.json', import.meta.url), policyParams)
console.log('wrote src/components/model/policyParams.schema.json')
