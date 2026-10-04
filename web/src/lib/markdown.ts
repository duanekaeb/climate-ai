// Markdown -> sanitized HTML for reports and Claude's answers (never trust model output as HTML).
import { marked } from 'marked'
import DOMPurify from 'dompurify'

marked.setOptions({ gfm: true, breaks: false })

export function renderMarkdown(md: string | null | undefined): string {
  if (!md) return ''
  const html = marked.parse(md, { async: false }) as string
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } })
}
