/* -------------------- 常量 -------------------- */
import {
  BiliBiliLogo,
  DouyinLogo,
  KuaishouLogo,
  LocalLogo,
  XhsLogo,
  YoutubeLogo,
} from '@/components/Icons/platform.tsx'

export const noteFormats = [
  { label: '目录', value: 'toc' },
  { label: '内容定位', value: 'link' },
  { label: '关键画面', value: 'screenshot' },
  { label: '洞察总结', value: 'summary' },
] as const

export const noteStyles = [
  { label: '自适应结构', value: 'adaptive' },
  { label: '知识萃取', value: 'knowledge' },
  { label: '竞品营销', value: 'marketing' },
  { label: '竞品速览', value: 'minimal' },
  { label: '深度洞察', value: 'detailed' },
  { label: '脚本拆解', value: 'tutorial' },
  { label: '市场研究', value: 'academic' },
  { label: '小红书种草分析', value: 'xiaohongshu' },
  { label: '用户情绪洞察', value: 'life_journal' },
  { label: '选题清单', value: 'task_oriented' },
  { label: '运营周报', value: 'business' },
  { label: '会议复盘', value: 'meeting_minutes' },
] as const

export const videoPlatforms = [
  { label: '哔哩哔哩', value: 'bilibili', logo: BiliBiliLogo },
  { label: 'YouTube', value: 'youtube', logo: YoutubeLogo },
  { label: '抖音', value: 'douyin', logo: DouyinLogo },
  { label: '快手', value: 'kuaishou', logo: KuaishouLogo },
  { label: '小红书', value: 'xiaohongshu', logo: XhsLogo },
  { label: '本地视频', value: 'local', logo: LocalLogo },
] as const
