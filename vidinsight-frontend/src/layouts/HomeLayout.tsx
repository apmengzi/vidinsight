import { useRef, useState } from 'react'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'

import { Link } from 'react-router-dom'
import { ResizablePanel, ResizablePanelGroup, ResizableHandle } from '@/components/ui/resizable'
import { ScrollArea } from "@/components/ui/scroll-area.tsx"
import type { ImperativePanelHandle } from 'react-resizable-panels'
import logo from '@/assets/vidinsight-logo.png'

interface IProps {
  NoteForm: React.ReactNode
  Preview: React.ReactNode
}

const HomeLayout: React.FC<IProps> = ({ NoteForm, Preview }) => {
  const [isLeftCollapsed, setIsLeftCollapsed] = useState(false)
  const leftPanelRef = useRef<ImperativePanelHandle>(null)

  return (
    <div className="relative flex h-full flex-col overflow-hidden">
      <ResizablePanelGroup direction="horizontal" className="h-full w-full">
        {/* 左边表单 */}
        <ResizablePanel
          ref={leftPanelRef}
          defaultSize={25}
          minSize={15}
          maxSize={45}
          collapsible
          collapsedSize={0}
          onCollapse={() => setIsLeftCollapsed(true)}
          onExpand={() => setIsLeftCollapsed(false)}
        >
          <aside className="flex h-full flex-col overflow-hidden border-r border-neutral-200 bg-white">
            <header className="flex h-14 shrink-0 items-center justify-between px-4">
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center overflow-hidden rounded-xl">
                  <img src={logo} alt="logo" className="h-full w-full object-contain" />
                </div>
                <div>
                  <div className="text-lg font-bold leading-tight text-gray-800">VidInsight</div>
                  <div className="text-xs text-gray-500">灵悉 · 内容洞察工作台</div>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      {/* P1-2：全局统一圆形图标按钮（h-8 w-8 rounded-full，
                          与 AppLayout 侧栏按钮同形态），不允许长条形 */}
                      <button
                        onClick={() => leftPanelRef.current?.collapse()}
                        className="flex h-8 w-8 cursor-pointer items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-neutral-100 hover:text-primary"
                        aria-label="收起工作区"
                      >
                        <PanelLeftClose className="h-4 w-4" />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>
                      <span>收起工作区</span>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </div>
            </header>
            <ScrollArea className="flex-1 overflow-auto">
              <div className="p-4">{NoteForm}</div>
            </ScrollArea>
          </aside>
        </ResizablePanel>

        <ResizableHandle />

        {/* 左面板折叠后的展开按钮
            P1-2 修复：旧版是 w-8 h-full 的全高长条按钮（占满左缘一整条），
            用户看到「两个大长条」不知道点哪个；且长条不可识别为按钮。
            改为圆形悬浮按钮（h-8 w-8 rounded-full），贴左缘 top 位，
            与 AppLayout 侧栏展开按钮形态完全一致——全局只有一个按钮形状。 */}
        {isLeftCollapsed && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={() => leftPanelRef.current?.expand()}
                  className="absolute left-2 top-3 z-20 flex h-8 w-8 items-center justify-center rounded-full border border-neutral-200 bg-white shadow-sm transition-colors hover:bg-neutral-50"
                  aria-label="展开工作区"
                >
                  <PanelLeftOpen className="h-4 w-4 text-muted-foreground" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="right">
                <span>展开工作区</span>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}

        {/* 右边预览 */}
        <ResizablePanel defaultSize={75} minSize={30}>
          <main className="flex h-full flex-col overflow-hidden bg-white">{Preview}</main>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  )
}

export default HomeLayout
