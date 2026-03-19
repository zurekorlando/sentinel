import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Merge Tailwind classes safely (shadcn/ui pattern). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Format a byte count into a human-readable string. */
export function formatBytes(bytes: number, decimals = 1): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(decimals))} ${sizes[i]}`
}

/** Format a duration in seconds as "Xm Ys" or "Xs". */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s}s`
}

/** Format an ISO-8601 timestamp as a locale date/time string. */
export function formatDate(iso?: string): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

/** Return a Tailwind colour class based on a job/snapshot status string. */
export function statusColor(status: string): string {
  switch (status) {
    case 'success':   return 'text-green-600'
    case 'running':   return 'text-blue-600'
    case 'queued':    return 'text-yellow-600'
    case 'failed':    return 'text-red-600'
    case 'completed': return 'text-green-600'
    default:          return 'text-muted-foreground'
  }
}

/** Return a badge variant string based on status. */
export function statusVariant(
  status: string,
): 'default' | 'secondary' | 'destructive' | 'outline' {
  switch (status) {
    case 'success':
    case 'completed': return 'default'
    case 'failed':    return 'destructive'
    case 'running':
    case 'queued':    return 'secondary'
    default:          return 'outline'
  }
}
