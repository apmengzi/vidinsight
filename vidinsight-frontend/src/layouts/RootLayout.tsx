import type { ReactNode, FC } from 'react'
// import "@/global.css"
import { Toaster } from 'react-hot-toast'

interface RootLayoutProps {
  children: ReactNode
}

export const metadata = {
  title: 'VidInsight / 灵悉 - 短视频内容洞察工具',
  description: '面向企业内容运营与市场研究的短视频洞察工具',
}

const RootLayout: FC<RootLayoutProps> = ({ children }) => {
  return (
    <div className="min-h-screen bg-neutral-100 font-sans text-neutral-900">
      <Toaster
        position="top-center" // 顶部居中显示
        toastOptions={{
          style: {
            borderRadius: '8px',
            background: '#333',
            color: '#fff',
          },
        }}
      />
      {children}
    </div>
  )
}

export default RootLayout
