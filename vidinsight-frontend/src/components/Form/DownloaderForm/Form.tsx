// 下载器 Cookie 设置表单（最简化版）
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { zodResolver } from '@hookform/resolvers/zod'
import {
  Form,
  FormField,
  FormItem,
  FormLabel,
  FormControl,
  FormMessage,
} from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import { getDownloaderCookie, updateDownloaderCookie } from '@/services/downloader' // 你自定义的请求
import { invoke } from '@tauri-apps/api/core'
import { useParams } from 'react-router-dom'
import { videoPlatforms } from '@/constant/note.ts'

const CookieSchema = z.object({
  cookie: z.string().min(10, '请填写有效 Cookie'),
})

const DownloaderForm = () => {
  const form = useForm({
    resolver: zodResolver(CookieSchema),
    defaultValues: { cookie: '' },
  })
  const { id } = useParams()

  const [loading, setLoading] = useState(true)

  // 应用内一键登录：弹出 VidInsight 自己的 WebView 登录窗，
  // 命中目标登录态 Cookie（HttpOnly 也读得到）后自动填入并保存。
  // douyin 无需登录：打开首页即由前端 JS 写入 s_v_web_id（yt-dlp 抖音
  // extractor 的硬性要求，2026-09-28 源码实证），等它出现即自动收窗。
  const LOGIN_TARGETS: Record<string, { site: string; wait: string }> = {
    xiaohongshu: { site: 'https://www.xiaohongshu.com/', wait: 'web_session' },
    bilibili: { site: 'https://www.bilibili.com/', wait: 'SESSDATA' },
    douyin: { site: 'https://www.douyin.com/', wait: 's_v_web_id' },
  }
  const [capturing, setCapturing] = useState(false)
  const isDesktop = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  const captureCookie = async () => {
    const target = LOGIN_TARGETS[id as string]
    if (!target) return
    // 一键登录依赖 Tauri 的 WebView 窗口（HttpOnly Cookie 只有内核才读得到），
    // 浏览器里没有 __TAURI_INTERNALS__ → invoke 是 undefined → 抛天书报错。
    // 提前拦下说人话，别让用户看着 'invoke' of undefined 发呆。
    if (!isDesktop) {
      toast.error(
        '一键登录需要桌面端（VidInsight 客户端）才能打开登录窗口。当前是浏览器访问 ——' +
          '请打开桌面上的 VidInsight 再点这个按钮；或在上方输入框手动粘贴 Cookie。',
        { duration: 9000 },
      )
      return
    }
    setCapturing(true)
    toast.loading('已打开登录窗口：登录成功后窗口会自动关闭…', { id: 'capture', duration: 300000 })
    try {
      const cookie = await invoke<string>('capture_site_cookie', {
        site: target.site,
        waitCookie: target.wait,
      })
      form.setValue('cookie', cookie, { shouldValidate: true })
      await form.handleSubmit(onSubmit)()   // 捕获成功直接保存
      toast.success('登录态已捕获并保存 ✓')
    } catch (e: any) {
      toast.error(typeof e === 'string' ? e : e?.message || '未捕获到登录态')
    } finally {
      toast.dismiss('capture')
      setCapturing(false)
    }
  }

  useEffect(() => {
    const loadCookie = async () => {
      setLoading(true) // 🔁 切换平台时显示 loading
      try {
        const res = await getDownloaderCookie(id)
        const cookie = (res as any)?.cookie || ''
        form.reset({ cookie }) // ✅ 正确重置表单值
      } catch (e) {
        toast.error('加载 Cookie 失败: ' + e)
        form.reset({ cookie: '' }) // ❗失败时也要清空旧值
      } finally {
        setLoading(false)
      }
    }

    if (id) loadCookie()
  }, [id]) // 🔁 每当 id 变化时触发

  const onSubmit = async (values: { cookie?: string }) => {
    try {
      await updateDownloaderCookie({
        platform: id,
        cookie: String(values.cookie),
      })
      toast.success('保存成功')
    } catch (e) {
      toast.error('保存失败')
    }
  }

  if (loading) return <div className="p-4">加载中...</div>

  return (
    <div className="max-w-xl p-4">
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col gap-4">
          <div className="text-lg font-bold">
            设置{videoPlatforms.find(item => item.value === id)?.label}下载器 Cookie
          </div>

          <FormField
            control={form.control}
            name="cookie"
            render={({ field }) => (
              <FormItem className="flex flex-col gap-2">
                <FormLabel>Cookie</FormLabel>
                <FormControl>
                  <Input {...field} placeholder="输入 Cookie" />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          {LOGIN_TARGETS[id as string] && (
            <Button
              type="button"
              variant="outline"
              disabled={capturing}
              onClick={captureCookie}
            >
              {capturing ? '等待登录中…' : '一键登录读取（推荐）'}
            </Button>
          )}

          <Button type="submit">保存</Button>
        </form>
      </Form>
    </div>
  )
}

export default DownloaderForm
