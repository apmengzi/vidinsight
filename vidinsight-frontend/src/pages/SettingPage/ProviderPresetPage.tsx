/**
 * 供应商预设选择页（路由 /settings/model/preset）。
 *
 * 流程：点「添加模型供应商」→ 本页 → 选预设或自定义 → 带着预填参数进表单。
 * 预填通过 URL query 传递（preset=bigmodel-api），表单侧读 query 拉模板详情，
 * 避免引入全局状态。
 */
import { useNavigate } from 'react-router-dom'
import {
  ProviderTemplatePicker,
  type ProviderTemplate,
} from '@/components/Form/modelForm/ProviderTemplatePicker'

const ProviderPresetPage = () => {
  const navigate = useNavigate()

  const handlePick = (tpl: ProviderTemplate) => {
    // 只传 id，表单自己拉模板（避免把整份模板塞进 URL）
    navigate(`/settings/model/new?preset=${encodeURIComponent(tpl.id)}`)
  }

  const handleCustom = () => {
    navigate('/settings/model/new')
  }

  return (
    <div className="mx-auto max-w-3xl">
      <ProviderTemplatePicker onPick={handlePick} onCustom={handleCustom} />
    </div>
  )
}

export default ProviderPresetPage
