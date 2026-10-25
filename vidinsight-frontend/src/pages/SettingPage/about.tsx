import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { ScrollArea } from '@/components/ui/scroll-area.tsx'
import logo from '@/assets/vidinsight-logo.png'

export default function AboutPage() {
  const appVersion = __APP_VERSION__

  const scenarios = [
    {
      title: '竞品短视频拆解',
      desc: '批量导入消费品、护肤、母婴、健身食品等竞品视频，提炼卖点、痛点和脚本结构。',
    },
    {
      title: '运营周报输出',
      desc: '将转写内容沉淀为结构化分析记录，辅助整理周报、选题库和投放复盘。',
    },
    {
      title: '洞察问答',
      desc: '基于已分析的视频资料提问，快速比较高频话术、用户关注点和可参考选题。',
    },
    {
      title: '桌面工作台',
      desc: '手机负责发现内容，桌面端负责批量分析、知识沉淀、报告输出和团队协作。',
    },
  ]

  return (
    <ScrollArea className="h-full overflow-y-auto bg-white">
      <div className="container mx-auto px-4 py-12">
        <div className="mb-14 flex flex-col items-center text-center">
          <div className="mb-4 flex items-center gap-4">
            <img src={logo} alt="VidInsight Logo" width={56} height={56} className="rounded-lg" />
            <div className="text-left">
              <h1 className="text-4xl font-bold">VidInsight / 灵悉</h1>
              <p className="text-muted-foreground mt-1 text-sm">v{appVersion}</p>
            </div>
          </div>
          <p className="text-muted-foreground max-w-3xl text-xl">
            面向企业内容运营与市场研究的短视频洞察工具
          </p>
          <div className="mt-6 flex flex-wrap justify-center gap-2">
            <Badge variant="secondary">内容运营</Badge>
            <Badge variant="secondary">市场研究</Badge>
            <Badge variant="secondary">竞品分析</Badge>
            <Badge variant="secondary">本地桌面端</Badge>
          </div>
        </div>

        <section className="mb-14">
          <h2 className="mb-5 text-center text-2xl font-semibold">项目定位</h2>
          <p className="mx-auto max-w-4xl text-center text-base leading-8 text-neutral-700">
            VidInsight / 灵悉帮助品牌方内容运营、市场研究人员把短视频从“刷到的素材”
            转化为“可交付的商业分析”。用户可以导入竞品视频链接或本地视频，系统自动完成转写、
            结构化分析、洞察问答和 Markdown 报告输出。
          </p>
        </section>

        <section className="mb-14">
          <h2 className="mb-6 text-center text-2xl font-semibold">核心能力</h2>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
            {scenarios.map(item => (
              <Card key={item.title} className="h-full">
                <CardContent className="pt-5">
                  <h3 className="mb-2 text-lg font-semibold">{item.title}</h3>
                  <p className="text-muted-foreground leading-7">{item.desc}</p>
                </CardContent>
              </Card>
            ))}
          </div>
        </section>

        <section className="mb-8">
          <h2 className="mb-5 text-center text-2xl font-semibold">典型工作流</h2>
          <div className="mx-auto grid max-w-5xl grid-cols-1 gap-3 text-sm md:grid-cols-5">
            {['发现竞品视频', '导入链接/文件', '生成结构化分析', '洞察问答', '输出周报/选题库'].map(
              step => (
                <div key={step} className="rounded-md border bg-neutral-50 px-4 py-3 text-center">
                  {step}
                </div>
              ),
            )}
          </div>
        </section>

        <footer className="border-t pt-6 text-center text-sm text-neutral-500">
          原型阶段项目名 VidInsight，汇报与演示品牌统一为 VidInsight / 灵悉。
        </footer>
      </div>
    </ScrollArea>
  )
}
