import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import { v4 as uuid } from 'uuid'
import { get as idbGet, set as idbSet, del as idbDel } from 'idb-keyval'

export interface ConversationMessage {
  role: 'user' | 'assistant'
  content: string
  timestamp: number
}

export interface ConversationTaskProgress {
  taskId: string
  status: string
  /** 归属会话 ID：轮询通过它 + store.getState() 定位目标，摆脱组件渲染闭包
   * （2026-10-20 实测：闭包过期会把成功状态写进别的会话，卡片永远"处理中"） */
  conversationId?: string
  title?: string
  url: string
  platform: string
  index: number
  /** 当前阶段百分比（0-100 或 null） */
  progress?: number
  stage_progress?: number | null
  elapsed_s?: number
  stage_times?: Record<string, number>
  failMsg?: string
  /** 后端阶段提示（如「正在加载转写模型（首次约 20 秒…）」）——进度卡副行展示 */
  message?: string
}

export interface Conversation {
  id: string
  title: string
  messages: ConversationMessage[]
  taskProgresses?: ConversationTaskProgress[]
  createdAt: number
  updatedAt: number
}

interface ConversationState {
  conversations: Conversation[]
  activeConversationId: string | null
  createConversation: () => string
  deleteConversation: (id: string) => void
  setActiveConversation: (id: string) => void
  addMessage: (conversationId: string, msg: Omit<ConversationMessage, 'timestamp'>) => void
  /** 原位更新一条消息的内容（收割进度气泡用），timestamp 刷新 */
  updateMessage: (conversationId: string, index: number, patch: Partial<Pick<ConversationMessage, 'content'>>) => void
  getMessages: (conversationId: string) => ConversationMessage[]
  updateTitle: (id: string, title: string) => void
  setConversationTasks: (conversationId: string, list: ConversationTaskProgress[]) => void
  updateConversationTasks: (
    conversationId: string,
    updater: (prev: ConversationTaskProgress[]) => ConversationTaskProgress[],
  ) => void
  pruneEmptyConversations: () => void
}

export const useConversationStore = create<ConversationState>()(
  persist(
    (set, get) => ({
      conversations: [],
      activeConversationId: null,

      createConversation: () => {
        const id = uuid()
        set(state => ({
          conversations: [
            {
              id,
              title: '新对话',
              messages: [],
              createdAt: Date.now(),
              updatedAt: Date.now(),
            },
            ...state.conversations,
          ],
          activeConversationId: id,
        }))
        return id
      },

      deleteConversation: (id) =>
        set(state => {
          const filtered = state.conversations.filter(c => c.id !== id)
          return {
            conversations: filtered,
            activeConversationId:
              state.activeConversationId === id
                ? filtered[0]?.id ?? null
                : state.activeConversationId,
          }
        }),

      setActiveConversation: (id) => set({ activeConversationId: id }),

      pruneEmptyConversations: () =>
        set(state => {
          // 历史遗留的空壳对话（应用每次挂载都会新建一个）只保留最新一个
          let seenEmpty = false
          const kept = state.conversations.filter(c => {
            const isEmpty =
              c.title === '新对话' &&
              (c.messages?.length ?? 0) === 0 &&
              (c.taskProgresses?.length ?? 0) === 0
            if (!isEmpty) return true
            if (seenEmpty) return false
            seenEmpty = true
            return true
          })
          if (kept.length === state.conversations.length) return state
          return {
            conversations: kept,
            activeConversationId: kept.some(c => c.id === state.activeConversationId)
              ? state.activeConversationId
              : kept[0]?.id ?? null,
          }
        }),

      addMessage: (conversationId, msg) =>
        set(state => ({
          conversations: state.conversations.map(c =>
            c.id === conversationId
              ? {
                  ...c,
                  messages: [...c.messages, { ...msg, timestamp: Date.now() }],
                  updatedAt: Date.now(),
                  // 用第一条用户消息作为标题
                  title:
                    c.title === '新对话' && msg.role === 'user'
                      ? msg.content.slice(0, 30) + (msg.content.length > 30 ? '...' : '')
                      : c.title,
                }
              : c,
          ),
        })),

      updateMessage: (conversationId, index, patch) =>
        set(state => ({
          conversations: state.conversations.map(c =>
            c.id === conversationId
              ? {
                  ...c,
                  messages: c.messages.map((m, i) =>
                    i === index ? { ...m, ...patch, timestamp: Date.now() } : m,
                  ),
                  updatedAt: Date.now(),
                }
              : c,
          ),
        })),

      getMessages: (conversationId) => {
        const conv = get().conversations.find(c => c.id === conversationId)
        return conv?.messages ?? []
      },

      updateTitle: (id, title) =>
        set(state => ({
          conversations: state.conversations.map(c =>
            c.id === id ? { ...c, title } : c,
          ),
        })),

      setConversationTasks: (conversationId, list) =>
        set(state => ({
          conversations: state.conversations.map(c =>
            c.id === conversationId
              ? { ...c, taskProgresses: list, updatedAt: Date.now() }
              : c,
          ),
        })),

      updateConversationTasks: (conversationId, updater) =>
        set(state => ({
          conversations: state.conversations.map(c =>
            c.id === conversationId
              ? { ...c, taskProgresses: updater(c.taskProgresses ?? []), updatedAt: Date.now() }
              : c,
          ),
        })),
    }),
    {
      name: 'vidinsight-conversations',
      // IndexedDB 存储：对话含完整报告正文，localStorage 5MB 配额写入会静默失败
      // （表现为刷新后对话全部消失）；idb-keyval 与 taskStore 同款方案
      storage: createJSONStorage(() => ({
        getItem: async (name: string): Promise<string | null> => (await idbGet(name)) ?? null,
        setItem: async (name: string, value: string): Promise<void> => {
          await idbSet(name, value)
        },
        removeItem: async (name: string): Promise<void> => {
          await idbDel(name)
        },
      })),
    },
  ),
)
