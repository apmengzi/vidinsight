import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import { delete_task, generateNote } from '@/services/note.ts'
import type { ServerTaskItem } from '@/services/note.ts'
import { v4 as uuidv4 } from 'uuid'
import toast from 'react-hot-toast'
import { get, set, del } from 'idb-keyval'


// 与后端 app/enmus/task_status_enums.py 的 TaskStatus 对齐。
// 旧值 'FAILD' 是拼写错误，导致 'FAILED' 被 TS 判为不可达分支（Home.tsx 的
// 失败态判断因此拿不到类型保护）。保留 'FAILD' 只为兼容已持久化到 IndexedDB 的旧任务。
export type TaskStatus =
  | 'PENDING'
  | 'PARSING'
  | 'DOWNLOADING'
  | 'TRANSCRIBING'
  | 'SUMMARIZING'
  | 'FORMATTING'
  | 'SAVING'
  | 'RUNNING'
  | 'SUCCESS'
  | 'FAILED'
  | 'CANCELLED'
  | 'FAILD'

export interface AudioMeta {
  cover_url: string
  duration: number
  file_path: string
  platform: string
  raw_info: any
  title: string
  video_id: string
}

export interface Segment {
  start: number
  end: number
  text: string
}

export interface Transcript {
  full_text: string
  language: string
  raw: any
  segments: Segment[]
}
export interface Markdown {
  ver_id: string
  content: string
  style: string
  model_name: string
  created_at: string
}

export interface Task {
  id: string
  markdown: string|Markdown [] //为了兼容之前的笔记
  transcript: Transcript
  status: TaskStatus
  /** 阶段内进度（0-100 或 null） */
  stage_progress?: number | null
  /** 任务已用时长（秒） */
  elapsed_s?: number
  /** 各阶段耗时明细 */
  stage_times?: Record<string, number>
  audioMeta: AudioMeta
  createdAt: string
  formData: {
    video_url: string
    link: undefined | boolean
    screenshot: undefined | boolean
    platform: string
    quality: string
    model_name: string
    provider_id: string
  }
}

interface TaskStore {
  tasks: Task[]
  currentTaskId: string | null
  addPendingTask: (taskId: string, platform: string, formData?: any) => void
  updateTaskContent: (id: string, data: Partial<Omit<Task, 'id' | 'createdAt'>>) => void
  removeTask: (id: string) => void
  clearTasks: () => void
  setCurrentTask: (taskId: string | null) => void
  getCurrentTask: () => Task | null
  retryTask: (id: string, payload?: any) => void
  /** 从后端 /tasks/list 补齐本地没有的任务（本地记录优先，不覆盖） */
  hydrateFromServer: (serverTasks: ServerTaskItem[]) => number
}

