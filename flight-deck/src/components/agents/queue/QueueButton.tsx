import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react'
import type { LucideIcon } from 'lucide-react'

// The Admin console's button looks (components/console/parts.tsx), at the
// queue's compact sizes. The console Button carries its own padding and text
// size, which would fight the sizes here, so the caller passes those.
type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'

/** The house focus ring, for every button the queue draws by hand. */
export const FOCUS = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/40'

const VARIANT: Record<Variant, string> = {
  primary: 'bg-violet-600 text-white hover:bg-violet-500 shadow-sm shadow-violet-900/20',
  secondary: 'border border-zinc-700 bg-zinc-900/60 text-zinc-200 hover:border-zinc-600 hover:bg-zinc-800',
  ghost: 'text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200',
  danger: 'border border-zinc-700 text-zinc-400 hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-600 dark:hover:text-red-400',
}

export function QBtn({ variant = 'secondary', icon: Icon, iconClassName = 'h-3.5 w-3.5', children, className = '', ref, ...rest }: {
  variant?: Variant
  icon?: LucideIcon
  iconClassName?: string
  children?: ReactNode
  ref?: Ref<HTMLButtonElement>
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      ref={ref}
      {...rest}
      // Not shrink-0: in a narrow card a long label wraps inside the button
      // instead of pushing it past the card's edge.
      className={`inline-flex items-center justify-center gap-1.5 font-medium transition-colors ${FOCUS} disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT[variant]} ${className}`}
    >
      {Icon && <Icon className={`shrink-0 ${iconClassName}`} />}
      {children}
    </button>
  )
}
