import axios, { AxiosInstance, AxiosResponse } from 'axios';
import toast from 'react-hot-toast'

// 统一响应类型
export interface IResponse<T = any> {
  code: number;
  msg: string;
  data: T;
  // 机器可读错误码（backend/app/exceptions/codes.py），透出到 toast 便于用户报障定位
  err?: string;
  detail?: Record<string, unknown>;
}

// 后端错误带 err 码时，toast 附加 [ERR码]——用户截图报障即可直接对码排障
const withErrCode = (msg: string, err?: string) =>
  err ? `${msg} [${err}]` : msg;

// 允许调用方在 axios 配置里带 suppressToast: true，让拦截器对【预期内的失败】
// 不弹全局红 toast（例如 onboarding 撞名重试、轮询健康检查）。业务代码自己 catch 处理。
declare module 'axios' {
  export interface AxiosRequestConfig {
    suppressToast?: boolean
  }
}

// 模拟一个消息提示函数 (实际项目中会使用UI库的组件，如 Ant Design 的 message 或 Element UI 的 ElMessage)
// This function simulates a message display (in real projects, you'd use a UI library's component)

const baseURL = import.meta.env.VITE_API_BASE_URL;

// 创建实例
 const request: AxiosInstance = axios.create({
  baseURL: baseURL || '/api',
  timeout: 10000,
});

// ---------------------------------------------------------------------------
// 匿名 owner：公开部署时每个浏览器只看到自己的任务。
// 首次访问生成 UUID 存 localStorage，之后每次请求带 X-VidInsight-Owner。
// 后端按此过滤任务列表（backend/app/services/owner_isolation.py）。
// 注意：这不是身份认证，只用于隔离误看；清缓存会变成新身份，旧任务不再可见。
// ---------------------------------------------------------------------------
const OWNER_KEY = 'vidinsight-owner-id';

const getOwnerId = (): string => {
  try {
    let id = localStorage.getItem(OWNER_KEY);
    if (!id || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
      id = (crypto.randomUUID?.() ?? `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`)
        .replace(/-/g, '');
      localStorage.setItem(OWNER_KEY, id);
    }
    return id;
  } catch {
    // 隐私模式/存储被禁：退化成一个会话级常量，行为同改造前（共享）
    return 'anonymous-fallback';
  }
};

request.interceptors.request.use((config) => {
  config.headers = config.headers ?? {};
  (config.headers as Record<string, string>)['X-VidInsight-Owner'] = getOwnerId();
  return config;
});

// 响应拦截器
request.interceptors.response.use(
  (response: AxiosResponse<IResponse>) => {
    const res = response.data;
    if (res.code === 0) {
      // 业务成功，可以根据需要显示成功消息，或者不显示（如果操作本身就是可见的）
      // showMessage('success', res.msg || '操作成功'); // 如果需要显示成功消息
      return res.data; // 返回data部分，简化后续业务代码
    } else {
      // 业务错误，统一显示后端返回的错误消息（除非调用方显式 suppressToast）
      if (!response.config?.suppressToast) {
        toast.error(withErrCode(res.msg || '操作失败，请稍后再试', res.err));
      }
      return Promise.reject(res); // 拒绝Promise，让业务代码可以捕获并处理
    }
  },
  (error) => {
    const suppress = error?.config?.suppressToast === true
    // 网络/服务器错误
    const res = error?.response?.data as IResponse | undefined;
    if (res) {
      const rejected = {
        ...res,
        httpStatus: error?.response?.status,
      }
      // 如果后端有返回错误信息，则显示后端信息
      if (!suppress) toast.error(withErrCode(res.msg || '服务器错误，请稍后再试', res.err));
      return Promise.reject(rejected);
    } else {
      // 没有响应数据（如网络中断），显示通用网络错误
      if (!suppress) toast.error('请求失败，请检查网络连接或稍后再试')
      return Promise.reject({
        code: -1,
        msg: '请求失败，请检查网络连接',
        data: null
      } as IResponse);
    }
  }
);

export default request
