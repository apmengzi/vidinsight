/** 尊重系统「减弱动效」设置的平滑滚动行为。
 *
 * 为什么需要：CSS 的 `@media (prefers-reduced-motion)` 管不到 JS 调用的
 * `scrollTo({behavior:'smooth'})` / `scrollIntoView({behavior:'smooth'})`。
 * 前端有 3 处调用，统一走这里，避免每个组件各写一遍 matchMedia 判断。
 */
export function scrollBehavior(): ScrollBehavior {
  if (typeof window === 'undefined' || !window.matchMedia) return 'auto'
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ? 'auto'
    : 'smooth'
}
