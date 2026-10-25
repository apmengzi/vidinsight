import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'
import { Link, Outlet } from 'react-router-dom'
import { SlidersHorizontal } from 'lucide-react'
import React from 'react'

interface ISettingLayoutProps {
  Menu: React.ReactNode
}
const SettingLayout = ({ Menu }: ISettingLayoutProps) => {
  return (
    <div
      className="flex h-full w-full flex-col"
      style={{
        backgroundColor: 'var(--vi-bg)',
      }}
    >
      <div className="flex flex-1 overflow-hidden">
        {/* 左侧部分：菜单（不再重复放 logo/产品名——主导航已经有一份了） */}
        <aside className="flex w-[260px] shrink-0 flex-col border-r border-[var(--vi-hairline)] bg-[var(--vi-surface)] lg:w-[300px]">
          <header className="flex h-14 shrink-0 items-center justify-between px-4 lg:h-16 lg:px-6">
            <div className="text-base font-semibold text-[var(--vi-ink)] lg:text-lg">设置</div>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Link to={'/chat'}>
                    <SlidersHorizontal className="h-4 w-4 cursor-pointer text-[var(--vi-ink-3)] hover:text-[var(--vi-ink)] lg:h-5 lg:w-5" />
                  </Link>
                </TooltipTrigger>
                <TooltipContent>
                  <span>返回首页</span>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </header>

          {/* 菜单内容 */}
          <div className="flex-1 overflow-auto p-3 lg:p-4">
            {Menu}
          </div>
        </aside>

        {/* 右侧内容区域 */}
        <main className="flex-1 overflow-auto bg-[var(--vi-bg)]">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
export default SettingLayout
