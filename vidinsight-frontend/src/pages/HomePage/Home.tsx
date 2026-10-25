import { FC, lazy, Suspense, useEffect, useState } from 'react'
import HomeLayout from '@/layouts/HomeLayout.tsx'
import NoteForm from '@/pages/HomePage/components/NoteForm.tsx'
import { useTaskStore } from '@/store/taskStore'
type ViewStatus = 'idle' | 'loading' | 'success' | 'failed'

const MarkdownViewer = lazy(() => import('@/pages/HomePage/components/MarkdownViewer.tsx'))
export const HomePage: FC = () => {
  const tasks = useTaskStore(state => state.tasks)
  const currentTaskId = useTaskStore(state => state.currentTaskId)

  const currentTask = tasks.find(t => t.id === currentTaskId)

  const [status, setStatus] = useState<ViewStatus>('idle')

  useEffect(() => {
    if (!currentTask) {
      setStatus('idle')
    } else if (currentTask.status === 'SUCCESS') {
      setStatus('success')
    } else if (currentTask.status === 'FAILED') {
      setStatus('failed')
    } else {
      setStatus('loading')
    }
  }, [currentTask, currentTask?.status])

  return (
    <HomeLayout
      NoteForm={<NoteForm />}
      Preview={
        <Suspense fallback={<div className="flex h-full items-center justify-center text-sm text-neutral-400">加载中...</div>}>
          <MarkdownViewer status={status} />
        </Suspense>
      }
    />
  )
}
