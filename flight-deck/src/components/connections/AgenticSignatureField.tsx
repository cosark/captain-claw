// The "Agentic signature" box (Connections → Google and → Microsoft): plain
// text the user's agents add to the end of every email and draft they write
// from that account. Flight Deck trims it and caps it at MAX_SIGNATURE_CHARS.

export const MAX_SIGNATURE_CHARS = 1000

// What Flight Deck stores for a typed signature (line breaks as \n, trailing
// spaces and surrounding blank lines trimmed) — so the Save button only lights
// up for a change that would actually be saved.
export function normalizeSignature(text: string): string {
  return text
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line) => line.replace(/\s+$/, ''))
    .join('\n')
    .trim()
}

interface AgenticSignatureFieldProps {
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  // What the agents write there, for the hint: "email and draft" / "draft".
  writes: string
  placeholder?: string
}

export function AgenticSignatureField({
  value,
  onChange,
  disabled,
  writes,
  placeholder = '—\nDrafted by my AI assistant',
}: AgenticSignatureFieldProps) {
  return (
    <div>
      <label className="block text-xs font-medium text-zinc-400 mb-1">
        Agentic signature <span className="text-zinc-600">(optional)</span>
      </label>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={3}
        maxLength={MAX_SIGNATURE_CHARS}
        disabled={disabled}
        placeholder={placeholder}
        className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-sm text-zinc-200 focus:border-violet-500 focus:outline-none"
      />
      <div className="mt-1 text-[11px] text-zinc-500 leading-snug">
        Added to the end of every {writes} your agents write from this account. Plain text.
        Empty = none.
      </div>
    </div>
  )
}
