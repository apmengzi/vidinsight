import { useRef, useState } from 'react'
import { SlidersHorizontal, PanelLeftClose, PanelLeftOpen } from 'lucide-react'
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
    <div className="flex h-full flex-col overflow-hidden">
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
                      <button
                        onClick={() => leftPanelRef.current?.collapse()}
                        className="text-muted-foreground hover:text-primary cursor-pointer rounded p-1 hover:bg-neutral-100"
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

        {/* 左面板折叠时的展开按钮 */}
        {isLeftCollapsed && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={() => leftPanelRef.current?.expand()}
                  className="flex h-full w-8 shrink-0 items-center justify-center border-r border-neutral-200 bg-white hover:bg-neutral-50"
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
