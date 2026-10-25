/**
 * LaTeX 预处理器 — 兜底处理 DeepSeek 输出的裸 LaTeX 命令。
 *
 * 核心逻辑（参照 ChatGPT-Next-Web / LobeChat 方案）：
 * 1. 保护代码块和已有 $ 分隔的公式
 * 2. 转换 \(...\) / \[...\] 为 $...$ / $$...$$
 * 3. 检测并包裹裸 LaTeX 命令
 */

// 最常见的 LaTeX 命令（出现在 LLM 输出中但未加 $ 分隔符）
const KNOWN_COMMANDS = new Set([
  // Greek letters
  'alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta', 'theta',
  'iota', 'kappa', 'lambda', 'mu', 'nu', 'xi', 'pi', 'rho', 'sigma',
  'tau', 'upsilon', 'phi', 'chi', 'psi', 'omega',
  'Gamma', 'Delta', 'Theta', 'Lambda', 'Xi', 'Pi', 'Sigma', 'Phi', 'Psi', 'Omega',
  // Operators & relations
  'pm', 'mp', 'times', 'div', 'cdot', 'ast', 'star', 'circ', 'bullet',
  'approx', 'equiv', 'sim', 'simeq', 'cong', 'propto', 'neq', 'doteq',
  'leq', 'geq', 'll', 'gg', 'prec', 'succ', 'preceq', 'succeq',
  'subset', 'supset', 'subseteq', 'supseteq', 'in', 'ni', 'notin',
  'cup', 'cap', 'setminus', 'land', 'lor', 'neg', 'forall', 'exists',
  'partial', 'nabla', 'infty', 'aleph', 'emptyset', 'varnothing',
  // Arrows
  'to', 'mapsto', 'implies', 'iff', 'Rightarrow', 'Leftarrow',
  'rightarrow', 'leftarrow', 'uparrow', 'downarrow',
  'longrightarrow', 'longleftarrow', 'Longrightarrow', 'Longleftarrow',
  'xrightarrow', 'xleftarrow',
  // Fractions, roots, sums, integrals
  'frac', 'sqrt', 'sum', 'prod', 'int', 'oint', 'iint', 'iiint', 'lim',
  // Functions
  'sin', 'cos', 'tan', 'cot', 'sec', 'csc',
  'arcsin', 'arccos', 'arctan',
  'sinh', 'cosh', 'tanh', 'coth',
  'log', 'ln', 'lg', 'exp', 'gcd', 'lcm',
  'max', 'min', 'sup', 'inf', 'limsup', 'liminf',
  'arg', 'det', 'dim', 'hom', 'ker', 'Pr',
  // Decorations & styling
  'mathbb', 'mathbf', 'mathcal', 'mathfrak', 'mathit', 'mathrm',
  'mathsf', 'mathtt', 'boldsymbol', 'boldsymbol',
  'text', 'textbf', 'textit', 'textrm', 'textsf', 'texttt',
  'vec', 'hat', 'tilde', 'bar', 'dot', 'ddot', 'widehat', 'widetilde',
  'overline', 'underline', 'overrightarrow', 'overleftarrow',
  // Brackets & spacing
  'left', 'right', 'big', 'Big', 'bigg', 'Bigg',
  'bigl', 'Bigl', 'biggl', 'Biggl',
  'quad', 'qquad', 'enspace', 'thinspace',
  'displaystyle', 'textstyle', 'scriptstyle',
  'operatorname', 'mathop', 'mathbin', 'mathrel',
  // Common in stats/probability
  'binom', 'choose', 'mathbb{E}', 'mathbb{P}', 'mathbb{R}',
  'mathcal{N}', 'mathcal{O}',
  // Misc
  'begin', 'end', 'hline', 'stackrel', 'underset', 'overset',
  'mod', 'pmod', 'bmod', 'mid', 'nmid', 'parallel', 'nparallel',
  'angle', 'triangle', 'square', 'diamond', 'clubsuit',
  'ell', 'hbar', 'imath', 'jmath', 'Re', 'Im',
  'ldots', 'cdots', 'vdots', 'ddots', 'therefore', 'because',
  'oplus', 'ominus', 'otimes', 'oslash', 'odot',
  'dagger', 'ddagger',
  'lceil', 'rceil', 'lfloor', 'rfloor', 'langle', 'rangle',
  'Vert', 'vert',
  'checkmark', 'Box',
])

// 匹配 LaTeX 命令: \name 或 \name* 或 \name[opt]{arg}...
// 支持一层大括号嵌套
const LATEX_CMD_RE = /\\([a-zA-Z]+)(\*)?(?:\[[^\]]*\])?(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})*/g

