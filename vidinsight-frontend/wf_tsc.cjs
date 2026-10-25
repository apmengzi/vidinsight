/**
 * 真实前端类型门控（棘轮模式）—— 2026-10-18 建立。
 *
 * 背景：vidinsight-frontend 根 tsconfig.json 是 files:[] 的 project-references 壳，
 * `tsc -p <根目录>` 一个文件都不查、永远 exit 0 —— 此前所有轮次的 "tsc 通过"
 * 全是假阴性（Gemini 实施、deepseek 实施均如此）。真查（-p tsconfig.app.json）
 * 暴露 118 个存量类型错误（12 个文件，多为 never 推断/隐式 any/缺字段声明），
 * 系项目从未真跑过 tsc 积攒的技术债，一次性修完不现实且高风险。
 *
 * 策略（大型代码库引入类型检查的标准做法 ratchet/baseline）：
 *   - .tsc-baseline 记录当前存量错误数
 *   - 门控 = 真查后「新增错误数 = 0」即通过（比 baseline 少则自动收紧）
 *   - node_modules 内的第三方源码错误（react-markdown@8 的 JSX namespace 等 4 条）过滤
 *
 * 用法：node wf_tsc.cjs   （退出码 0=通过，1=有新增错误并打印新增行）
 */
const { execSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = __dirname;
const BASELINE_FILE = path.join(ROOT, '.tsc-baseline');

try {
  const out = execSync(
    'npx tsc --noEmit -p tsconfig.app.json 2>&1',
    { cwd: ROOT, encoding: 'utf8', timeout: 300000, stdio: ['pipe', 'pipe', 'pipe'] },
  );
  var lines = out.split('\n').filter((l) => l.includes('error TS'));
} catch (e) {
  // tsc 有错误时 exit 2 且输出在 e.stdout —— 这正是我们要解析的
  var lines = String(e.stdout || '').split('\n').filter((l) => l.includes('error TS'));
}

// 过滤第三方包自身源码错误（skipLibCheck 管不到 .ts 源文件形式的包）
const ours = lines.filter((l) => !l.includes('node_modules'));
const count = ours.length;

const baseline = fs.existsSync(BASELINE_FILE)
  ? parseInt(fs.readFileSync(BASELINE_FILE, 'utf8').trim(), 10)
  : null;

if (baseline === null) {
  fs.writeFileSync(BASELINE_FILE, String(count));
  console.log(`[wf_tsc] 初次运行：baseline 记录为 ${count}（存量债务，见进度文件）`);
  console.log(`[wf_tsc] PASS（基线初始化）`);
  process.exit(0);
}

if (count <= baseline) {
  if (count < baseline) {
    fs.writeFileSync(BASELINE_FILE, String(count));
    console.log(`[wf_tsc] PASS —— 错误数 ${count} ≤ 基线 ${baseline}，基线自动收紧至 ${count}`);
  } else {
    console.log(`[wf_tsc] PASS —— 错误数 ${count} = 基线 ${baseline}（无新增）`);
  }
  process.exit(0);
}

// 有新增：打印新增文件的错误行（按文件聚合前 20 行帮助定位）
console.log(`[wf_tsc] FAIL —— 错误数 ${count} > 基线 ${baseline}（新增 ${count - baseline} 个）`);
console.log('[wf_tsc] 错误分布（按文件）：');
const byFile = {};
for (const l of ours) {
  const f = l.replace(/\(.*\)/, '');
  byFile[f] = (byFile[f] || 0) + 1;
}
for (const [f, n] of Object.entries(byFile).sort((a, b) => b[1] - a[1]).slice(0, 12)) {
  console.log(`  ${String(n).padStart(4)}  ${f}`);
}
process.exit(1);
