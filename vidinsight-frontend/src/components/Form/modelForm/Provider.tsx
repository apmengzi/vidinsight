import ProviderCard from '@/components/Form/modelForm/components/providerCard.tsx'
import { Button } from '@/components/ui/button.tsx'
import { useProviderStore } from '@/store/providerStore'
import { useNavigate } from 'react-router-dom'

const Provider = () => {
  const providers = useProviderStore(state => state.provider)
  const navigate = useNavigate()
  const handleClick = () => {
    // 先进预设选择页（选 BigModel/Z.ai/DeepSeek 等自动填好 baseUrl 和协议），
    // 想手填再点「创建自定义供应商」——用户手填 baseUrl 极易配错
    navigate(`/settings/model/preset`)
  }

  return (
    <div className="flex flex-col gap-2">
      <div className={'search flex gap-1 py-1.5'}>
        <Button
          type={'button'}
          onClick={() => {
            handleClick()
          }}
          className="w-full"
        >
          添加模型供应商
        </Button>
      </div>
      <div className="text-sm font-light">模型供应商列表</div>
      <div>
        {providers &&
          providers.map((provider, index) => {
            return (
              <ProviderCard
                key={index}
                providerName={provider.name}
                Icon={provider.logo}
                id={provider.id}
                enable={provider.enabled}
              />
            )
          })}
      </div>
    </div>
  )
}
export default Provider