export const useTaskStore = create<TaskStore>()(
  persist(
    (set, get) => ({
      tasks: [],
      currentTaskId: null,

      addPendingTask: (taskId: string, platform: string, formData: any) =>

        set(state => ({
          tasks: [
            {
              formData: formData,
              id: taskId,
              status: 'PENDING',
              markdown: '',
              platform: platform,
              transcript: {
                full_text: '',
                language: '',
                raw: null,
                segments: [],
              },
              createdAt: new Date().toISOString(),
              audioMeta: {
                cover_url: '',
                duration: 0,
                file_path: '',
                platform: '',
                raw_info: null,
                title: '',
                video_id: '',
              },
            },
            ...state.tasks,
          ],
          currentTaskId: taskId, // 默认设置为当前任务
        })),

      updateTaskContent: (id, data) =>
          set(state => ({
            tasks: state.tasks.map(task => {
              if (task.id !== id) return task

              if (task.status === 'SUCCESS' && data.status === 'SUCCESS') return task

              // 如果是 markdown 字符串，封装为版本
              if (typeof data.markdown === 'string') {
                const prev = task.markdown
                const newVersion: Markdown = {
                  ver_id: `${task.id}-${uuidv4()}`,
                  content: data.markdown,
                  style: task.formData.style || '',
                  model_name: task.formData.model_name || '',
                  created_at: new Date().toISOString(),
                }

                let updatedMarkdown: Markdown[]
                if (Array.isArray(prev)) {
                  updatedMarkdown = [newVersion, ...prev]
                } else {
                  updatedMarkdown = [
                    newVersion,
                    ...(typeof prev === 'string' && prev
                        ? [{
                          ver_id: `${task.id}-${uuidv4()}`,
                          content: prev,
                          style: task.formData.style || '',
                          model_name: task.formData.model_name || '',
                          created_at: new Date().toISOString(),
                        }]
                        : []),
                  ]
                }

                return {
                  ...task,
                  ...data,
                  markdown: updatedMarkdown,
                }
              }

              return { ...task, ...data }
            }),
          })),


      getCurrentTask: () => {
        const currentTaskId = get().currentTaskId
        return get().tasks.find(task => task.id === currentTaskId) || null
      },
      retryTask: async (id: string, payload?: any) => {

        if (!id){
          toast.error('任务不存在')
          return
        }
        const task = get().tasks.find(task => task.id === id)
        console.log('retry',task)
        if (!task) return

        const newFormData = payload || task.formData
        try {
          await generateNote({
            ...newFormData,
            task_id: id,
          })
        } catch (e: any) {
          // 就绪门禁：转写模型未下载好。不要把任务标成 PENDING（会一直转），
          // 给提示让用户先去下载。
          if (e?.data?.reason === 'transcriber_model_not_ready') {
            toast.error(
              e?.data?.downloading
                ? '转写模型正在下载中，请稍候再重试'
                : '转写模型尚未下载，请先去「设置 → 音频转写配置」页下载',
            )
            return
          }
          console.error('重试任务失败：', e)
          return
        }

        set(state => ({
          tasks: state.tasks.map(t =>
              t.id === id
                  ? {
                    ...t,
                    formData: newFormData, // ✅ 显式更新 formData
                    status: 'PENDING',
                  }
                  : t
          ),
        }))
      },


      removeTask: async id => {
        const task = get().tasks.find(t => t.id === id)

        // 更新 Zustand 状态
        set(state => ({
          tasks: state.tasks.filter(task => task.id !== id),
          currentTaskId: state.currentTaskId === id ? null : state.currentTaskId,
        }))

        // 调用后端删除接口（如果找到了任务）
        if (task) {
          // 后端 /delete_task 要求 platform 必填（Pydantic 422），而 Task 上的
          // 平台只存在 formData/audioMeta 里——此前直接读 task.platform 恒为
          // undefined，导致删除请求每次都被后端拒绝。
          const platform = task.formData?.platform || task.audioMeta?.platform || ''
          if (!platform) {
            toast.error('本地记录已移除，但缺少平台信息，后端记录未能删除')
            return
          }
          try {
            await delete_task({
              video_id: task.audioMeta.video_id,
              platform,
            })
          } catch {
            // delete_task 内部已 toast 具体原因；这里吞掉避免调用方
            // （NoteHistory 的 onClick）产生未处理的 Promise rejection。
            // 本地记录已从列表移除，后端失败时用户可重新刷新历史。
          }
        }
      },

      clearTasks: () => set({ tasks: [], currentTaskId: null }),

      setCurrentTask: taskId => set({ currentTaskId: taskId }),

      // 后端产物补齐：IndexedDB 只有本浏览器提交过的任务，换浏览器/清缓存后
      // 历史会凭空消失。以 task_id 去重合并，本地已有的一律不动（本地可能带
      // 更新的轮询状态），只追加本地缺的。返回追加条数供调用方决定是否提示。
      hydrateFromServer: serverTasks => {
        if (!Array.isArray(serverTasks) || serverTasks.length === 0) return 0
        const local = new Set(get().tasks.map(t => t.id))
        const missing = serverTasks
          .filter(st => st?.task_id && !local.has(st.task_id))
          .map(st => {
            const task: Task = {
              id: st.task_id,
              markdown: st.markdown || '',
              transcript: { full_text: '', language: '', raw: null, segments: [] },
              status: (st.status as TaskStatus) || 'PENDING',
              audioMeta: {
                cover_url: st.cover_url || '',
                duration: st.duration || 0,
                file_path: '',
                platform: st.platform || '',
                raw_info: null,
                title: st.title || '',
                video_id: st.video_id || '',
              },
              createdAt: st.created_at || new Date().toISOString(),
              formData: {
                video_url: st.video_url || '',
                link: undefined,
                screenshot: undefined,
                platform: st.platform || '',
                quality: 'fast',
                model_name: '',
                provider_id: '',
              },
            }
            return task
          })
        if (missing.length === 0) return 0
        set(state => ({ tasks: [...state.tasks, ...missing] }))
        return missing.length
      },
    }),
    {
      name: 'task-storage',
      storage: createJSONStorage(() => ({
        getItem: async (name: string): Promise<string | null> => {
          const value = await get(name)
          return value ?? null
        },
        setItem: async (name: string, value: string): Promise<void> => {
          await set(name, value)
        },
        removeItem: async (name: string): Promise<void> => {
          await del(name)
        },
      })),
    }
  )
)