// 匹配已存在的 $ 分隔数学公式
const DOLLAR_MATH_RE = /\$\$[\s\S]*?\$\$|\$[^$\n]+?\$/g

// 匹配代码块
const CODE_BLOCK_RE = /```[\s\S]*?```|`[^`\n]+`/g

// 匹配 \(...\) 和 \[...\]
const PAREN_MATH_RE = /\\\[([\s\S]*?[^\\])\\\]|\\\((.*?[^\\])\\\)/g

/**
 * 预处理文本，确保 LaTeX 被正确包裹在 $ 分隔符中。
 */
export function preprocessLatex(content: string): string {
  // Step 0: 空文本直接返回
  if (!content || !content.includes('\\')) return content

  // Step 1: 保护代码块 / 行内代码
  const codeBlocks: string[] = []
  let processed = content.replace(CODE_BLOCK_RE, (match) => {
    codeBlocks.push(match)
    return `\x00C${codeBlocks.length - 1}\x00`
  })

  // Step 2: 保护已有的 $...$ / $$...$$ 数学公式
  const mathBlocks: string[] = []
  processed = processed.replace(DOLLAR_MATH_RE, (match) => {
    mathBlocks.push(match)
    return `\x00M${mathBlocks.length - 1}\x00`
  })

  // Step 3: 转换 \(...\) → $...$，\[...\] → $$...$$
  processed = processed.replace(PAREN_MATH_RE, (_match, display, inline) => {
    if (display) return `$$${display}$$`
    if (inline) return `$${inline}$`
    return _match
  })

  // Step 4: 检测裸 LaTeX 命令并按行包裹
  const lines = processed.split('\n')
  const resultLines: string[] = []

  for (const line of lines) {
    // 跳过保护标记行
    if (line.includes('\x00C') || line.includes('\x00M')) {
      resultLines.push(line)
      continue
    }

    // 检查该行是否包含裸 LaTeX 命令
    const latexCmds = findBareLatexCommands(line)
    if (latexCmds.length === 0) {
      resultLines.push(line)
      continue
    }

    // 判断是否整行都是数学（没有太多普通文字）
    const strippedLine = line
      .replace(LATEX_CMD_RE, '')  // 去掉所有 LaTeX 命令
      .replace(/[\s\d=+\-*/().,[\]|&:;!?<>%^]+/g, '') // 去掉运算符和空格
      .trim()

    if (strippedLine.length === 0 || strippedLine.length < 5) {
      // 几乎全是数学 → 整行用 $$ 包裹
      resultLines.push(`$$${line}$$`)
    } else {
      // 混合文本 → 逐个包裹 LaTeX 命令为 $...$
      resultLines.push(wrapInlineLatex(line))
    }
  }

  processed = resultLines.join('\n')

  // Step 5: 恢复保护的数学公式
  processed = processed.replace(/\x00M(\d+)\x00/g, (_, i) => mathBlocks[parseInt(i)] || '')

  // Step 6: 恢复代码块
  processed = processed.replace(/\x00C(\d+)\x00/g, (_, i) => codeBlocks[parseInt(i)] || '')

  return processed
}

/**
 * 查找一行中所有裸 LaTeX 命令（不在 $ 或保护标记中）。
 */
function findBareLatexCommands(line: string): RegExpMatchArray[] {
  const cmds: RegExpMatchArray[] = []
  const re = new RegExp(LATEX_CMD_RE.source, 'g')
  let match: RegExpMatchArray | null
  while ((match = re.exec(line)) !== null) {
    const cmdName = match[1]
    if (KNOWN_COMMANDS.has(cmdName)) {
      cmds.push(match)
    }
  }
  return cmds
}

/**
 * 将行内裸 LaTeX 命令逐个包裹在 $...$ 中。
 */
function wrapInlineLatex(line: string): string {
  // 逐个替换已知命令 → $\\cmd...$
  let result = line
  for (const cmdName of KNOWN_COMMANDS) {
    // 构建命令的正则：\cmd 后跟可选的 *、[...] 参数、{...} 参数
    const cmdRe = new RegExp(
      `\\\\${escapeRegex(cmdName)}(\\*)?(\\[[^\\]]*\\])?(\\{[^{}]*(?:\\{[^{}]*\\}[^{}]*)*\\})*`,
      'g'
    )
    result = result.replace(cmdRe, (match) => {
      // 检查是否已在 $ 中
      return `$${match}$`
    })
  }
  return result
}

/**
 * 转义正则特殊字符。
 */
function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
