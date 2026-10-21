import { MessagesSquare, ShieldCheck } from 'lucide-react'
import { openConsole, leaveConsole } from '../../stores/consoleStore'

// The desktop app's two faces for an admin: the Admin console, or the
// chat-first simple layout everyone else works in. Shown only in the desktop
// app (isDesktopApp) — a browser has its own ways between them.

export function ShellModeSwitch({ current }: { current: 'admin' | 'user' }) {
  const option = (mode: 'admin' | 'user', label: string, hint: string, Icon: typeof ShieldCheck, go: () => void) => {
    const on = current === mode
    return (
      <button
        type="button"
        role="radio"
        aria-checked={on}
        title={hint}
        onClick={() => { if (!on) go() }}
        className={`flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
          on ? 'bg-violet-600 text-white shadow-sm' : 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200'
        }`}
      >
        <Icon className="h-4 w-4" />
        {label}
      </button>
    )
  }
  return (
    <div role="radiogroup" aria-label="View" className="grid grid-cols-2 gap-1 rounded-xl border border-zinc-800 bg-zinc-950/60 p-1">
      {option('admin', 'Admin', 'Admin console — models, archetypes, connections, users, settings', ShieldCheck, () => openConsole())}
      {option('user', 'User', 'The agents and their chats, as your users see them', MessagesSquare, () => leaveConsole(undefined, 'simple'))}
    </div>
  )
}
