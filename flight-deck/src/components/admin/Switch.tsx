// The on/off switch of the Admin page's settings cards.

export function Switch({ on, onToggle, disabled = false, label }: {
  on: boolean
  onToggle: () => void
  disabled?: boolean
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onToggle}
      disabled={disabled}
      className={`relative inline-flex h-6 w-11 shrink-0 rounded-full border-2 border-transparent transition-colors disabled:opacity-50 ${
        on ? 'bg-violet-600' : 'bg-zinc-700'
      }`}
    >
      <span className={`inline-block h-5 w-5 rounded-full bg-white transition-transform ${
        on ? 'translate-x-5' : 'translate-x-0'
      }`} />
    </button>
  )
}
