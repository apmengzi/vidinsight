import { useEffect, useRef } from 'react'
import { useTaskStore, type TaskStatus } from '@/store/taskStore'
import { get_task_status } from '@/services/note.ts'
import toast from 'react-hot-toast'

const TERMINAL = new Set(['SUCCESS', 'FAILED', 'FAILD', 'CANCELLED'])

export const useTaskPolling = (interval = 3000) => {
  const tasks = useTaskStore(state => state.tasks)
  const updateTaskContent = useTaskStore(state => state.updateTaskContent)

  const tasksRef = useRef(tasks)

  // 每次 tasks 更新，把最新的 tasks 同步进去
  useEffect(() => {
    tasksRef.current = tasks
  }, [tasks])

  useEffect(() => {
    const timer = setInterval(async () => {
      const pendingTasks = tasksRef.current.filter(
        task => !TERMINAL.has(String(task.status)),
      )

      // 无活跃任务时跳过轮询
      if (pendingTasks.length === 0) return

      for (const task of pendingTasks) {
        try {
          const res = await get_task_status(task.id)
          const status = String(res?.status ?? '') as TaskStatus

          // 提取 stage_progress（优先 stage_progress，兜底旧 progress 字段）
          const stageProgress = typeof res?.stage_progress === 'number'
            ? res.stage_progress
            : (typeof res?.progress === 'number' ? res.progress : null)
          const elapsed_s = typeof res?.elapsed_s === 'number' ? res.elapsed_s : undefined
          const stage_times = res?.stage_times

          if (status) {
            if (status === 'SUCCESS') {
              const { markdown, transcript, audio_meta } = res.result ?? {}
              toast.success('内容洞察生成成功')
              updateTaskContent(task.id, {
                status,
                markdown,
                transcript,
                audioMeta: audio_meta,
                stage_progress: null,
                elapsed_s,
                stage_times,
              })
            } else if (status === 'FAILED') {
              updateTaskContent(task.id, { status, stage_progress: null })
              console.warn(`⚠️ 任务 ${task.id} 失败`)
            } else {
              // 状态变更或阶段内进度推进时更新
              updateTaskContent(task.id, {
                status,
                stage_progress: stageProgress,
                elapsed_s,
                stage_times,
              })
            }
          }
        } catch (e: any) {
          // 后端把 FAILED 作为业务错误返回（code=500 + 中文原因），只有这种情况
          // 才能判定任务真的失败；网络抖动/超时（拦截器转成 code=-1）绝不能
          // 直接把任务标成 FAILED——那会让用户在断网一瞬间看到"内容分析失败"的误报。
          if (typeof e?.code === 'number' && e.code > 0) {
            updateTaskContent(task.id, { status: 'FAILED' })
            toast.error(e?.msg ? `内容洞察生成失败：${e.msg}` : '内容洞察生成失败', {
              duration: 6000,
            })
          } else {
            console.warn('任务轮询网络抖动，下一轮重试：', e?.msg || e)
          }
        }
      }
    }, interval)

    return () => clearInterval(timer)
  }, [interval, updateTaskContent])
}
