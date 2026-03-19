/**
 * Minimal toast notification system.
 *
 * Usage:
 *   import { useToast } from '../../components/ui/toast'
 *   const { toast } = useToast()
 *   toast({ title: 'Backup complete', variant: 'success' })
 *
 * Mount <Toaster /> once in main.tsx or AppLayout.tsx.
 */
import React, { createContext, useCallback, useContext, useState } from 'react'
import { cn } from '../../lib/utils'
import { X, CheckCircle, XCircle, Info } from 'lucide-react'

export type ToastVariant = 'default' | 'success' | 'error' | 'info'

export interface ToastMessage {
  id: string
  title: string
  description?: string
  variant?: ToastVariant
}

interface ToastContextValue {
  toasts: ToastMessage[]
  toast: (msg: Omit<ToastMessage, 'id'>) => void
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastMessage[]>([])

  const toast = useCallback((msg: Omit<ToastMessage, 'id'>) => {
    const id = Math.random().toString(36).slice(2)
    setToasts((prev) => [...prev, { id, ...msg }])
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 5000)
  }, [])

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  return (
    <ToastContext.Provider value={{ toasts, toast, dismiss }}>
      {children}
    </ToastContext.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within ToastProvider')
  return ctx
}

const variantIcon: Record<ToastVariant, React.ReactNode> = {
  default: <Info className="h-4 w-4" />,
  success: <CheckCircle className="h-4 w-4 text-green-500" />,
  error: <XCircle className="h-4 w-4 text-red-500" />,
  info: <Info className="h-4 w-4 text-blue-500" />,
}

export function Toaster() {
  const ctx = useContext(ToastContext)
  if (!ctx) return null
  const { toasts, dismiss } = ctx

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            'flex min-w-[280px] items-start gap-3 rounded-md border bg-card px-4 py-3 shadow-lg',
            t.variant === 'error' && 'border-red-300',
            t.variant === 'success' && 'border-green-300',
          )}
        >
          <span className="mt-0.5">{variantIcon[t.variant ?? 'default']}</span>
          <div className="flex-1">
            <p className="text-sm font-medium">{t.title}</p>
            {t.description && (
              <p className="text-xs text-muted-foreground">{t.description}</p>
            )}
          </div>
          <button onClick={() => dismiss(t.id)} className="text-muted-foreground hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>
      ))}
    </div>
  )
}
