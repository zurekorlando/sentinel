/**
 * Settings page — 4 tabs: Global, Exclusion Templates, Notifications, About.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import {
  Loader2, Trash2, Plus, ExternalLink, Server,
  AlertCircle, CheckCircle2, Send,
} from 'lucide-react'

import * as api from '../lib/api'
import { formatBytes } from '../lib/utils'
import { useT, useLanguage, type Locale } from '../lib/i18n'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Textarea } from '../components/ui/textarea'
import { Slider } from '../components/ui/slider'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { Separator } from '../components/ui/separator'
import type { ExclusionTemplate, SystemInfoResponse } from '../types'


// ── Local-storage helpers ─────────────────────────────────────────────────────

function useLocalSetting<T>(key: string, defaultValue: T) {
  const stored = localStorage.getItem(key)
  const initial = stored !== null ? (JSON.parse(stored) as T) : defaultValue
  const [value, setRaw] = useState<T>(initial)

  function setValue(v: T) {
    localStorage.setItem(key, JSON.stringify(v))
    setRaw(v)
  }

  return [value, setValue] as const
}


// ── Theme toggle ──────────────────────────────────────────────────────────────

type Theme = 'light' | 'dark' | 'system'

function ThemeSelector() {
  const { t } = useT()
  const [theme, setTheme] = useLocalSetting<Theme>('sentinel-theme', 'system')

  function applyTheme(th: Theme) {
    const root = document.documentElement
    if (th === 'dark') {
      root.classList.add('dark')
    } else if (th === 'light') {
      root.classList.remove('dark')
    } else {
      // System: follow prefers-color-scheme
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
      root.classList.toggle('dark', prefersDark)
    }
    setTheme(th)
  }

  const OPTIONS: { value: Theme; label: string }[] = [
    { value: 'light',  label: t.settings.themeLight },
    { value: 'dark',   label: t.settings.themeDark },
    { value: 'system', label: t.settings.themeSystem },
  ]

  return (
    <div className="flex gap-2">
      {OPTIONS.map(o => (
        <Button
          key={o.value}
          variant={theme === o.value ? 'default' : 'outline'}
          size="sm"
          onClick={() => applyTheme(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  )
}


// ── Language selector ─────────────────────────────────────────────────────────

function LanguageSelector() {
  const { t } = useT()
  const { locale, setLocale } = useLanguage()

  const OPTIONS: { value: Locale; label: string }[] = [
    { value: 'en', label: t.settings.langEn },
    { value: 'es', label: t.settings.langEs },
  ]

  return (
    <div className="flex gap-2">
      {OPTIONS.map(o => (
        <Button
          key={o.value}
          variant={locale === o.value ? 'default' : 'outline'}
          size="sm"
          onClick={() => setLocale(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  )
}


// ── Tab 1: Global settings ────────────────────────────────────────────────────

function GlobalTab() {
  const { t, interp } = useT()
  const [defaultCompression, setDefaultCompression] = useLocalSetting('sentinel-default-compression', 3)
  const [retDaily, setRetDaily]    = useLocalSetting('sentinel-retention-daily', 7)
  const [retWeekly, setRetWeekly]  = useLocalSetting('sentinel-retention-weekly', 4)
  const [retMonthly, setRetMonthly] = useLocalSetting('sentinel-retention-monthly', 12)

  return (
    <div className="space-y-8">
      {/* Theme */}
      <div>
        <h3 className="text-base font-semibold mb-4">{t.settings.appearanceTitle}</h3>
        <div className="space-y-2">
          <Label>{t.settings.theme}</Label>
          <ThemeSelector />
          <p className="text-xs text-muted-foreground">
            {t.settings.themeHint}
          </p>
        </div>
      </div>

      <Separator />

      {/* Language */}
      <div>
        <h3 className="text-base font-semibold mb-4">{t.settings.languageTitle}</h3>
        <div className="space-y-2">
          <Label>{t.settings.language}</Label>
          <LanguageSelector />
          <p className="text-xs text-muted-foreground">
            {t.settings.languageHint}
          </p>
        </div>
      </div>

      <Separator />

      {/* Default compression */}
      <div>
        <h3 className="text-base font-semibold mb-4">{t.settings.defaultJobTitle}</h3>
        <p className="text-xs text-muted-foreground mb-4">
          {t.settings.defaultJobHint}
        </p>

        <div className="space-y-6">
          <div className="space-y-3">
            <Label>{interp(t.settings.defaultCompression, { level: defaultCompression })}</Label>
            <Slider
              min={1} max={22} step={1}
              value={[defaultCompression]}
              onValueChange={([v]) => setDefaultCompression(v)}
            />
            <p className="text-xs text-muted-foreground">
              {t.settings.compressionHint}
            </p>
          </div>

          <div>
            <Label className="block mb-3">{t.settings.defaultRetention}</Label>
            <div className="grid grid-cols-3 gap-4">
              {[
                { label: t.settings.dailySnapshots, value: retDaily, setter: setRetDaily },
                { label: t.settings.weeklySnapshots, value: retWeekly, setter: setRetWeekly },
                { label: t.settings.monthlySnapshots, value: retMonthly, setter: setRetMonthly },
              ].map(({ label, value, setter }) => (
                <div key={label} className="space-y-1.5">
                  <Label className="text-sm">{label}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={value}
                    onChange={e => setter(Number(e.target.value))}
                  />
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}


// ── Tab 2: Exclusion templates ─────────────────────────────────────────────────

const templateSchema = z.object({
  id: z.string().min(1, 'Required').regex(/^[a-z0-9_-]+$/, 'Lowercase letters, numbers, hyphens, underscores'),
  name: z.string().min(1, 'Required'),
  description: z.string(),
  patterns: z.string().min(1, 'Enter at least one pattern'),
})
type TemplateForm = z.infer<typeof templateSchema>

function ExclusionTemplatesTab() {
  const { t, interp } = useT()
  const qc = useQueryClient()
  const [showForm, setShowForm] = useState(false)

  const { data: templates = [], isLoading } = useQuery({
    queryKey: ['exclusion-templates'],
    queryFn: api.exclusions.list,
  })

  const { register, handleSubmit, reset, formState: { errors } } = useForm<TemplateForm>({
    resolver: zodResolver(templateSchema),
    defaultValues: { id: '', name: '', description: '', patterns: '' },
  })

  const createMut = useMutation({
    mutationFn: (vals: TemplateForm) => api.exclusions.create({
      id: vals.id,
      name: vals.name,
      description: vals.description,
      patterns: vals.patterns.split('\n').map(s => s.trim()).filter(Boolean),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['exclusion-templates'] })
      reset()
      setShowForm(false)
    },
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.exclusions.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['exclusion-templates'] }),
  })

  const allTemplates = templates as ExclusionTemplate[]
  const builtins = allTemplates.filter(tmpl => tmpl.builtin)
  const custom   = allTemplates.filter(tmpl => !tmpl.builtin)

  return (
    <div className="space-y-6">
      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : (
        <>
          {/* Built-in templates */}
          <div>
            <h3 className="text-base font-semibold mb-3">{t.settings.builtinTemplates}</h3>
            <div className="space-y-3">
              {builtins.map(tmpl => (
                <Card key={tmpl.id} className="bg-muted/30">
                  <CardHeader className="pb-2">
                    <div className="flex items-center gap-2">
                      <CardTitle className="text-sm">{tmpl.name}</CardTitle>
                      <Badge variant="secondary" className="text-xs">{t.settings.builtinBadge}</Badge>
                    </div>
                    <CardDescription className="text-xs">{tmpl.description}</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <div className="flex flex-wrap gap-1">
                      {tmpl.patterns.slice(0, 8).map(p => (
                        <code key={p} className="text-xs bg-muted px-1.5 py-0.5 rounded font-mono">
                          {p}
                        </code>
                      ))}
                      {tmpl.patterns.length > 8 && (
                        <span className="text-xs text-muted-foreground">
                          {interp(t.settings.morePatterns, { count: tmpl.patterns.length - 8 })}
                        </span>
                      )}
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>

          <Separator />

          {/* Custom templates */}
          <div>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-base font-semibold">{t.settings.customTemplates}</h3>
              <Button size="sm" variant="outline" onClick={() => setShowForm(v => !v)}>
                <Plus className="h-4 w-4 mr-1.5" />
                {t.settings.newTemplate}
              </Button>
            </div>

            {showForm && (
              <Card className="mb-4">
                <CardHeader>
                  <CardTitle className="text-sm">{t.settings.createTemplate}</CardTitle>
                </CardHeader>
                <CardContent>
                  <form onSubmit={handleSubmit(vals => createMut.mutate(vals))} className="space-y-4">
                    <div className="grid grid-cols-2 gap-3">
                      <div className="space-y-1.5">
                        <Label>{t.settings.templateId}</Label>
                        <Input {...register('id')} placeholder={t.settings.templateIdPlaceholder} />
                        {errors.id && <p className="text-xs text-destructive">{errors.id.message}</p>}
                      </div>
                      <div className="space-y-1.5">
                        <Label>{t.settings.templateName}</Label>
                        <Input {...register('name')} placeholder={t.settings.templateNamePlaceholder} />
                        {errors.name && <p className="text-xs text-destructive">{errors.name.message}</p>}
                      </div>
                    </div>
                    <div className="space-y-1.5">
                      <Label>{t.settings.templateDesc}</Label>
                      <Input {...register('description')} placeholder={t.settings.templateDescPlaceholder} />
                    </div>
                    <div className="space-y-1.5">
                      <Label>{t.settings.templatePatterns}</Label>
                      <Textarea
                        {...register('patterns')}
                        placeholder={'*.log\ntmp/\n__pycache__/'}
                        rows={4}
                      />
                      {errors.patterns && <p className="text-xs text-destructive">{errors.patterns.message}</p>}
                    </div>
                    {createMut.isError && (
                      <p className="text-xs text-destructive">{String(createMut.error)}</p>
                    )}
                    <div className="flex gap-2 justify-end">
                      <Button type="button" variant="outline" onClick={() => { reset(); setShowForm(false) }}>
                        {t.common.cancel}
                      </Button>
                      <Button type="submit" disabled={createMut.isPending}>
                        {createMut.isPending
                          ? <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> {t.common.saving}</>
                          : t.settings.createTemplate
                        }
                      </Button>
                    </div>
                  </form>
                </CardContent>
              </Card>
            )}

            {custom.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                {t.settings.noCustomTemplates}
              </p>
            ) : (
              <div className="space-y-3">
                {custom.map(tmpl => (
                  <Card key={tmpl.id}>
                    <CardHeader className="pb-2">
                      <div className="flex items-start justify-between">
                        <div>
                          <CardTitle className="text-sm">{tmpl.name}</CardTitle>
                          <CardDescription className="text-xs">{tmpl.description}</CardDescription>
                        </div>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="text-muted-foreground hover:text-destructive h-7 w-7"
                          onClick={() => deleteMut.mutate(tmpl.id)}
                          disabled={deleteMut.isPending}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </CardHeader>
                    <CardContent>
                      <div className="flex flex-wrap gap-1">
                        {tmpl.patterns.map(p => (
                          <code key={p} className="text-xs bg-muted px-1.5 py-0.5 rounded font-mono">
                            {p}
                          </code>
                        ))}
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}


// ── Tab 3: Notifications ──────────────────────────────────────────────────────

function NotificationsTab() {
  const { t } = useT()
  const [webhookUrl, setWebhookUrl] = useState('')
  const [testStatus, setTestStatus] = useState<'idle' | 'sending' | 'ok' | 'error'>('idle')
  const [testError, setTestError] = useState('')

  async function handleTest() {
    if (!webhookUrl) return
    setTestStatus('sending')
    setTestError('')
    try {
      const res = await fetch(webhookUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event: 'TEST',
          message: 'Sentinel webhook test',
          timestamp: new Date().toISOString(),
        }),
      })
      setTestStatus(res.ok ? 'ok' : 'error')
      if (!res.ok) setTestError(`HTTP ${res.status}`)
    } catch (e) {
      setTestStatus('error')
      setTestError(String(e))
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold mb-1">{t.settings.notifTitle}</h3>
        <p className="text-sm text-muted-foreground mb-4">
          {t.settings.notifDesc}
        </p>
      </div>

      <div className="space-y-3">
        <Label htmlFor="webhook-test">{t.settings.testWebhook}</Label>
        <div className="flex gap-2">
          <Input
            id="webhook-test"
            type="url"
            placeholder="https://hooks.example.com/sentinel"
            value={webhookUrl}
            onChange={e => { setWebhookUrl(e.target.value); setTestStatus('idle') }}
          />
          <Button
            variant="outline"
            onClick={handleTest}
            disabled={!webhookUrl || testStatus === 'sending'}
          >
            {testStatus === 'sending'
              ? <Loader2 className="h-4 w-4 animate-spin" />
              : <Send className="h-4 w-4 mr-2" />
            }
            {testStatus !== 'sending' && t.settings.testBtn}
          </Button>
        </div>

        {testStatus === 'ok' && (
          <div className="flex items-center gap-2 text-sm text-green-700 dark:text-green-400">
            <CheckCircle2 className="h-4 w-4" />
            {t.settings.webhookOk}
          </div>
        )}
        {testStatus === 'error' && (
          <div className="flex items-center gap-2 text-sm text-destructive">
            <AlertCircle className="h-4 w-4" />
            {testError || t.settings.webhookFailed}
          </div>
        )}
      </div>

      <Separator />

      <div className="rounded-md bg-muted/50 p-4 text-sm space-y-2">
        <p className="font-medium">{t.settings.payloadFormat}</p>
        <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap">
{`{
  "event": "JOB_SUCCESS" | "JOB_FAILED",
  "job_id": "my-backup",
  "run_id": "uuid",
  "status": "success" | "failed",
  "files_processed": 1234,
  "bytes_original": 102400,
  "duration_s": 42.3,
  "errors": []
}`}
        </pre>
      </div>
    </div>
  )
}


// ── Tab 4: About ──────────────────────────────────────────────────────────────

function AboutTab() {
  const { t, interp } = useT()

  const { data: info, isLoading } = useQuery<SystemInfoResponse>({
    queryKey: ['system', 'info'],
    queryFn: api.system.info,
  })

  function InfoRow({ label, value }: { label: string; value: string | number | undefined }) {
    return (
      <div className="flex items-center justify-between py-2.5 border-b last:border-0 text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium font-mono text-xs">{value ?? '—'}</span>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <div className="h-12 w-12 rounded-lg bg-primary/10 flex items-center justify-center">
          <Server className="h-6 w-6 text-primary" />
        </div>
        <div>
          <h3 className="text-xl font-bold">Sentinel</h3>
          <p className="text-sm text-muted-foreground">
            {interp(t.settings.aboutVersion, { version: info?.sentinel_version ?? '…' })}
          </p>
        </div>
      </div>

      <Separator />

      <div>
        <h3 className="text-base font-semibold mb-3">{t.settings.systemInfo}</h3>
        {isLoading ? (
          <div className="flex items-center gap-2 text-muted-foreground py-4">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span className="text-sm">{t.common.loading}</span>
          </div>
        ) : (
          <Card>
            <CardContent className="p-0 px-4">
              <InfoRow label={t.settings.hostname}        value={info?.hostname} />
              <InfoRow label={t.settings.operatingSystem} value={info?.os} />
              <InfoRow label={t.settings.cpuCount}        value={info?.cpu_count} />
              <InfoRow label={t.settings.totalDisk}       value={info ? formatBytes(info.total_disk_bytes) : undefined} />
              <InfoRow label={t.settings.freeDisk}        value={info ? formatBytes(info.free_disk_bytes) : undefined} />
              <InfoRow label={t.settings.catalogSize}     value={info ? formatBytes(info.catalog_size_bytes) : undefined} />
              <InfoRow
                label={t.settings.uptime}
                value={info ? (() => {
                  const s = Math.floor(info.uptime_seconds)
                  const h = Math.floor(s / 3600)
                  const m = Math.floor((s % 3600) / 60)
                  return interp(t.settings.uptimeFormat, { h, m })
                })() : undefined}
              />
            </CardContent>
          </Card>
        )}
      </div>

      <Separator />

      <div className="space-y-3">
        <h3 className="text-base font-semibold">{t.settings.resources}</h3>
        <div className="flex flex-wrap gap-3">
          <Button asChild variant="outline" size="sm">
            <a href="/docs" target="_blank" rel="noopener noreferrer">
              <ExternalLink className="h-4 w-4 mr-2" />
              {t.settings.apiDocs}
            </a>
          </Button>
          <Button asChild variant="outline" size="sm">
            <a href="/redoc" target="_blank" rel="noopener noreferrer">
              <ExternalLink className="h-4 w-4 mr-2" />
              {t.settings.redoc}
            </a>
          </Button>
        </div>
      </div>
    </div>
  )
}


// ── Page ──────────────────────────────────────────────────────────────────────

export default function Settings() {
  const { t } = useT()

  return (
    <div className="p-8 space-y-6 max-w-3xl">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">{t.settings.title}</h1>
        <p className="text-muted-foreground mt-1">
          {t.settings.subtitle}
        </p>
      </div>

      <Tabs defaultValue="global">
        <TabsList className="grid w-full grid-cols-4">
          <TabsTrigger value="global">{t.settings.tabGlobal}</TabsTrigger>
          <TabsTrigger value="exclusions">{t.settings.tabExclusions}</TabsTrigger>
          <TabsTrigger value="notifications">{t.settings.tabNotifications}</TabsTrigger>
          <TabsTrigger value="about">{t.settings.tabAbout}</TabsTrigger>
        </TabsList>

        <div className="mt-6">
          <TabsContent value="global">
            <GlobalTab />
          </TabsContent>
          <TabsContent value="exclusions">
            <ExclusionTemplatesTab />
          </TabsContent>
          <TabsContent value="notifications">
            <NotificationsTab />
          </TabsContent>
          <TabsContent value="about">
            <AboutTab />
          </TabsContent>
        </div>
      </Tabs>
    </div>
  )
}
