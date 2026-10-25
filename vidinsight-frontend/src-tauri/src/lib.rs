use tauri::{Manager, Emitter, State};
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use std::env;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant};
use serde::Serialize;

// Sidecar 启动期内前端不该看到「加载中」无限转。
// 总等待上限 = 启动期 PyInstaller 解压 + uvicorn bind 时间的最坏估计，
// 实测 macOS / Windows 慢盘大概 5-20s，设 45s 留余量但不至于让用户绝望。
const BACKEND_STARTUP_TIMEOUT_SECS: u64 = 45;
const BACKEND_DEFAULT_PORT: u16 = 8483;

/// 所有代码创建的 WebView 窗口的浏览器参数——必须与 tauri.conf.json 主窗口的
/// additionalBrowserArgs **逐字符一致**：同一 user-data-dir 下 WebView2 只允许一套
/// 环境参数，主窗口（带 direct://）先启动 browser 进程后，后续窗口若用 wry 默认
/// 参数再建环境会直接失败（2026-10-20 实测：捕获窗口白死，导航都不发生）。
/// direct:// = 内嵌页绕开系统代理直连（App 只访问国内平台+回环，Clash 抖动与
/// 海外出口风控都避开）；disable-features 三件是 wry 的默认值，替换语义下手动带回。
const WEBVIEW_ARGS: &str =
    "--proxy-server=direct:// --disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection";

// Sidecar 子进程句柄，用 Mutex 包裹方便 restart 时杀旧进程
struct SidecarHandle(Mutex<Option<CommandChild>>);



// ── Cookie 一次性授权捕获（应用内登录）─────────────────────
// 用户在 VidInsight 自己的 WebView 里登录一次站点（如小红书），
// 命中目标 Cookie（web_session / SESSDATA 等）后自动收齐整串 Cookie 返回给前端保存。
// HttpOnly Cookie 只有 WebView 自己能读（cookies_for_url）——既绕开了 Chrome
// 应用绑定加密（ABE）对第三方进程读 Cookie 的封锁，也是桌面应用「一次授权、长期使用」的标准做法。

#[tauri::command]
async fn capture_site_cookie(
    app: tauri::AppHandle,
    site: String,
    wait_cookie: String,
) -> Result<String, String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    let label = format!(
        "login-capture-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    let parsed: tauri::Url = site
        .parse()
        .map_err(|e| format!("无效地址: {e}"))?;

    let win = WebviewWindowBuilder::new(
        &app,
        label.clone(),
        WebviewUrl::External(parsed.clone()),
    )
    .additional_browser_args(WEBVIEW_ARGS)
    .title("在窗口内登录后，本窗口会自动关闭")
    .inner_size(460.0, 720.0)
    .build()
    .map_err(|e| format!("打开登录窗口失败: {e}"))?;

    let win_ref = win.clone();
    let app_ref = app.clone();
    let label_ref = label.clone();
    let poll_url = parsed.clone();
    let res = tauri::async_runtime::spawn_blocking(move || {
        let deadline = Instant::now() + Duration::from_secs(300);
        while Instant::now() < deadline {
            // 用户手动关窗 → 立即终止而不是傻等 5 分钟
            if app_ref.get_webview_window(&label_ref).is_none() {
                return Err("登录窗口已关闭，未捕获到登录态".to_string());
            }
            if let Ok(cookies) = win_ref.cookies_for_url(poll_url.clone()) {
                let pairs: Vec<String> = cookies
                    .iter()
                    .map(|c| format!("{}={}", c.name(), c.value()))
                    .collect();
                if pairs
                    .iter()
                    .any(|p| p.starts_with(&format!("{}=", wait_cookie)))
                {
                    return Ok(pairs.join("; "));
                }
            }
            std::thread::sleep(Duration::from_millis(1500));
        }
        Err("5 分钟内未捕获到登录态，请确认已在窗口内登录成功".to_string())
    })
    .await
    .map_err(|e| format!("捕获任务失败: {e}"))?;

    let _ = win.close();
    res
}

// ── B站 WebView 兜底元数据（真人浏览器内核，对抗风控）─────────
// yt-dlp 被 B 站风控(412)拦截时的预览兜底：真实 WebView 加载视频页，
// 注入脚本从 __INITIAL_STATE__.videoData 取标题/时长/封面/UP主，
// POST 回后端暂存（/api/parse/webview_meta），前端轮询取回。
// 全程真人浏览器指纹与登录态，风控无法区分。

#[tauri::command]
async fn fetch_bilibili_meta(
    app: tauri::AppHandle,
    url: String,
    task: String,
) -> Result<String, String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    if !url.contains("bilibili.com") {
        return Err("仅支持 B 站链接".to_string());
    }
    let parsed: tauri::Url = url.parse().map_err(|e| format!("无效地址: {e}"))?;

    let label = format!(
        "bili-meta-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    let win = WebviewWindowBuilder::new(
        &app,
        label.clone(),
        WebviewUrl::External(parsed),
    )
    .additional_browser_args(WEBVIEW_ARGS)
    .title("正在读取视频信息…（读取完成后自动关闭）")
    .inner_size(720.0, 540.0)
    .build()
    .map_err(|e| format!("打开窗口失败: {e}"))?;

    // 注入脚本：取 videoData 并回传后端。CSP 无法拦截 fetch 同源/跨源简单请求，
    // B 站页面 CSP 允许 connect-src 到自身；跨源 POST 到 127.0.0.1 属 simple request
    // （application/json 预检会被拦，用 text/plain 规避预检），后端按 raw body 解析。
    let inject = format!(
        r#"(async () => {{ try {{ const s = window.__INITIAL_STATE__; const v = s && s.videoData; if (!v) return; const payload = {{bvid: v.bvid, title: v.title, duration: v.duration, cover: v.pic, uploader: v.owner && v.owner.name, desc: (v.desc||'').slice(0,200)}}; await fetch('http://127.0.0.1:8483/api/parse/webview_meta?task={t}', {{method:'POST', headers:{{'Content-Type':'text/plain'}}, body: JSON.stringify({{task:'{t}', data: payload}})}}); }} catch(e) {{}} }})()"#,
        t = task
    );

    let win_ref = win.clone();
    let app_ref = app.clone();
    let label_ref = label.clone();
    let inject_ref = inject.clone();
    let task_ref = task.clone();
    let res = tauri::async_runtime::spawn_blocking(move || {
        let deadline = Instant::now() + Duration::from_secs(60);
        // 等待页面水合（__INITIAL_STATE__ 由 SSR 直出，稍等即有）
        std::thread::sleep(Duration::from_millis(3500));
        while Instant::now() < deadline {
            // 用户关窗 → 立即终止（与 capture_site_cookie 同款检测）
            if app_ref.get_webview_window(&label_ref).is_none() {
                return Err("窗口已关闭".to_string());
            }
            let _ = win_ref.eval(&inject_ref);
            std::thread::sleep(Duration::from_millis(2000));
            if let Ok(resp) = ureq::get(
                format!("http://127.0.0.1:8483/api/parse/webview_meta/{}", task_ref).as_str(),
            )
            .timeout(Duration::from_secs(4))
            .call()
            {
                if let Ok(body) = resp.into_string() {
                    if body.contains("\"ready\":true") {
                        return Ok(body);
                    }
                }
            }
        }
        Err("60 秒内未能读取视频信息（页面可能要求扫码登录）".to_string())
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?;

    let _ = win.close();
    res
}

// ── hellotik 兜底（B 站专用）──────────────────────────────
// 做法完全照搬真人：打开 hellotik 网页 → 在输入框里粘贴链接 → 点「解析视频」
// → 从结果页已渲染的 <video src> 读出平台 CDN 无水印直链 → POST 回后端。
//
// 不碰它的加密协议（gate 票据 + AES-GCM），所以不受它每周换协议 hash 的影响。
// 实测边界（2026-10-04）：B站 ✅；YouTube/抖音/小红书 ✗ —— 故只对 B 站开放。
// 用途定位：兜底通道，主通道仍是 yt-dlp+Cookie，失败时用户手动点这个按钮。

#[tauri::command]
async fn fetch_hellotik_direct(
    app: tauri::AppHandle,
    url: String,
    task: String,
) -> Result<String, String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    // 2026-10-08：用户反馈抖音实测可用（我这边只有自造 ID，不敢据此判不支持——
    // 同样的错在小红书犯过一次）。故抖音通道放开，待真实链接实测确认。
    // 仅 YouTube 实测三次均失败，明确不支持。
    if url.contains("youtube.com") || url.contains("youtu.be") {
        return Err("hellotik 不支持 YouTube（实测三次均解析失败）".to_string());
    }
    if !url.contains("bilibili.com") && !url.contains("b23.tv")
        && !url.contains("xiaohongshu.com") && !url.contains("xhslink")
        && !url.contains("douyin.com")
    {
        return Err("hellotik 兜底目前支持 B 站 / 小红书 / 抖音".to_string());
    }

    let label = format!(
        "hellotik-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    // 进对应平台的专属页（真人也是这么用的）；小红书页同样能解析 B 站，
    // 但专属页命中率更高。整段分享文案（含标题与「拷走这段」尾巴）可直接贴入，
    // 站点会自行从中提取链接——已实测通过。
    let entry = if url.contains("xiaohongshu.com") || url.contains("xhslink") {
        "https://www.hellotik.app/zh/rednote"
    } else if url.contains("douyin.com") {
        "https://www.hellotik.app/zh/douyin"
    } else {
        "https://www.hellotik.app/zh"
    };
    let home: tauri::Url = entry.parse().map_err(|e| format!("无效地址: {e}"))?;

    let win = WebviewWindowBuilder::new(&app, label.clone(), WebviewUrl::External(home))
        .additional_browser_args(WEBVIEW_ARGS)
        .title("兜底解析中…（读完后自动关闭）")
        .inner_size(760.0, 620.0)
        .build()
        .map_err(|e| format!("打开窗口失败: {e}"))?;

    // 第一步：像真人一样填框 + 点「解析视频」。URL 先裁掉 query（?p=/spm_id_from
    // 等）可提高命中率；b23.tv 短链保持原样交给它服务端跳转。
    let safe_url = url.replace('\\', "\\\\").replace('\'', "\\'").replace(['\n', '\r'], "");
    let fill = format!(
        r#"(function(){{try{{var u='{u}';var i=document.querySelector('input[type=text],input:not([type])');if(!i)return 'NO_INPUT';var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(i,u);i.dispatchEvent(new Event('input',{{bubbles:true}}));var b=document.querySelector('button[type=submit]')||[].slice.call(document.querySelectorAll('button')).filter(function(x){{return (x.textContent||'').indexOf('解析')>=0;}})[0];if(b){{b.click();return 'CLICKED';}}return 'NO_BUTTON';}}catch(e){{return 'ERR:'+e.message;}}}})()"#,
        u = safe_url
    );

    // 第二步：结果页出来后读已渲染的 <video src> + 标题 + 封面，回传后端。
    // 标题必须在 video 所在的结果卡里找，直接取 h1/h2/h3 会命中页头站名。
    let extract = format!(
        r#"(function(){{try{{var v=document.querySelector('video');var src=(v&&(v.src||(v.querySelector('source')||{{}}).src))||'';if(!src)return 'NO_RESULT';var title='',cover='';var card=v.closest('div[class]');for(var k=0;k<6&&card;k++){{var t=card.querySelector('h1,h2,h3,h4');if(t&&(t.textContent||'').trim()){{title=t.textContent.trim();break;}}card=card.parentElement;}}var img=card?card.querySelector('img'):null;if(img&&img.src&&img.src.indexOf('data:')!==0){{cover=img.src;}}var payload={{task:'{t}',data:{{direct_url:src,title:title,cover:cover,source_url:'{u}'}}}};fetch('http://127.0.0.1:8483/api/parse/hellotik',{{method:'POST',headers:{{'Content-Type':'text/plain'}},body:JSON.stringify(payload)}});return 'SENT';}}catch(e){{return 'ERR:'+e.message;}}}})()"#,
        t = task,
        u = safe_url
    );

    let win_ref = win.clone();
    let app_ref = app.clone();
    let label_ref = label.clone();
    let task_ref = task.clone();

    let res = tauri::async_runtime::spawn_blocking(move || {
        let deadline = Instant::now() + Duration::from_secs(90);
        // 等首屏水合（React 挂载出输入框才能填）
        std::thread::sleep(Duration::from_millis(4000));
        let mut filled = false;
        while Instant::now() < deadline {
            if app_ref.get_webview_window(&label_ref).is_none() {
                return Err("窗口已关闭".to_string());
            }
            if !filled {
                let _ = win_ref.eval(&fill);
                filled = true;
                std::thread::sleep(Duration::from_millis(2500));
                continue;
            }
            let _ = win_ref.eval(&extract);
            std::thread::sleep(Duration::from_millis(2500));
            if let Ok(resp) = ureq::get(
                format!("http://127.0.0.1:8483/api/parse/hellotik/{}", task_ref).as_str(),
            )
            .timeout(Duration::from_secs(4))
            .call()
            {
                if let Ok(body) = resp.into_string() {
                    if body.contains("\"ready\":true") {
                        return Ok(body);
                    }
                }
            }
        }
        Err("90 秒内未能取到直链（可能是该站限流或页面需要验证）".to_string())
    })
    .await
    .map_err(|e| format!("任务失败: {e}"))?;

    let _ = win.close();
    res
}

// ── 小红书合集读取（WebView 被动捕获）─────────────────
// XHS 网页版硬规则：笔记详情（explore/{id}）只对带有效 xsec_token 的 URL 出数据，
// 无 token 一律码墙（登录态也一样）；而合集页 SSR 的 noteList 不带 token，
// token 只出现在登录态 SPA 渲染出的卡片 href 里（由页面自己的签名 API 取回）。
//
// 2026-09-15 事故：自动收割（自动导航/搜索/滚动）触发小红书官方账号异常
// 警告（浏览行为不像人类），已整体废弃。当前唯一模式 = 被动捕获：
// 窗口加载合集页后只当记录仪，用户像平常一样浏览并点开想分析的笔记，
// 注入脚本的 XHR/fetch 钩子 + 每次自然导航后的 __INITIAL_STATE__ 深扫，
// 顺路记录 XHS 自己返回的笔记数据（noteDetailMap → /ingest_state，
// 带 xsecToken 的笔记对象 → /ingest_tokens）。全程零导航零滚动，
// 流量特征 100% 真人。用户关闭窗口 = 浏览完成，已记录数据全部保留。

#[tauri::command]
async fn fetch_xhs_collection_detail(
    app: tauri::AppHandle,
    url: String,
    passive: Option<bool>,
) -> Result<String, String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    if !url.contains("xiaohongshu.com") {
        return Err("仅支持小红书链接".to_string());
    }
    // 2026-09-15：自动收割（导航/搜索/滚动）触发小红书账号异常警告，
    // 已整体废弃。当前唯一模式 = 被动捕获：窗口只当记录仪，用户像平常
    // 一样浏览，工具零导航零滚动，只顺路记录 XHS 自己返回的笔记数据。
    let passive = passive.unwrap_or(true);

    let label = format!(
        "xhs-collect-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    let parsed: tauri::Url = url.parse().map_err(|e| format!("无效地址: {e}"))?;
    let cookie_url = parsed.clone(); // 收割成功后读 cookie 用（builder 会 move parsed）

    // 状态机 v4：initialization_script 在每次文档创建时自动注入（导航不丢），
    // JS 每 1s 自 tick；信号经后端 /xhs/harvest_signal 中转给 Rust 轮询。
    // v3 的两处致命伤：
    //   1) 用 document.title 传信号——WebView2 不会把 document.title 同步到原生
    //      窗口标题，win.title() 永远返回初始值 → Rust 永远收不到终态 → 窗口
    //      卡到 6 分钟看门狗（用户看到的「卡在这里」）
    //   2) find-author 抓页面上第一个 /user/profile/ 链接——登录后 XHS 跳到
    //      用户自己的主页，抓到的 uid 是用户自己 → 在错误的主页上无限下滑
    const HARVEST_JS_TEMPLATE: &str = r#"
(function(){
  if (window.top !== window) return;
  if (window.__VIB_TICK__) return;
  window.__VIB_TICK__ = true;
  var LABEL = '__LABEL__';
  var API = 'http://127.0.0.1:8483/api/xhs';
  function sig(ev, data){
    try {
      fetch(API + '/harvest_signal', {
        method: 'POST', headers: { 'Content-Type': 'text/plain' },
        body: JSON.stringify({ label: LABEL, event: ev, data: data || null })
      }).catch(function(){});
    } catch(e) {}
  }
  function bodyText(){ return document.body ? document.body.innerText : ''; }
  function loginWall(){
    var t = bodyText();
    return t.indexOf('登录后查看搜索结果') >= 0 || t.indexOf('扫码登录') >= 0 ||
           t.indexOf('登录超限') >= 0 || t.indexOf('新用户可直接登录') >= 0 ||
           t.indexOf('输入手机号') >= 0;
  }
  function loadQ(){ try { return JSON.parse(sessionStorage.getItem('__VIBQ') || 'null'); } catch(e) { return null; } }
  function saveQ(q){ sessionStorage.setItem('__VIBQ', JSON.stringify(q)); }
  // 诊断上报：多探针定位「收割 0 token」根因。cookie 探针只是辅助
  // （web_session 可能 HttpOnly）；state 里 loggedIn 计数是最强登录态信号；
  // tokenedHrefs 不筛合集 ids，样例 3 条足够分辨 href 通道死活。
  function diag(where, extra){
    try {
      var samples = [], tokened = 0, cards = 0;
      document.querySelectorAll('a[href]').forEach(function(a){
        var h = a.getAttribute('href') || '';
        if (h.indexOf('xsec_token=') >= 0) {
          tokened++;
          if (samples.length < 3) samples.push(h.slice(0, 120));
        }
        if (h.indexOf('/explore/') >= 0 || h.indexOf('/discovery/item/') >= 0 ||
            h.indexOf('/search_result/') >= 0) cards++;
      });
      var s = '', liTrue = 0, liFalse = 0, stateTok = 0;
      try {
        s = JSON.stringify(window.__INITIAL_STATE__ || {});
        var m1 = s.match(/"loggedIn":true/g); liTrue = m1 ? m1.length : 0;
        var m2 = s.match(/"loggedIn":false/g); liFalse = m2 ? m2.length : 0;
        var m3 = s.match(/xsec_token/g); stateTok = m3 ? m3.length : 0;
      } catch(e) {}
      var hasLoginBtn = !!document.querySelector('.login-btn') ||
        Array.prototype.some.call(document.querySelectorAll('a,button,div'), function(el){
          return el.children.length === 0 && (el.textContent || '').trim() === '登录';
        });
      var payload = { where: where, path: location.pathname,
        loginBtn: hasLoginBtn, cookieSession: document.cookie.indexOf('web_session') >= 0,
        stateLoggedIn: liTrue > 0 && liFalse === 0, liTrue: liTrue, liFalse: liFalse,
        tokenedHrefs: tokened, cardHrefs: cards, samples: samples,
        stateTokenCount: stateTok };
      if (extra) for (var k in extra) payload[k] = extra[k];
      sig('diag', payload);
    } catch(e) {}
  }
  // 兜底②：作者主页的 token 可能不在卡片 href，而在 SPA __INITIAL_STATE__
  // 的笔记对象里（xsecToken 字段）。泛型递归收集：不依赖具体 schema 路径，
  // 只要对象同时携带 xsecToken 与合集内的 note id 就收。
  function stateTokens(ids){
    var out = {}, scanned = 0;
    try {
      (function walk(v){
        if (!v || typeof v !== 'object' || scanned > 30000) return;
        scanned++;
        if (Array.isArray(v)) { for (var i = 0; i < v.length; i++) walk(v[i]); return; }
        var tok = v.xsecToken || v.xsec_token;
        if (typeof tok === 'string' && tok) {
          var nid = v.noteId || v.note_id || v.id;
          if (typeof nid === 'string' && /^[0-9a-f]{24}$/.test(nid) &&
              ids.indexOf(nid) >= 0 && !out[nid]) {
            var src = v.xsecSource || v.xsec_source || 'pc_user';
            out[nid] = 'https://www.xiaohongshu.com/explore/' + nid +
                       '?xsec_token=' + encodeURIComponent(tok) + '&xsec_source=' + src;
          }
        }
        for (var k in v) walk(v[k]);
      })(window.__INITIAL_STATE__);
    } catch(e) {}
    return out;
  }
  function finish(links, ev){
    if (links.length) {
      fetch(API + '/ingest_tokens', { method: 'POST', headers: { 'Content-Type': 'text/plain' },
        body: JSON.stringify({ links: links }) })
        .then(function(){ sessionStorage.removeItem('__VIBQ'); sig(ev, { count: links.length }); })
        .catch(function(){ sessionStorage.removeItem('__VIBQ'); sig('data', { links: links }); });
    } else {
      var qe = loadQ();
      diag('harvest-end', { got: qe ? Object.keys(qe.found || {}).length : 0,
                            idsTotal: qe ? (qe.ids || []).length : 0 });
      sessionStorage.removeItem('__VIBQ'); sig('none');
    }
  }
  // 逐条抓笔记详情（同源 fetch 自带登录 cookie）：解析 SSR state 里的笔记节点
  // POST 到 /xhs/ingest_state → 后端任务直接命中 XHSNoteCache。
  // 为什么必须走 webview：后端 curl 的存量 cookie 过期会把请求毒化成登录墙，
  // 而匿名+token 的 SSR 时灵时不灵（2026-09-15 实测两次结果不同）——只有
  // 登录态 webview 是稳定通道。
  function collectDetails(links, idx, ev){
    if (idx >= links.length) { finish(links, ev); return; }
    document.title = 'VidInsight 抓取笔记详情 ' + (idx + 1) + '/' + links.length + '…';
    fetch(links[idx], { credentials: 'include' })
      .then(function(r){ return r.text(); })
      .then(function(html){
        try {
          var m = html.match(/window\.__INITIAL_STATE__\s*=\s*([\s\S]*?)<\/script>/);
          if (m) {
            var raw = m[1]
              .replace(/\bundefined\b/g, 'null')
              .replace(/new\s+Map\(\s*\[\s*\]\s*\)/g, '{}')
              .replace(/new\s+Set\(\s*\[\s*\]\s*\)/g, '[]')
              .replace(/new\s+Map\((\[\s*[\s\S]*?\s*\])\)/g, '$1');
            var st = JSON.parse(raw);
            // 笔记节点所在位置随页面版本变（noteData.data / 顶层 note / AiNoteDetailStore），
            // 深扫所有 noteDetailMap，按 note_id 去重后全部上报
            var seen = {};
            function walkMap(o, depth){
              if (!o || typeof o !== 'object' || depth > 5) return;
              for (var k in o){
                var v = o[k];
                if (!v || typeof v !== 'object') continue;
                if (k === 'noteDetailMap'){
                  for (var id in v){
                    var node = (v[id] || {}).note;
                    if (node && typeof node === 'object' && (node.noteId || id)
                        && !seen[node.noteId || id]){
                      seen[node.noteId || id] = 1;
                      fetch(API + '/ingest_state', { method: 'POST',
                        headers: { 'Content-Type': 'text/plain' },
                        body: JSON.stringify({ note_id: node.noteId || id, note: node })
                      }).catch(function(){});
                    }
                  }
                } else {
                  walkMap(v, depth + 1);
                }
              }
            }
            walkMap(st, 0);
          }
        } catch(e) { diag('detail-parse-fail', { idx: idx, err: String(e).slice(0, 80) }); }
        setTimeout(function(){ collectDetails(links, idx + 1, ev); }, 400);
      })
      .catch(function(){ setTimeout(function(){ collectDetails(links, idx + 1, ev); }, 400); });
  }
  function tick(){
    try {
      var captcha = !!(document.querySelector('[class*="captcha"]') || bodyText().indexOf('安全验证') >= 0);
      if (captcha) { document.title = 'VidInsight 请完成安全验证（拖动滑块），完成后自动继续'; sig('captcha'); return; }
      var q = loadQ();
      var path = location.pathname;

      if (!q) {
        // 阶段 0：合集页 SSR 读笔记清单 + 作者昵称（无需登录态）
        var s = window.__INITIAL_STATE__;
        var nl = s && s.noteData && s.noteData.collectionData ? (s.noteData.collectionData.noteList || []) : [];
        if (!nl.length) {
          if (!window.__VIB_W0) window.__VIB_W0 = Date.now();
          if (Date.now() - window.__VIB_W0 > 20000) {
            window.__VIB_W0 = Date.now();
            sig('fail', { reason: '合集页未能读取到笔记清单：链接可能失效或页面被重定向。请确认窗口里显示的是合集页后重试' });
          }
          return;
        }
        var author = '';
        for (var k = 0; k < nl.length; k++) {
          var u = nl[k].user || {};
          if (u.nickname) { author = u.nickname; break; }
        }
        if (!author) { sig('fail', { reason: '合集笔记未携带作者昵称，无法定位作者主页' }); return; }
        sessionStorage.setItem('__VIB_COL', location.href);
        q = { ids: nl.map(function(n){ return n.id; }).filter(Boolean),
              author: author, phase: 'search', candidates: [], tried: [], found: {},
              navRetries: 0, restarts: 0, pageSince: Date.now() };
        saveQ(q);
        document.title = 'VidInsight 正在搜索作者 ' + author + '…（请勿关闭）';
        sig('phase', { msg: '正在搜索作者 ' + author + '…' });
        q.pageSince = Date.now(); saveQ(q);
        location.href = 'https://www.xiaohongshu.com/search_result?keyword=' + encodeURIComponent(author) + '&type=54';
        return;
      }

      if (loginWall()) {
        q.phase = 'login'; saveQ(q);
        document.title = '请在本窗口内登录小红书（扫码或手机号），登录后自动继续读取合集';
        sig('login');
        return;
      }

      if (q.phase === 'login') {
        // 登录墙消失 = 登录成功。登录后 XHS 常跳到个人主页——一律回合集页
        // 重启整条流程（限 2 次），绝不让旧阶段状态在陌生页面上继续跑
        q.restarts = (q.restarts || 0) + 1;
        if (q.restarts > 2) { sig('fail', { reason: '登录后多次未能重新读取合集，请关闭窗口重试' }); return; }
        var col = sessionStorage.getItem('__VIB_COL') || 'https://www.xiaohongshu.com';
        sessionStorage.removeItem('__VIBQ');
        sig('phase', { msg: '登录成功，正在重新读取合集…' });
        location.href = col;
        return;
      }

      if (q.phase === 'search') {
        if (path.indexOf('/search_result') !== 0) {
          // 被甩到别的页（重定向/风控）→ 回搜索页
          if (Date.now() - q.pageSince > 8000) {
            q.navRetries = (q.navRetries || 0) + 1;
            if (q.navRetries > 3) { sig('fail', { reason: '搜索页多次被重定向，疑似触发风控，请几分钟后重试' }); return; }
            q.pageSince = Date.now(); saveQ(q);
            location.href = 'https://www.xiaohongshu.com/search_result?keyword=' + encodeURIComponent(q.author) + '&type=54';
          }
          return;
        }
        var cands = [];
        document.querySelectorAll('a[href*="/user/profile/"]').forEach(function(a){
          var m = (a.getAttribute('href') || '').match(/user\/profile\/([0-9a-f]{24})/);
          // 只收文本含作者昵称的搜索结果卡片。登录态顶栏自己的头像 href 也是
          // /user/profile/（v4.1 诊断实锤：首候选抓到自己主页 → 昵称不符 → 候选
          // 耗尽误报 fail）；结果卡片 anchor 的文本必含昵称，顶栏头像链接无文本。
          if (m && cands.indexOf(m[1]) < 0 && q.tried.indexOf(m[1]) < 0 &&
              (a.textContent || '').indexOf(q.author) >= 0) cands.push(m[1]);
        });
        if (!cands.length) {
          // 结果是客户端渲染：边滚边等（顺带把排名靠后的作者滚出来）。
          // 翻页容错（XHS 用户搜索排序不稳定，实测同一链接两次结果不同）：
          // 有界滚动 8 拍 → 仍无候选 → 二次搜索重进一次 → 还无才失败，
          // 失败时 diag 带全现场（candidates/tried/scrolls）。
          window.scrollTo(0, document.body.scrollHeight);
          q.scrollAttempts = (q.scrollAttempts || 0) + 1;
          if (q.scrollAttempts > 8) {
            if (!q.researched) {
              q.researched = true; q.scrollAttempts = 0; q.pageSince = Date.now();
              saveQ(q);
              diag('search-retry', { author: q.author, tried: q.tried });
              document.title = 'VidInsight 二次搜索作者…（请勿关闭）';
              location.href = 'https://www.xiaohongshu.com/search_result?keyword=' + encodeURIComponent(q.author) + '&type=54';
              return;
            }
            diag('search-exhausted', { author: q.author, candidates: q.candidates, tried: q.tried, scrolls: q.scrollAttempts });
            sig('fail', { reason: '用户搜索结果中未找到「' + q.author + '」的卡片（排序未命中或登录态失效），请几分钟后重试' });
            return;
          }
          q.pageSince = Date.now(); saveQ(q);
          document.title = 'VidInsight 翻页搜索作者中…(' + q.scrollAttempts + '/8)';
          return;
        }
        q.scrollAttempts = 0;
        q.candidates = q.candidates.concat(cands.filter(function(c){ return q.candidates.indexOf(c) < 0; }));
        q.tried.push(cands[0]);
        q.currentUid = cands[0]; q.phase = 'verify'; q.pageSince = Date.now();
        saveQ(q);
        document.title = 'VidInsight 验证作者主页…（请勿关闭）';
        sig('phase', { msg: '验证作者主页…' });
        location.href = 'https://www.xiaohongshu.com/user/profile/' + cands[0];
        return;
      }

      if (q.phase === 'verify') {
        if (path.indexOf('/user/profile/') !== 0) {
          if (Date.now() - q.pageSince > 8000) {
            q.pageSince = Date.now(); saveQ(q);
            location.href = 'https://www.xiaohongshu.com/user/profile/' + (q.currentUid || '');
          }
          return;
        }
        // 昵称验证：主页必须真的显示作者昵称——防止登录后跳到自己主页、
        // 或搜索排序偏差抓错人（v3 就栽在这里）
        if (bodyText().indexOf(q.author) >= 0) {
          q.phase = 'harvest';
          q.harvestDeadline = Date.now() + 45000;
          q.pageSince = Date.now();
          saveQ(q);
          document.title = 'VidInsight 作者主页已确认，收割 token 中…';
          diag('harvest-entry');
          sig('phase', { msg: '作者主页已确认，正在收割笔记 token…' });
          return;
        }
        var next = null;
        for (var i = 0; i < q.candidates.length; i++) {
          if (q.tried.indexOf(q.candidates[i]) < 0) { next = q.candidates[i]; break; }
        }
        if (!next) {
          // 候选耗尽：若还没做过翻页/二次搜索，回搜索页滚出更多候选再试一轮
          // （search 阶段的有界滚动会接手）；已 researched 才判失败。
          if (!q.researched) {
            q.researched = true; q.scrollAttempts = 0; q.pageSince = Date.now();
            q.phase = 'search'; saveQ(q);
            diag('verify-exhausted-go-search', { author: q.author, tried: (q.tried || []).slice(0, 8) });
            document.title = 'VidInsight 回搜索页翻找更多候选…（请勿关闭）';
            location.href = 'https://www.xiaohongshu.com/search_result?keyword=' + encodeURIComponent(q.author) + '&type=54';
            return;
          }
          diag('verify-exhausted', { author: q.author,
            candidates: (q.candidates || []).slice(0, 8), tried: (q.tried || []).slice(0, 8) });
          sig('fail', { reason: '候选主页都不是作者「' + q.author + '」，无法定位作者主页' });
          return;
        }
        q.tried.push(next);
        q.currentUid = next; q.pageSince = Date.now();
        saveQ(q);
        location.href = 'https://www.xiaohongshu.com/user/profile/' + next;
        return;
      }

      if (q.phase === 'harvest') {
        if (path.indexOf('/user/profile/') !== 0) {
          if (Date.now() - q.pageSince > 8000) {
            q.pageSince = Date.now(); saveQ(q);
            location.href = 'https://www.xiaohongshu.com/user/profile/' + (q.currentUid || '');
          }
          return;
        }
        window.scrollTo(0, document.body.scrollHeight);
        document.querySelectorAll('a[href*="xsec_token="]').forEach(function(a){
          var h = a.getAttribute('href') || '';
          var abs = h.indexOf('http') === 0 ? h : 'https://www.xiaohongshu.com' + h;
          // 卡片 href 有四种形态：/explore/{id}、/discovery/item/{id}、
          // /search_result/{id}、/user/profile/{uid}/{id}（主页卡片——v4.1 诊断
          // 发现收割 0 token 的根因：主页卡片全是这种形态，被旧前缀白名单整批丢弃）。
          // 顶栏个人主页链接 /user/profile/{uid}?... 无第二段 id，两个正则都不会匹配。
          var m = abs.match(/(?:explore|discovery\/item|search_result)\/([0-9a-f]{24})/) ||
                  abs.match(/user\/profile\/[0-9a-f]{24}\/([0-9a-f]{24})/);
          if (m && q.ids.indexOf(m[1]) >= 0 && !q.found[m[1]]) q.found[m[1]] = abs;
        });
        var got = Object.keys(q.found).length;
        // href 扫完后合并 state 兜底（href 通道死时仍能收齐）
        var st = stateTokens(q.ids);
        for (var sk in st) if (!q.found[sk]) q.found[sk] = st[sk];
        got = Object.keys(q.found).length;
        if (got !== q.announced) { q.announced = got; sig('phase', { msg: '收割中 ' + got + '/' + q.ids.length }); }
        document.title = 'VidInsight 收割中 ' + got + '/' + q.ids.length + '（完成后窗口自动关闭）';
        // 硬 deadline：45s 内没找齐也收尾，绝不无限下滑（部分命中照样可用，
        // 缺失的笔记走登录态免 token / SSR 兜底）
        if (got >= q.ids.length || Date.now() > (q.harvestDeadline || 0)) {
          var links = Object.keys(q.found).map(function(k){ return q.found[k]; });
          collectDetails(links, 0, got >= q.ids.length ? 'done' : 'partial');
          return;
        }
        saveQ(q);
        return;
      }
    } catch(e) { sig('fail', { reason: '页面脚本异常: ' + e }); }
  }
  setInterval(tick, 1000);
})()
"#;
    // 被动捕获模板 v5：零自动化——无 location.href 跳转、无 scrollTo、
    // 无自动搜索/点击。窗口只当记录仪：用户像平常一样浏览并点开想分析的
    // 笔记，XHR/fetch 钩子 + 每次自然导航后的 __INITIAL_STATE__ 深扫，
    // 顺路记录 XHS 自己返回的笔记数据（noteDetailMap → /ingest_state，
    // 带 xsecToken 的笔记对象 → /ingest_tokens）。流量特征 100% 真人。
    // 用户关闭窗口 = 完成，已记录数据全部保留。
    const PASSIVE_JS_TEMPLATE: &str = r#"
(function(){
  var LABEL = '__LABEL__';
  var API = 'http://127.0.0.1:8483/api/xhs';
  function sig2(ev, data){
    try {
      var x = new XMLHttpRequest();
      x.open('POST', API + '/harvest_signal', true);
      x.setRequestHeader('Content-Type', 'text/plain');
      x.send(JSON.stringify({ label: LABEL, event: ev, data: data || null }));
    } catch(e) {}
  }
  window.addEventListener('error', function(e){
    sig2('fail', { reason: '页面脚本错误: ' + (e.message || 'unknown') + ' @' + (e.filename || '').slice(-40) + ':' + (e.lineno || 0) });
  });
  try { sig2('injected', { path: location.pathname.slice(0, 60), ready: document.readyState }); } catch(e) {}
  if (window.top !== window) return;
  var ID_RE = /^[0-9a-f]{24}$/;
  function sig(ev, data){
    try {
      fetch(API + '/harvest_signal', {
        method: 'POST', headers: { 'Content-Type': 'text/plain' },
        body: JSON.stringify({ label: LABEL, event: ev, data: data || null })
      }).catch(function(){});
    } catch(e) {}
  }
  function post(url, body, onAck){
    try {
      fetch(url, { method: 'POST', headers: { 'Content-Type': 'text/plain' },
        body: JSON.stringify(body) })
        .then(function(r){ if (r && r.ok && onAck) onAck(); })
        .catch(function(){});
    } catch(e) {}
  }
  function seenSet(kind){
    try { return new Set(JSON.parse(sessionStorage.getItem('__VIBSEEN_' + kind) || '[]')); }
    catch(e) { return new Set(); }
  }
  var seenTok = seenSet('tok'), seenNote = seenSet('note');
  // 收藏专辑（board）捕获：专辑页 boardFeedsMap.notes / 分页 homefeed API
  // 的条目是轻量元数据（noteId/标题/类型/可选 xsecToken），逐条 upsert 到
  // /ingest_board_notes ——「勾选工作台」的输入。重复条目后到覆盖先到。
  var boardId = (location.pathname.match(/\/board\/([0-9a-f]{24})/) || [])[1] || '';
  var seenBoard = seenSet('brd_' + boardId);
  function boardAnnounce(){ return seenBoard.size; }
  function ingestBoardEntry(n){
    if (!boardId) return;
    var nid = n.noteId || n.note_id || n.id;
    if (typeof nid !== 'string' || !ID_RE.test(nid)) return;
    // 详情节点（有 desc 正文）不进专辑清单——它该走 ingest_state 全量缓存
    if (typeof n.desc === 'string' && n.desc.length > 200) return;
    if (typeof n.title === 'string' && n.title.length > 0 &&
        typeof n.displayTitle === 'undefined' && typeof n.type === 'undefined') {
      // 既无标题也无类型的裸 id 对象（未知形态），跳过防脏数据
      if (!n.cover && !n.xsecToken) return;
    }
    // 2026-10-17 修复：原来「已见过直接 return」让旧「未命名」数据永远无法
    // 更新（sessionStorage 恢复的 seenBoard 挡住了全部旧 note_id）。
    // 改为：本次若带有标题或封面，允许覆盖更新（后端 upsert）。
    var _hadOld = seenBoard.has(nid);
    if (_hadOld && !n.title && !n.displayTitle && !n.cover &&
        !(n.noteCard && (n.noteCard.displayTitle || n.noteCard.cover))) return;
    seenBoard.add(nid);
    // 封面：卡片对象的 cover 是图片对象（url/urlDefault/infoList 多档），
    // 取最高档给工作台卡片流（前端经 image_proxy 带 xhscdn Referer 展示）
    var cov = '';
    try {
      // 小红书不同接口/版本的封面字段位置不同，逐层兜底
      var c = n.cover || n.noteCard && n.noteCard.cover || {};
      cov = c.url || c.urlDefault || c.urlPre || '';
      if (!cov && c.infoList && c.infoList.length) {
        cov = c.infoList[c.infoList.length - 1].url || '';
      }
      // 部分形态把封面放在 imageList / images 里
      if (!cov) {
        var il = n.imageList || n.images || (n.noteCard && (n.noteCard.imageList || n.noteCard.images)) || [];
        if (il && il.length) {
          var im = il[0] || {};
          cov = im.urlDefault || im.url || im.urlPre || '';
        }
      }
    } catch (e) { cov = ''; }
    // 标题同样多层兜底（displayTitle / title / noteCard.displayTitle / desc 首行）
    var ttl = n.title || n.displayTitle ||
              (n.noteCard && (n.noteCard.displayTitle || n.noteCard.title)) || '';
    if (!ttl && typeof n.desc === 'string') ttl = n.desc.split('\n')[0];
    if (!ttl) ttl = '未命名';
    post(API + '/ingest_board_notes', { board_id: boardId, notes: [{
      note_id: nid,
      title: String(ttl).slice(0, 200),
      type: n.type || (n.noteCard && n.noteCard.type) || '',
      cover: cov,
      xsec_token: n.xsecToken || n.xsec_token || (n.noteCard && n.noteCard.xsecToken) || '',
      xsec_source: n.xsecSource || n.xsec_source || ''
    }] });
  }
  // 深扫任意 JS/JSON 值：收 noteDetailMap 节点 + 带 xsecToken 的笔记对象。
  // 直接走活对象（不 stringify 往返），Map/Set 显式展开，避免 new Map() 丢数据。
  function scanValue(v){
    var scanned = 0;
    (function walk(o, depth){
      if (!o || typeof o !== 'object' || depth > 6 || scanned > 60000) return;
      scanned++;
      if (typeof Map !== 'undefined' && o instanceof Map) { o.forEach(function(mv){ walk(mv, depth + 1); }); return; }
      if (typeof Set !== 'undefined' && o instanceof Set) { o.forEach(function(sv){ walk(sv, depth + 1); }); return; }
      if (Array.isArray(o)) { for (var i = 0; i < o.length; i++) walk(o[i], depth + 1); return; }
      // 专辑页：boardFeedsMap.{bid}.notes[] 的条目直接进专辑清单
      if (boardId && o.noteId && (o.type === 'video' || o.type === 'normal' || o.cover)) {
        ingestBoardEntry(o);
      }
      var tok = o.xsecToken || o.xsec_token;
      if (typeof tok === 'string' && tok) {
        var nid = o.noteId || o.note_id || o.id;
        if (typeof nid === 'string' && ID_RE.test(nid) && !seenTok.has(nid)) {
          seenTok.add(nid);
          var src = o.xsecSource || o.xsec_source || 'pc_user';
          post(API + '/ingest_tokens', { links: [
            'https://www.xiaohongshu.com/explore/' + nid +
            '?xsec_token=' + encodeURIComponent(tok) + '&xsec_source=' + src ] });
        }
      }
      for (var k in o) {
        var val = o[k];
        if (k === 'noteDetailMap' && val && typeof val === 'object') {
          for (var id in val) {
            var node = (val[id] || {}).note;
            var nnid = node && typeof node === 'object' ? (node.noteId || id) : null;
            if (nnid && !seenNote.has(nnid)) {
              seenNote.add(nnid);
              post(API + '/ingest_state', { note_id: nnid, note: node });
            }
          }
        } else if (k === 'notes' && Array.isArray(val) && boardId && depth <= 4) {
          // boardFeedsMap.{bid}.notes / 分页 API 的 notes 数组
          val.forEach(function(entry){
            if (entry && typeof entry === 'object' && !Array.isArray(entry)) ingestBoardEntry(entry);
          });
        } else {
          walk(val, depth + 1);
        }
      }
    })(v, 0);
  }
  function flush(){
    try {
      sessionStorage.setItem('__VIBSEEN_tok', JSON.stringify(Array.from(seenTok).slice(-3000)));
      sessionStorage.setItem('__VIBSEEN_note', JSON.stringify(Array.from(seenNote).slice(-3000)));
      if (boardId) {
        sessionStorage.setItem('__VIBSEEN_brd_' + boardId,
          JSON.stringify(Array.from(seenBoard).slice(-3000)));
      }
    } catch(e) {}
  }
  // 用户每次自然导航产生的 SSR 状态（initialization_script 每文档重新注入）
  function scanState(){
    try { scanValue(window.__INITIAL_STATE__ || {}); } catch(e) {}
    flush();
  }
  // XHR/fetch 钩子：SPA 客户端渲染路径的数据同样顺路记录（只读响应，不改请求）
  function hookResponses(){
    // 2026-10-17 修正：原逻辑只处理"含特定关键词"的响应，但专辑列表 API
    // 的响应结构不一定含那些词，导致标题/封面全丢。改为：**所有 JSON 响应都扫**，
    // 由 scanValue 内部按字段形态判断（它有 noteId/notes 数组的识别逻辑）。
    function tryScan(txt){
      if (!txt || txt.length > 8 * 1024 * 1024) return;
      // 快速排除明显无关的（省 CPU）
      if (txt.indexOf('noteId') < 0 && txt.indexOf('note_id') < 0 &&
          txt.indexOf('"notes"') < 0 && txt.indexOf('xsecToken') < 0 &&
          txt.indexOf('boardFeedsMap') < 0 && txt.indexOf('displayTitle') < 0) return;
      // ── 字段诊断（2026-10-17：标题/封面全空，需要看真实响应结构）──
      // 只发一次样本：找到第一个含 noteId 的对象，把它的 key 列表发到后端日志。
      if (!window.__VIB_SCHEMA_SENT__) {
        try {
          var parsed = JSON.parse(txt);
          var found = null;
          (function find(o, d){
            if (found || !o || typeof o !== 'object' || d > 6) return;
            if (!Array.isArray(o) && (o.noteId || o.note_id)) { found = o; return; }
            for (var k in o) find(o[k], d + 1);
          })(parsed, 0);
          if (found) {
            window.__VIB_SCHEMA_SENT__ = true;
            var keys = Object.keys(found).slice(0, 40);
            var coverKeys = found.cover ? Object.keys(found.cover).slice(0, 12) : [];
            sig('diag', { step: 'api_schema', keys: keys.join(','),
                          coverKeys: coverKeys.join(','),
                          sampleTitle: String(found.title || found.displayTitle || '').slice(0, 60),
                          hasNoteCard: !!found.noteCard,
                          noteCardKeys: found.noteCard ? Object.keys(found.noteCard).slice(0, 20).join(',') : '' });
          }
        } catch(e) {}
      }
      try { scanValue(JSON.parse(txt)); flush(); } catch(e) {}
    }
    if (window.fetch && !window.__VIB_FH__) {
      window.__VIB_FH__ = true;
      var of_ = window.fetch;
      window.fetch = function(){
        return of_.apply(this, arguments).then(function(resp){
          try {
            var ct = resp.headers.get('content-type') || '';
            if (ct.indexOf('json') >= 0 && resp.clone) {
              resp.clone().text().then(tryScan).catch(function(){});
            }
          } catch(e) {}
          return resp;
        });
      };
    }
    if (window.XMLHttpRequest && !window.__VIB_XH__) {
      window.__VIB_XH__ = true;
      var oOpen = XMLHttpRequest.prototype.open, oSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(){
        this.__vibUrl = arguments[1];
        return oOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function(){
        var xhr = this;
        this.addEventListener('load', function(){
          try { tryScan(xhr.responseText); } catch(e) {}
        });
        return oSend.apply(this, arguments);
      };
    }
  }
  hookResponses();
  // ── DOM 兜底扫描（2026-10-13 实测补）──────────────────────
  // 为什么必须有它：专辑页 SSR 的 boardFeedsMap.notes 实测是**空数组**
  // （"notes":[]，hasMore:true），笔记卡片由页面 JS 异步渲染后才出现在 DOM。
  // 只扫 JS 对象会一条都读不到；而页面上真实渲染的卡片链接是可靠的——
  // 用户平时就是点这些链接进笔记的。这里只读 DOM，不做任何点击/导航。
  var _domBoardScanAt = 0;
  function scanBoardCards(){
    if (!boardId) return;
    // 节流：每秒 tick 都扫太浪费，2 秒一次足够
    var now = Date.now();
    if (now - _domBoardScanAt < 2000) return;
    _domBoardScanAt = now;
    var anchors;
    try {
      // 主选择器 + 兜底：小红书卡片结构多变（a 包 div / div 包 a / 相对路径），
      // 统一按"href 里含 24 位十六进制 note id"来认，覆盖面最广。
      anchors = document.querySelectorAll(
        'a[href*="/explore/"], a[href*="/discovery/item/"], a[href*="/search_result/"], a[href*="/note/"]');
      if (!anchors || anchors.length === 0) {
        // 二次兜底：扫描所有 a[href]，用正则筛
        anchors = document.querySelectorAll('a[href]');
      }
    } catch(e) { return; }
    for (var i = 0; i < anchors.length; i++) {
      var a = anchors[i];
      var href = a.getAttribute('href') || '';
      // 2026-10-17 修正：之前漏了 /note/ 形态，导致部分卡片被跳过
      var m = href.match(/\/(?:explore|discovery\/item|search_result|note)\/([0-9a-f]{24})/);
      if (!m) continue;
      var nid = m[1];
      // 2026-10-17 关键修复：原来「已见过直接跳过」导致旧会话捕获的
      // 「未命名」数据永远无法被更新（sessionStorage 恢复的 seenBoard 里
      // 全是旧 note_id，新提取逻辑根本没机会跑）。
      // 改为：没标题/封面的条目允许重新提取并覆盖（后端是 upsert）。
      // 只有「已有完整信息」的才跳过，且跳过前记录一次即可。
      // xsec_token 通常在 href 的 query 里（页面自己签好的），顺路带走
      var tok = '';
      try {
        var q = href.indexOf('?') >= 0 ? href.slice(href.indexOf('?') + 1) : '';
        var mm = q.match(/xsec_token=([^&]+)/);
        if (mm) tok = decodeURIComponent(mm[1]);
      } catch(e) {}
      // ── 标题/封面提取（2026-10-17 修复"全部未命名无封面"）──
      // 原实现用 a.closest('section,div') 会向上找到**整个卡片网格**，
      // 于是在网格里 querySelector 拿到的是第一张卡的标题/封面（甚至空），
      // 导致 241 条全是"未命名"、无封面。
      // 正确顺序：① 先只看 <a> 自身内部 ② 不够再向上最多 2 层 ③ 都取不到
      // 才留空（前端会显示占位图标，不误导）。
      var title = '', cover = '';
      function pickFrom(root){
        if (!root) return;
        if (!cover) {
          var img = root.querySelector && root.querySelector('img');
          if (img) {
            var src = img.currentSrc || img.src || '';
            // 小红书封面在 data-src / srcset 里也常见
            if (!src && img.getAttribute) src = img.getAttribute('data-src') || '';
            if (!src && img.getAttribute) {
              var ss = img.getAttribute('srcset') || '';
              var first = ss.split(',')[0] || '';
              src = first.trim().split(' ')[0] || '';
            }
            if (src && src.indexOf('data:') !== 0) cover = src;
          }
          // 2026-10-17 补：小红书部分卡片用 CSS background-image 而非 <img>。
          // 检查 root 自身与直接子元素的 style/backgroundImage。
          if (!cover && root.querySelectorAll) {
            var bgs = root.querySelectorAll('[style*="background-image"], [style*="background:"]');
            for (var bi = 0; bi < bgs.length && !cover; bi++) {
              var bs = (bgs[bi].getAttribute('style') || '');
              var bm = bs.match(/url\(["']?(https?:\/\/[^"')]+\.(?:jpg|jpeg|png|webp|avif)[^"')]*)["']?\)/i);
              if (bm) cover = bm[1];
            }
          }
        }
        if (!title) {
          var cands = root.querySelectorAll
            ? root.querySelectorAll('.title, [class*="title"], [class*="Title"], [class*="name"], span, div')
            : [];
          for (var ci = 0; ci < cands.length; ci++) {
            var txt = (cands[ci].textContent || '').trim();
            // 过滤明显不是标题的短标签（作者名/点赞数/时长等）
            if (txt.length >= 4 && txt.length <= 200 && !/^[\d.\s万+]+$/.test(txt)) {
              title = txt;
              break;
            }
          }
        }
      }
      pickFrom(a);                       // ① <a> 内部（最常见）
      if (!title || !cover) {            // ② 向上 2 层
        var up = a.parentElement;
        for (var lv = 0; lv < 2 && up; lv++) {
          pickFrom(up);
          if (title && cover) break;
          up = up.parentElement;
        }
      }
      // ③ 标题兜底：<a> 自身的 aria-label / title 属性
      if (!title) {
        title = (a.getAttribute('aria-label') || a.getAttribute('title') || '').trim();
      }
      // ── 卡片结构诊断（一次性）：把第一个卡片的真实 HTML 发回日志 ──
      // 目的：标题/封面提取反复失败，原因是猜测的 DOM 结构与实际不符。
      // 有了真实片段就能一步到位写对选择器。只发一次，不影响性能。
      if (!window.__VIB_CARD_DUMPED__) {
        window.__VIB_CARD_DUMPED__ = true;
        try {
          var dump = (a.outerHTML || '').slice(0, 1200);
          var upEl = a.parentElement;
          if (upEl) dump += '\n---PARENT---\n' + (upEl.outerHTML || '').slice(0, 1200);
          sig('diag', { step: 'card_dom', html: dump });
        } catch(e) {}
      }
      // 去重与更新策略（2026-10-17 修复）：
      // - 已记录且这次没拿到新信息 → 跳过（不重复 post）
      // - 已记录但这次**拿到了标题/封面** → 重新 post 覆盖旧数据（后端 upsert）
      // - 没记录过 → post
      var had = seenBoard.has(nid);
      if (had && !title && !cover) continue;   // 旧的未命名条目且这次也没拿到 → 跳过
      seenBoard.add(nid);
      post(API + '/ingest_board_notes', { board_id: boardId, notes: [{
        note_id: nid,
        title: title || '未命名',
        type: '',
        cover: cover,
        xsec_token: tok,
        xsec_source: 'pc_board_dom'
      }] });
    }
  }
  var lastAnnounced = -1, wallSince = 0, loginWallOn = false, captchaOn = false, failed = false;
  function bodyText(){ return document.body ? document.body.innerText : ''; }
  // 登录提示横幅（2026-10-17 实测：专辑"笔记·272"但列表空 = 未登录，
  // 小红书只对登录态返回列表。用户不知道要登录会以为产品坏了，这里主动提示。）
  var loginTipShown = false;
  function mountLoginTip(){
    if (loginTipShown || !document.body) return;
    var emptyList = bodyText().indexOf('该专辑还没收藏任何笔记') >= 0;
    var hasLoginBtn = !!document.querySelector('#login-btn, .login-btn, [class*="login-btn"]');
    var hasLoginText = bodyText().indexOf('登录') >= 0;
    if (!emptyList || !hasLoginText) return;
    loginTipShown = true;
    var bar = document.createElement('div');
    bar.id = '__vib_login_tip__';
    bar.setAttribute('style',
      'position:fixed!important;left:0!important;right:0!important;top:0!important;z-index:2147483646!important;' +
      'background:#fff3cd!important;border-bottom:1px solid #ffe08a!important;color:#664d03!important;' +
      'font:13px/1.6 "Microsoft YaHei",system-ui!important;padding:10px 16px!important;text-align:center!important;');
    bar.innerHTML = '<b>需要先登录才能读取笔记列表</b>　' +
      '小红书只对登录用户返回专辑里的笔记（页面上方显示的数量是公开的，但列表不是）。' +
      '请点击页面左侧的 <b>「登录」</b> 按钮完成登录，列表出现后正常翻一遍即可。' +
      '<button id="__vib_tip_close__" style="margin-left:12px;padding:2px 10px;border:1px solid #d3c07a;' +
      'border-radius:4px;background:#fff;cursor:pointer;font-size:12px">知道了</button>';
    document.body.appendChild(bar);
    try {
      document.getElementById('__vib_tip_close__').addEventListener('click', function(){ bar.remove(); });
    } catch(e) {}
  }
  function tick(){
    try {
      var t = bodyText();
      var cap = !!(document.querySelector('[class*="captcha"]') || t.indexOf('安全验证') >= 0);
      if (cap && !captchaOn) { captchaOn = true; sig('captcha'); }
      if (!cap && captchaOn) captchaOn = false;
      var wall = t.indexOf('登录后查看') >= 0 || t.indexOf('扫码登录') >= 0 || t.indexOf('登录超限') >= 0;
      if (wall && !loginWallOn) { loginWallOn = true; sig('login'); }
      if (!wall && loginWallOn) loginWallOn = false;
      mountLoginTip();
      // DOM 兜底：专辑页笔记卡片是 JS 渲染的，SSR 里读不到（实测 notes 为空数组）
      scanBoardCards();
      // 进度播报（有变化才发，避免刷屏）
      var totalSeen = seenNote.size + boardAnnounce();
      if (totalSeen !== lastAnnounced) {
        lastAnnounced = totalSeen;
        sig('phase', { msg: boardId
          ? '专辑已顺路记录 ' + boardAnnounce() + ' 条笔记'
          : '已顺路记录 ' + seenNote.size + ' 条笔记' });
      }
      // 30s 毫无收获且无登录墙/验证码，且页面连 SSR 状态/卡片链接都没有
      // → 页面根本没加载出来，提示用户（用户正常浏览合集页时绝不误报）
      if (!failed && seenNote.size === 0 && seenTok.size === 0 && !wall && !cap) {
        if (!wallSince) wallSince = Date.now();
        if (Date.now() - wallSince > 30000) {
          wallSince = Date.now();
          var hasState = !!window.__INITIAL_STATE__;
          var hasCards = !!document.querySelector(
            'a[href*="/explore/"], a[href*="/discovery/item/"], a[href*="/search_result/"]');
          if (!hasState && !hasCards) {
            failed = true;
            // 诊断现场：正文长度/标题/UA/readyState——「窗口空白 0 记录」的定位证据
            sig('fail', { reason: '窗口打开 30 秒仍未加载出小红书页面。若持续空白请点 ↻ 刷新，或在窗口内重新登录后重试。',
              diag: JSON.stringify({ path: location.pathname, ready: document.readyState,
                bodyLen: document.body ? document.body.innerText.length : -1,
                title: (document.title || '').slice(0, 60), ua: navigator.userAgent.slice(0, 80) }) });
          }
        }
      } else {
        wallSince = 0;
      }
    } catch(e) {}
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scanState);
  } else { scanState(); }
  // ── 导航控件：被动模式铁律是不自动动，但用户要能自由返回 ──
  // 悬浮在页面右上角：← 返回 / → 前进 / ↻ 刷新 / 🏠 回合集。全部由
  // 用户点击触发，工具本身不做任何导航。SPA 内部跳转也会被轮询感知。
  var navReady = false;
  function mountNav(){
    if (navReady || !document.body) return;
    navReady = true;
    var bar = document.createElement('div');
    bar.id = '__vib_nav__';
    bar.setAttribute('style',
      'position:fixed!important;top:8px!important;right:8px!important;z-index:2147483647!important;' +
      'display:flex!important;gap:6px!important;background:rgba(20,20,20,.88)!important;' +
      'border-radius:10px!important;padding:6px 8px!important;box-shadow:0 4px 16px rgba(0,0,0,.4)!important;');
    function btn(txt, title, fn){
      var b = document.createElement('button');
      b.textContent = txt; b.title = title;
      b.setAttribute('style',
        'width:34px;height:34px;border:none;border-radius:8px;background:#fff;color:#111;' +
        'font-size:16px;cursor:pointer;line-height:1;');
      b.addEventListener('click', function(e){ e.stopPropagation(); fn(); });
      return b;
    }
    bar.appendChild(btn('←', '返回上一页', function(){ history.back(); }));
    bar.appendChild(btn('→', '前进', function(){ history.forward(); }));
    bar.appendChild(btn('↻', '刷新当前页', function(){ location.reload(); }));
    var homeUrl = (sessionStorage.getItem('__VIB_HOME__') || '').trim();
    if (homeUrl.indexOf('xiaohongshu.com') === 0 || homeUrl.indexOf('http') === 0) {
      bar.appendChild(btn('🏠', '回到起始页（合集）', function(){ location.href = homeUrl; }));
    }
    document.body.appendChild(bar);
  }
  function homeTick(){
    try {
      if (!sessionStorage.getItem('__VIB_HOME__') &&
          location.host.indexOf('xiaohongshu.com') >= 0) {
        sessionStorage.setItem('__VIB_HOME__', location.href);
      }
      mountNav();
    } catch(e) {}
  }
  setInterval(homeTick, 800);
  setInterval(tick, 1000);
})()
"#;
    let harvest_js = if passive {
        PASSIVE_JS_TEMPLATE.replace("__LABEL__", &label)
    } else {
        HARVEST_JS_TEMPLATE.replace("__LABEL__", &label)
    };

    let mut builder = WebviewWindowBuilder::new(
        &app,
        label.clone(),
        WebviewUrl::External(parsed),
    )
    .additional_browser_args(WEBVIEW_ARGS)
    .initialization_script(&harvest_js)
    // WebView2 默认 UA 带「Edg/」且头信息与真 Chrome 略异——XHS 曾对降级 UA
    // 返回空白壳页（11:36 空窗口实况）。覆盖成与手动 Edge 浏览完全一致的 UA。
    .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0");
    if passive {
        // 被动模式：用户要亲手浏览，窗口必须够大（接近平板体验）、抢焦点、标题讲清玩法
        builder = builder
            .title("VidInsight 小红书浏览窗口：像平常一样浏览并点开想分析的笔记（右上角有 ←返回/↻刷新），完成后直接关闭本窗口")
            .inner_size(1100.0, 800.0)
            .position(80.0, 40.0)
            .focused(true);
    } else {
        // 用户点名体验：过程窗口默认缩成屏幕角落小窗（不挡视线）；隐藏/最小化会被
        // WebView 定时器节流（实测 hydration 25s+），小窗可见是唯一安全解。
        // 出现登录墙/验证码时放大回 480x800 并抢焦点，方便扫码；VIB_DEBUG_HARVEST=1
        // 时直接大窗（测试观察用）。
        builder = builder
            .title("VidInsight 正在读取合集（如需登录/验证会在窗口内提示；完成后自动关闭）")
            .inner_size(300.0, 200.0)
            .position(1560.0, 900.0)
            .focused(false);
    }
    let win = builder
        .visible(true)
        .build()
        .map_err(|e| format!("打开抓取窗口失败: {e}"))?;

    let debug_harvest = !passive && std::env::var("VIB_DEBUG_HARVEST").ok().as_deref() == Some("1");
    if debug_harvest {
        let _ = win.set_size(tauri::LogicalSize::new(480.0f64, 800.0f64));
        let _ = win.set_position(tauri::LogicalPosition::new(140.0f64, 80.0f64));
        let _ = win.set_focus();
    }

    // 2026-10-19 登录态加固：合集/主页窗口原来只在「收割成功」后才回写 cookie，
    // 用户中途关窗或超时退出时新登录态直接丢掉（下次还得重新登）。
    // 现在关窗时读一次 cookie（窗口销毁前）再上报。
    // 注意：cookies_for_url 内部是「向事件循环投消息 + 阻塞等回包」，在事件
    // 回调线程里直接调用会自锁——整个读取+上报必须挪到 spawn_blocking；
    // 且窗口可能已销毁，按 label 重新取活窗口（与 board 窗口同款做法）。
    {
        let close_app = app.clone();
        let close_label = label.clone();
        let close_cookie_url = cookie_url.clone();
        win.on_window_event(move |event| {
            if !matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                return;
            }
            let a = close_app.clone();
            let l = close_label.clone();
            let u = close_cookie_url.clone();
            tauri::async_runtime::spawn_blocking(move || {
                std::thread::sleep(Duration::from_millis(300));
                if let Some(w) = a.get_webview_window(&l) {
                    if let Some(cookie) = collect_xhs_cookie(&w, &u) {
                        post_xhs_cookie(cookie);
                    }
                }
            });
        });
    }

    let win_ref = win.clone();
    let app_ref = app.clone();
    let label_ref = label.clone();
    let res = tauri::async_runtime::spawn_blocking(move || {
        // 被动模式看门狗 20 分钟（用户在看笔记，不催）；自动收割 6 分钟。
        // 信号通道 = 后端 /xhs/harvest_signal（JS POST、Rust GET 轮询）
        let mut deadline = Instant::now() + Duration::from_secs(if passive { 1200 } else { 360 });
        let mut login_extended = false;
        let mut captcha_extended = false;
        let signal_url = format!("http://127.0.0.1:8483/api/xhs/harvest_signal?label={label}");
        while Instant::now() < deadline {
            if app_ref.get_webview_window(&label_ref).is_none() {
                // 被动模式：用户关闭窗口 = 浏览完成，已顺路记录的数据全部有效
                return if passive {
                    Ok(String::new())
                } else {
                    Err("已取消：抓取窗口被关闭".to_string())
                };
            }
            match ureq::get(&signal_url).timeout(Duration::from_secs(2)).call() {
                Ok(resp) => {
                    if let Ok(body) = resp.into_string() {
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&body) {
                            let event = v["data"]["event"].as_str().unwrap_or("").to_string();
                            // 进度透传给前端对话区气泡（v4.1）：phase 带 msg，fail 带 reason
                            let _ = app_ref.emit(
                                "xhs-harvest-phase",
                                serde_json::json!({
                                    "event": event,
                                    "msg": v["data"]["data"]["msg"].as_str().unwrap_or(""),
                                    "reason": v["data"]["data"]["reason"].as_str().unwrap_or(""),
                                    "diag": v["data"]["data"]["diag"].as_str().unwrap_or(""),
                                }),
                            );
                            match event.as_str() {
                                "done" | "partial" => return Ok(String::new()),
                                "data" => {
                                    // ingest_tokens 直投失败：把链接交回前端补投
                                    let links = v["data"]["data"]["links"].clone();
                                    return Ok(serde_json::json!({ "links": links }).to_string());
                                }
                                "none" => {
                                    return Err(
                                        "未能收割到合集笔记的 xsec_token（作者主页确认失败或触发风控）。\
                                         请稍后重试，或把每条笔记从主页/搜索页复制的带 xsec_token 链接直接发给 VidInsight。"
                                            .to_string(),
                                    )
                                }
                                "fail" => {
                                    let reason = v["data"]["data"]["reason"]
                                        .as_str()
                                        .unwrap_or("未知原因")
                                        .to_string();
                                    return Err(reason);
                                }
                                "login" => {
                                    if !login_extended {
                                        login_extended = true;
                                        deadline = Instant::now() + Duration::from_secs(300);
                                    }
                                    // 需要用户交互：自动模式放大回全尺寸并抢焦点，方便扫码/拖滑块；
                                    // 被动模式窗口本就是全尺寸，只抢焦点 + 改标题
                                    if !passive {
                                        let _ = win_ref.set_size(tauri::LogicalSize::new(480.0f64, 800.0f64));
                                        let _ = win_ref.set_position(tauri::LogicalPosition::new(140.0f64, 80.0f64));
                                    }
                                    let _ = win_ref.set_focus();
                                    let _ = win_ref.set_title(
                                        "⚠️ 请在本窗口内登录小红书（扫码或手机号），登录后继续浏览想分析的笔记",
                                    );
                                }
                                "captcha" => {
                                    if !captcha_extended {
                                        captcha_extended = true;
                                        deadline = Instant::now() + Duration::from_secs(180);
                                    }
                                    if !passive {
                                        let _ = win_ref.set_size(tauri::LogicalSize::new(480.0f64, 800.0f64));
                                        let _ = win_ref.set_position(tauri::LogicalPosition::new(140.0f64, 80.0f64));
                                    }
                                    let _ = win_ref.set_focus();
                                    let _ = win_ref.set_title("请完成安全验证（拖动滑块），完成后继续浏览想分析的笔记");
                                }
                                "phase" => {
                                    if let Some(msg) = v["data"]["data"]["msg"].as_str() {
                                        let suffix = if passive {
                                            " · 浏览完直接关闭窗口"
                                        } else {
                                            "（完成后窗口自动关闭）"
                                        };
                                        let _ = win_ref.set_title(&format!("VidInsight {msg}{suffix}"));
                                    }
                                }
                                _ => {}
                            }
                        }
                    }
                }
                Err(_) => { /* 后端瞬时不可达：下一拍再试 */ }
            }
            std::thread::sleep(Duration::from_millis(700));
        }
        if passive {
            Err("浏览窗口已超时（20 分钟）：已顺路记录的笔记不受影响，可重新发起补齐剩余部分".to_string())
        } else if captcha_extended {
            Err("小红书弹出了安全验证且未完成：请在窗口里拖动滑块完成验证后重试".to_string())
        } else if login_extended {
            Err("登录未完成：请在窗口内的登录墙里完成小红书登录（扫码或手机号），登录后重试".to_string())
        } else {
            Err(
                "读取合集超时：小红书风控较敏感，请等几分钟后重试；已收割到的笔记 token 已保留，重试只需补齐剩余部分"
                    .to_string(),
            )
        }
    })
    .await
    .map_err(|e| format!("抓取任务失败: {e}"))?;

    // 登录态冗余：收割成功后把 webview 的 XHS cookie 回写后端（→downloader.json），
    // 让 curl 通道与 webview 会话同步，避免存量 cookie 过期毒化详情请求
    if res.is_ok() {
        match win.cookies_for_url(cookie_url) {
            Ok(cookies) => {
                let pairs: Vec<String> = cookies
                    .iter()
                    .map(|c| format!("{}={}", c.name(), c.value()))
                    .collect();
                if pairs.iter().any(|p| p.starts_with("web_session=")) {
                    let body = serde_json::json!({
                        "platform": "xiaohongshu",
                        "cookie": pairs.join("; "),
                    });
                    let _ = ureq::post("http://127.0.0.1:8483/api/xhs/ingest_cookies")
                        .timeout(Duration::from_secs(5))
                        .send_string(&body.to_string());
                }
            }
            Err(_) => { /* cookie 读取失败不影响主流程 */ }
        }
    }

    let _ = win.close();
    res
}

// 全局停止（2026-09-21 用户实测「停止键停不下浏览窗口」）：前端停止键除了
// 取消任务，还要能关掉 XHS 浏览窗口。被动模式见窗口关闭即返回 Ok，挂起的
// fetch_xhs_collection_detail invoke 立即解除，前端继续收尾。关闭所有
// xhs-collect-* 前缀的窗口，不碰主窗口。
#[tauri::command]
fn close_xhs_windows(app: tauri::AppHandle) -> Result<(), String> {
    let mut closed = 0;
    for (_, win) in app.webview_windows() {
        if win.label().starts_with("xhs-collect-") {
            let _ = win.close();
            closed += 1;
        }
    }
    Ok(())
}

/// 查询捕获窗口是否还开着（前端轮询用：关窗即视为捕获完成，自动刷新清单）
#[tauri::command]
fn xhs_capture_window_open(app: tauri::AppHandle) -> bool {
    app.webview_windows()
        .keys()
        .any(|label| label.starts_with("xhs-collect-"))
}

// 收藏专辑被动捕获窗口（独立页面调用）：打开 board 页，用户自己翻自己的
// 收藏，页面 JS 顺路记录条目到 /ingest_board_notes。无收割状态机——
// 用户关闭窗口即完成，前端随即拉 /xhs/board/{id}/notes 刷新勾选列表。
// 与 fetch_xhs_collection_detail 的区别：不等待、不轮询、开了就返回。
// 2026-10-18：收藏专辑窗口关闭前把 WebView 的小红书登录态回写后端
// （POST /xhs/ingest_cookies → downloader.json）。旧收割流程一直有这步，
// board 流程没有——登录态只活在 WebView 里，HTTP SSR 通道匿名拿壳页
// （400101 "未找到该笔记数据"），"生成所选"从未真正端到端通过。
// 两条关窗路径（自动关窗 / 用户点 ×）都必须在 destroy() 之前调用。
/// 从 WebView 读当前小红书 Cookie 串；未登录（无 web_session）返回 None。
/// 2026-10-19 拆出来供两条关窗路径共用（board 窗口的延迟销毁 / 合集窗口的
/// CloseRequested）。注意：内部会阻塞等事件循环回包，**不能在事件回调线程直接调**，
/// 必须放在 spawn_blocking 里（且要在窗口仍存活时调用）。
fn collect_xhs_cookie(win: &tauri::WebviewWindow, cookie_url: &tauri::Url) -> Option<String> {
    let cookies = win.cookies_for_url(cookie_url.to_owned()).ok()?;
    let pairs: Vec<String> = cookies
        .iter()
        .map(|c| format!("{}={}", c.name(), c.value()))
        .collect();
    // 没有 web_session 说明登录态不在（匿名/已登出），不覆盖存量 cookie
    if !pairs.iter().any(|p| p.starts_with("web_session=")) {
        return None;
    }
    Some(pairs.join("; "))
}

fn post_xhs_cookie(cookie: String) {
    let body = serde_json::json!({
        "platform": "xiaohongshu",
        "cookie": cookie,
    });
    let _ = ureq::post("http://127.0.0.1:8483/api/xhs/ingest_cookies")
        .timeout(Duration::from_secs(5))
        .send_string(&body.to_string());
}

fn export_xhs_cookies(win: &tauri::WebviewWindow, cookie_url: &tauri::Url) {
    if let Some(cookie) = collect_xhs_cookie(win, cookie_url) {
        post_xhs_cookie(cookie);
    }
}

#[tauri::command]
async fn open_xhs_passive_window(app: tauri::AppHandle, url: String) -> Result<String, String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};
    if !url.contains("xiaohongshu.com") {
        return Err("仅支持小红书链接".to_string());
    }
    let label = format!(
        "xhs-collect-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    // 2026-10-17 白屏排查：用户贴的是 App 分享链接，带一长串追踪参数
    // （xhsshare=&appuid=…&apptime=…&share_id=…&share_channel=…），
    // 而且复制到输入框时可能被截断（截图里末尾只剩 "channel=t"）。
    // 这类脏 URL 在 WebView 里打开可能得到空白页。
    // 统一剥成干净地址：只保留 scheme://host/path（board/{id}）。
    let clean_url = {
        let mut u = url.clone();
        if let Some(q) = u.find('?') {
            u.truncate(q);
        }
        if let Some(h) = u.find('#') {
            u.truncate(h);
        }
        u
    };
    let _ = ureq::post("http://127.0.0.1:8483/api/xhs/harvest_signal")
        .timeout(Duration::from_secs(2))
        .send_string(&serde_json::json!({
            "label": format!("{}-open", label),
            "event": "diag",
            "data": { "raw_url": url.chars().take(200).collect::<String>(),
                      "clean_url": clean_url.chars().take(200).collect::<String>() }
        }).to_string());
    let parsed: tauri::Url = clean_url
        .parse()
        .map_err(|e| format!("无效地址: {e}"))?;
    const BOARD_JS_TEMPLATE: &str = r#"
(function(){
  var LABEL = '__LABEL__';
  var API = 'http://127.0.0.1:8483/api/xhs';
  function sig2(ev, data){
    try {
      var x = new XMLHttpRequest();
      x.open('POST', API + '/harvest_signal', true);
      x.setRequestHeader('Content-Type', 'text/plain');
      x.send(JSON.stringify({ label: LABEL, event: ev, data: data || null }));
    } catch(e) {}
  }
  window.addEventListener('error', function(e){
    sig2('fail', { reason: '页面脚本错误: ' + (e.message || 'unknown') + ' @' + (e.filename || '').slice(-40) + ':' + (e.lineno || 0) });
  });
  try { sig2('injected', { path: location.pathname.slice(0, 60), ready: document.readyState }); } catch(e) {}
  if (window.top !== window) return;
  var ID_RE = /^[0-9a-f]{24}$/;
  function sig(ev, data){
    try {
      fetch(API + '/harvest_signal', {
        method: 'POST', headers: { 'Content-Type': 'text/plain' },
        body: JSON.stringify({ label: LABEL, event: ev, data: data || null })
      }).catch(function(){});
    } catch(e) {}
  }
  function post(path, body){
    try {
      fetch(API + path, { method: 'POST', headers: { 'Content-Type': 'text/plain' },
        body: JSON.stringify(body) }).catch(function(){});
    } catch(e) {}
  }
  var boardId = (location.pathname.match(/\/board\/([0-9a-f]{24})/) || [])[1] || '';
  var seen = {};
  // DOM 通道见过的笔记 ID（与 seen 分开：seen 只由 state/API 通道写入）。
  // 2026-10-19：虚拟列表滚到底会把卡片从 DOM 移除，瞬时 DOM 卡片数可能是 0，
  // 判断"到底有没有捕获到东西"必须用累计去重数，不能用瞬时数。
  var domSeen = {};
  // 2026-10-20：后端确认入库的笔记 ID（POST 拿到 2xx 才记）——capturedTotal
  // 的唯一数据源。fire-and-forget 会静默丢（实测丢 91/244），不能拿"发过"
  // 当"存了"。
  var ingested = {};
  function ingestEntry(n){
    if (!boardId) return;
    var nid = n.noteId || n.note_id || n.id;
    if (typeof nid !== 'string' || !ID_RE.test(nid) || seen[nid]) return;
    if (typeof n.desc === 'string' && n.desc.length > 200) return;  // 详情节点走 ingest_state
    if (!n.title && !n.displayTitle && !n.cover && !n.xsecToken) return;
    seen[nid] = 1;
    post('/ingest_board_notes', { board_id: boardId, notes: [{
      note_id: nid,
      title: n.title || n.displayTitle || '未命名',
      type: n.type || '',
      xsec_token: n.xsecToken || n.xsec_token || '',
      xsec_source: n.xsecSource || n.xsec_source || ''
    }] }, function(){ ingested[nid] = 1; });
  }
  function scanValue(v){
    var scanned = 0;
    (function walk(o, depth){
      if (!o || typeof o !== 'object' || depth > 6 || scanned > 60000) return;
      scanned++;
      if (typeof Map !== 'undefined' && o instanceof Map) { o.forEach(function(mv){ walk(mv, depth + 1); }); return; }
      if (typeof Set !== 'undefined' && o instanceof Set) { o.forEach(function(sv){ walk(sv, depth + 1); }); return; }
      if (Array.isArray(o)) { for (var i = 0; i < o.length; i++) walk(o[i], depth + 1); return; }
      if (o.noteId && (o.type === 'video' || o.type === 'normal' || o.cover)) ingestEntry(o);
      for (var k in o) {
        var val = o[k];
        if (k === 'noteDetailMap' && val && typeof val === 'object') {
          for (var id in val) {
            var node = (val[id] || {}).note;
            if (node && typeof node === 'object') {
              var nnid = node.noteId || id;
              if (typeof nnid === 'string' && ID_RE.test(nnid)) {
                post('/ingest_state', { note_id: nnid, note: node });
              }
            }
          }
        } else if (k === 'notes' && Array.isArray(val) && depth <= 4) {
          val.forEach(function(e2){ if (e2 && typeof e2 === 'object' && !Array.isArray(e2)) ingestEntry(e2); });
        } else {
          walk(val, depth + 1);
        }
      }
    })(v, 0);
  }
  // XHR/fetch 钩子：翻页/滚动加载的响应顺路记录
  if (window.fetch && !window.__VIB_FH__) {
    window.__VIB_FH__ = true;
    var of_ = window.fetch;
    window.fetch = function(){
      return of_.apply(this, arguments).then(function(resp){
        try {
          var ct = resp.headers.get('content-type') || '';
          if (ct.indexOf('json') >= 0 && resp.clone) {
            resp.clone().text().then(function(txt){
              if (txt && (txt.indexOf('"notes":[') >= 0 || txt.indexOf('noteDetailMap') >= 0 || txt.indexOf('xsecToken') >= 0)) {
                try { scanValue(JSON.parse(txt)); } catch(e) {}
              }
            }).catch(function(){});
          }
        } catch(e) {}
        return resp;
      });
    };
  }
  if (window.XMLHttpRequest && !window.__VIB_XH__) {
    window.__VIB_XH__ = true;
    var oOpen = XMLHttpRequest.prototype.open, oSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(){ this.__vibUrl = arguments[1]; return oOpen.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function(){
      var xhr = this;
      this.addEventListener('load', function(){
        try {
          var txt = xhr.responseText;
          if (txt && (txt.indexOf('"notes":[') >= 0 || txt.indexOf('noteDetailMap') >= 0 || txt.indexOf('xsecToken') >= 0)) {
            scanValue(JSON.parse(txt));
          }
        } catch(e) {}
      });
      return oSend.apply(this, arguments);
    };
  }
  function scanState(){ try { scanValue(window.__INITIAL_STATE__ || {}); } catch(e) {} }
  // 2026-10-17 修复"自动滚动不启动"：自动启动逻辑调用 bodyText() 但本模板
  // 没定义这个函数（它在另一个模板里）——ReferenceError 使启动静默失败。
  function bodyText(){ return document.body ? document.body.innerText : ''; }
  // 当前是否仍在收藏专辑页（登录后 XHS 常跳到首页/个人主页，此时绝不能自动关窗）
  function onBoardPage(){
    try { return /\/board\/[0-9a-f]{24}/.test(location.pathname || ''); } catch(e) { return false; }
  }
  // ── 登录态守卫（2026-10-19 用户实测事故）──────────────────────────
  // 事故：登录态失效后点「继续捕获更多」→ 专辑页一条笔记都读不到 →
  // 自动遍历把"没有新卡片"判成"到底"→ 发 auto_scroll_done → Rust 关窗，
  // 正好打断用户正在输入的账号密码。
  // 判定只读页面特征，**绝不自动登录 / 自动填表 / 自动导航**（产品红线）。
  // 特征依据（本文件既有实证）：
  //   · 登录墙文案 '登录后查看' / '扫码登录' / '登录超限'（见 PASSIVE 模板 line 354）
  //   · 可见的短文案「登录」按钮（见 line 384-386）
  //   · 专辑页空列表 = 未登录（见 line 1050 注释）
  var LOGIN_LOST = false;
  var loginLostHits = 0, loginOkHits = 0;
  var loginResumeTries = 0;
  var LOGIN_RESUME_MAX = 3;        // 登录恢复后最多自动续跑 3 次，防死循环
  var closeBlocked = false;        // 本页生命周期内禁止自动关窗
  var scrollPausedByLogin = false;
  var pendingResumeAt = 0;
  var autoScrolling = false;       // 提到外层：登录守卫需要暂停遍历
  var autoRunSeq = 0;              // 自动滚动轮次号：作废上一轮遗留的恢复/等待定时器
  var autoBtnRef = null, autoScrollFn = null;
  var navMountedAt = 0;            // 导航条挂载时刻（保留原「先给页面 4 秒」的语义）
  var loginBanner = null;
  var stuckBanner = null;          // 自动滚动卡住提示条（与登录横幅分开，底部显示）
  var _lastLoginSigAt = 0;
  function pageCardCount(){
    try {
      return document.querySelectorAll(
        'a[href*="/board/"], a[href*="/explore/"], a[href*="/discovery/item/"], ' +
        'a[href*="/search_result/"], a[href*="/note/"]').length;
    } catch(e) { return 0; }
  }
  // 本次遍历累计捕获到的笔记数（state/API 通道 + DOM 通道去重并集）
  function capturedTotal(){
    // 2026-10-20 计数真实化：只数「后端已确认入库」的（ingested）。
    // 此前数 seen∪domSeen——fire-and-forget 的 POST 丢一批（实测 244 发只落
    // 142，91 条 fetch 静默丢失），页面却报 233，用户被虚高数字误导。
    var n = 0, k;
    for (k in ingested) n++;
    return n;
  }
  // 可见的「登录」入口（隐藏的登录弹层模板不算）
  function visibleLoginEntry(){
    try {
      var cands = document.querySelectorAll('#login-btn, .login-btn, [class*="login-btn"], [class*="loginBtn"]');
      for (var i = 0; i < cands.length; i++) {
        var el = cands[i];
        if (el.offsetParent === null && el.getClientRects().length === 0) continue;
        var tx = (el.textContent || '').trim();
        if (tx && tx.length <= 6 && tx.indexOf('登录') >= 0) return true;
      }
    } catch(e) {}
    return false;
  }
  // 页面是否呈现"已登录"的正面证据（顶栏头像/用户信息）
  function loggedInMarker(){
    try {
      return !!document.querySelector(
        '[class*="avatar"], [class*="user-info"], [class*="userInfo"], [class*="side-bar"] [class*="avatar"]');
    } catch(e) { return false; }
  }
  // SPA 状态里的登录标记（PASSIVE 模板 line 380 已实证该字段存在）
  function stateLoggedIn(){
    try {
      var s = JSON.stringify(window.__INITIAL_STATE__ || {});
      if (!s) return null;
      var hasTrue = s.indexOf('"loggedIn":true') >= 0;
      var hasFalse = s.indexOf('"loggedIn":false') >= 0;
      if (hasTrue) return true;
      if (hasFalse) return false;
      return null;
    } catch(e) { return null; }
  }
  function detectLoginLost(){
    try {
      // ① 明确的登录墙文案（PASSIVE 模板既有判据）
      var t = bodyText();
      if (t.indexOf('登录后查看') >= 0 || t.indexOf('扫码登录') >= 0 ||
          t.indexOf('登录超限') >= 0 || t.indexOf('登录已过期') >= 0 ||
          t.indexOf('请先登录') >= 0 || t.indexOf('新用户可直接登录') >= 0 ||
          t.indexOf('输入手机号') >= 0) return true;
      // ② 页面上出现可见的「登录」入口 = 当前是匿名态
      if (visibleLoginEntry()) return true;
      // ③ 专辑页空列表 + 无任何已登录标记 + 状态明确 loggedIn:false
      //    （空专辑也 0 卡片，所以必须有 loggedIn:false 或登录入口才判失效，
      //     避免把「专辑本来就是空的」误报成登录失效）
      if (onBoardPage() && pageCardCount() === 0 && !loggedInMarker() &&
          stateLoggedIn() === false) return true;
    } catch(e) {}
    return false;
  }
  function showLoginBanner(msg){
    if (!document.body) return;
    if (!loginBanner) {
      loginBanner = document.createElement('div');
      loginBanner.id = '__vib_login_lost__';
      loginBanner.setAttribute('style',
        'position:fixed!important;left:0!important;right:0!important;top:0!important;z-index:2147483646!important;' +
        'background:#fff4e5!important;border-bottom:1px solid #f0c98a!important;color:#8a4b08!important;' +
        'font:13px/1.6 "Microsoft YaHei",system-ui!important;padding:9px 170px 9px 16px!important;' +
        'text-align:center!important;box-shadow:0 2px 8px rgba(0,0,0,.12)!important;');
    }
    loginBanner.textContent = msg;
    if (!loginBanner.parentNode) document.body.appendChild(loginBanner);
  }
  function hideLoginBanner(){
    try { if (loginBanner && loginBanner.parentNode) loginBanner.parentNode.removeChild(loginBanner); } catch(e) {}
  }
  // ── 自动滚动卡住提示条（2026-10-19）──────────────────────────────
  // 懒加载停摆 / 虚拟列表推不动时，视口会卡在中间但高度与数量都已稳定。
  // 旧逻辑据此误判「滚动完成」→ 等封面 → auto_scroll_done 关窗，用户只拿到
  // 一半数据（实测 249 条只到 116）。现在卡住时**绝不发 auto_scroll_done**，
  // 改为在页面底部提示用户手动滚动或点 ↻ 重试（窗口保持打开）。
  function showStuckBanner(msg){
    if (!document.body) return;
    if (!stuckBanner) {
      stuckBanner = document.createElement('div');
      stuckBanner.id = '__vib_scroll_stuck__';
      stuckBanner.setAttribute('style',
        'position:fixed!important;left:0!important;right:0!important;bottom:0!important;z-index:2147483646!important;' +
        'background:#e8f0ff!important;border-top:1px solid #a8c3f0!important;color:#1b3a6b!important;' +
        'font:13px/1.6 "Microsoft YaHei",system-ui!important;padding:9px 16px!important;' +
        'text-align:center!important;box-shadow:0 -2px 8px rgba(0,0,0,.12)!important;');
    }
    stuckBanner.textContent = msg;
    if (!stuckBanner.parentNode) document.body.appendChild(stuckBanner);
  }
  function hideStuckBanner(){
    try { if (stuckBanner && stuckBanner.parentNode) stuckBanner.parentNode.removeChild(stuckBanner); } catch(e) {}
  }
  // 专辑总条数：从页面头部徽标/正文里读「共 N 条」（只读 DOM，不做任何交互）。
  // 读不到返回 0（调用方按「无总数信息」处理，不做对账）。
  function boardTotalFromDom(){
    try {
      // 2026-10-20：小红书 board 页真实形态是「笔记 249」「249 篇」——原三正则
      // （共N条/笔记·N）全不匹配，导致 total 恒为 0、总数对账从未生效（163/249
      // 误报完成的直接原因）。这里多形态宽松匹配，并尝试 __INITIAL_STATE__。
      var pats = [
        /共\s*(\d+)\s*个?笔记/, /共\s*(\d+)\s*条/,
        /笔记\s*[·:：]?\s*(\d+)/, /(\d+)\s*篇/, /(\d+)\s*条笔记/
      ];
      var sels = ['[class*="count"]', '[class*="total"]', '[class*="num"]', 'header', 'h1'];
      for (var si = 0; si < sels.length; si++) {
        var nodes;
        try { nodes = document.querySelectorAll(sels[si]); } catch(e) { continue; }
        for (var ni = 0; ni < nodes.length && ni < 40; ni++) {
          var tx = (nodes[ni].textContent || '');
          for (var pi = 0; pi < pats.length; pi++) {
            var m = tx.match(pats[pi]);
            if (m) { var n1 = parseInt(m[1], 10); if (n1 > 0) return n1; }
          }
        }
      }
      var bt = bodyText();
      for (var pj = 0; pj < pats.length; pj++) {
        var mm = bt.match(pats[pj]);
        if (mm) { var n2 = parseInt(mm[1], 10); if (n2 > 0) return n2; }
      }
      // 最后尝试页面全局状态里的专辑笔记数（小红书 SPA 常见挂载点）
      try {
        var st = window.__INITIAL_STATE__;
        var cand = st && (st.board || st.boardPage || st.note || {});
        for (var k in cand) {
          var v = cand[k];
          if ((/count|total|num/i.test(k)) && typeof v === 'number' && v > 0 && v < 100000) return v;
        }
      } catch(e) {}
    } catch(e) {}
    return 0;
  }
  function enterLoginLost(){
    if (LOGIN_LOST) return;
    LOGIN_LOST = true;
    // 注意：不重置 loginResumeTries——登录→滚动→再次失效 的循环必须共享
    // 同一个次数预算，否则每轮重置会让「防死循环」形同虚设。
    pendingResumeAt = 0;
    scrollPausedByLogin = true;
    autoScrolling = false;   // 让自动滚动 interval 下一拍自行退出
    try {
      if (autoBtnRef) {
        autoBtnRef.textContent = '⏸';
        autoBtnRef.title = '登录态失效：登录后将自动继续遍历';
      }
    } catch(e) {}
    showLoginBanner('检测到登录态失效 —— 请登录后将继续捕获，此窗口不会自动关闭');
    sig('login_lost', { cards: pageCardCount(), path: (location.pathname || '').slice(0, 80) });
  }
  function exitLoginLost(){
    if (!LOGIN_LOST) return;
    LOGIN_LOST = false;
    pendingResumeAt = 0;
    hideLoginBanner();
    sig('login_resumed', { cards: pageCardCount(), tries: loginResumeTries,
                           path: (location.pathname || '').slice(0, 80) });
  }
  function checkLoginState(){
    var lost = detectLoginLost();
    if (lost) { loginLostHits++; loginOkHits = 0; }
    else { loginOkHits++; loginLostHits = 0; }
    if (!LOGIN_LOST && loginLostHits >= 2) enterLoginLost();
    else if (LOGIN_LOST && loginOkHits >= 2) exitLoginLost();
  }
  // 登录恢复后自动续跑（复用 restarts 语义：有限次数，超限转手动）
  function maybeResumeScroll(){
    if (!scrollPausedByLogin || LOGIN_LOST) return;
    if (!onBoardPage()) {
      // 登录后跳到别的页面：不做自动导航（红线），让用户自己点「←」
      showLoginBanner('已登录 —— 请点右上角「←」返回收藏专辑页，返回后自动继续捕获（窗口不会自动关闭）');
      closeBlocked = true;
      return;
    }
    var cards = pageCardCount();
    if (cards === 0) {
      if (!pendingResumeAt) pendingResumeAt = Date.now();
      if (Date.now() - pendingResumeAt > 15000) {
        pendingResumeAt = 0;
        loginResumeTries++;
        if (loginResumeTries >= LOGIN_RESUME_MAX) {
          showLoginBanner('已登录但仍未读取到笔记列表 —— 请手动关闭本窗口后重试（窗口不会自动关闭）');
          closeBlocked = true;
          sig('login_stuck', { tries: loginResumeTries, path: (location.pathname || '').slice(0, 80) });
        } else {
          showLoginBanner('登录成功，但笔记列表仍为空 —— 请确认专辑页已加载（窗口不会自动关闭）');
        }
      }
      return;
    }
    pendingResumeAt = 0;
    // 次数上限：达上限后不再自动续跑，转人工（但依然不自动关窗）
    if (loginResumeTries >= LOGIN_RESUME_MAX) {
      showLoginBanner('已登录但仍未读取到笔记列表 —— 请手动关闭本窗口后重试（窗口不会自动关闭）');
      closeBlocked = true;
      return;
    }
    loginResumeTries++;
    scrollPausedByLogin = false;
    closeBlocked = false;
    hideLoginBanner();
    sig('login_resumed_scroll', { cards: cards, tries: loginResumeTries });
    if (autoScrollFn) {
      setTimeout(function(){
        try { if (!LOGIN_LOST && !autoScrolling) autoScrollFn(autoBtnRef); } catch(e) {}
      }, 800);
    }
  }
  // 用户自己导航回专辑页（点「←」/「↻」）：解除「离开专辑页」的封锁，
  // 允许重新开始自动遍历。不做任何自动导航——只跟随用户操作。
  function rearmOnBoardPage(){
    if (LOGIN_LOST) return;
    if (!onBoardPage()) return;
    if (!closeBlocked && !scrollPausedByLogin) return;
    var cards = pageCardCount();
    if (cards === 0) return;
    closeBlocked = false;
    scrollPausedByLogin = false;
    pendingResumeAt = 0;
    window.__VIB_AUTO_STARTED__ = false;
    navMountedAt = Date.now();
    hideLoginBanner();
    sig('rearm_board', { cards: cards, path: (location.pathname || '').slice(0, 80) });
  }
  // 唯一的关窗信号出口：任何一条不满足都不发（宁可让用户手动关，也不打断登录）
  function emitDone(extra){
    if (LOGIN_LOST || closeBlocked || scrollPausedByLogin) return;
    // 不在专辑页（登录后跳走/用户点开笔记）：不能把"离开页面"当"遍历完成"。
    // 2026-10-20 修复：此前直接 return 会让 ⏳ 状态永久挂着（用户点开一条
    // 笔记再返回，窗口再也不自动关、清单不刷新——日志实锤页面曾到 about:blank）。
    // 改为记录待发状态，tick 里回到专辑页后重发（120s 内有效）。
    if (!onBoardPage()) {
      window.__VIB_PENDING_DONE_AT = Date.now();
      try { btnEl.title = '已捕获完成，返回专辑页后将自动关窗…'; } catch(e0) {}
      return;
    }
    extra = extra || {};
    // 注意：遍历里的 cnt 只数 seen（state/API 通道）+ explore 链接，而专辑页
    // 卡片主链接是 /board/{bid}/{nid}，所以成功遍历时 cnt 也可能是 0。
    // 这里用**累计**捕获数兜底（虚拟列表滚到底时瞬时 DOM 卡片数会变 0），
    // 否则正常成功也会被"一条都没捕获"误挡。
    var captured = (extra.captured || 0) || capturedTotal();
    if (captured === 0) {
      // 最后再看一眼当前 DOM：可能 state 通道没数据、DOM 也恰好被虚拟列表清空
      captured = pageCardCount();
    }
    // 兜底：真的一条都没有时自动关窗没有任何收益，只会打断正在登录的用户。
    // 登录检测万一漏判（登录弹层未渲染出特征文案），这里仍能挡住关窗。
    if (captured === 0) {
      showLoginBanner(detectLoginLost()
        ? '检测到登录态失效 —— 请登录后将继续捕获，此窗口不会自动关闭'
        : '未读取到任何笔记 —— 窗口不会自动关闭；确认列表已出现后请手动关闭本窗口');
      sig('auto_scroll_done_blocked', { reason: 'no_captured', path: (location.pathname || '').slice(0, 80) });
      return;
    }
    extra.captured = captured;
    extra.login_lost = false;
    extra.total = boardTotalFromDom() || 0; // Rust 侧总数终裁用（JS 对账漏网的双保险）
    extra.path = (location.pathname || '').slice(0, 80);
    sig('auto_scroll_done', extra);
  }
  // 自动开始遍历（原 mountNav 内的 4s 一次性逻辑）。抽出来供 tick 重试：
  // 首次打开时列表还没渲染 / 用户正在登录，都不能就此永久放弃。
  function tryAutoStart(){
    if (window.__VIB_AUTO_STARTED__) return;
    if (LOGIN_LOST || scrollPausedByLogin || closeBlocked) return;
    if (!onBoardPage() || !autoScrollFn) return;
    // 保留原语义：页面挂载后先给 4 秒让列表渲染，不要一上来就滚
    if (Date.now() - navMountedAt < 4000) return;
    if (detectLoginLost()) return;
    var cards = pageCardCount();
    if (cards === 0) return;          // 列表还没出来，等下一拍
    window.__VIB_AUTO_STARTED__ = true;
    setTimeout(function(){
      try { if (!LOGIN_LOST && !autoScrolling) autoScrollFn(autoBtnRef); } catch(e) {}
    }, 600);
  }
  // 实测发现：小红书 board 列表 API 返回的对象**只有 noteId/type/xsecToken，
  // 没有 title/cover**——这就是 242 条全"未命名"的根因。
  // 标题和封面只在**页面渲染出的卡片 DOM** 里。此前的 DOM 扫描被误加到了
  // PASSIVE_JS_TEMPLATE，而实际窗口用的是本模板——修复没生效。
  // 这里直接扫 DOM：只读，不点击不导航。
  var _domScanAt = 0;
  function scanBoardDom(){
    if (!boardId) return;
    var now = Date.now();
    if (now - _domScanAt < 1500) return;
    _domScanAt = now;
    var anchors;
    try {
      // 2026-10-17 card_dom 诊断实证：专辑页卡片的**主链接**是
      // /board/{boardId}/{noteId}?xsec_token=…（不是 /explore/…），
      // 且主链接里带 xsec_token。/explore/ 形态只出现在一个
      // display:none 的隐藏空 <a> 里（无标题封面可提取）。
      // 之前只认 explore → 31 条卡片漏抓 + 提取质量差。现两种都认。
      anchors = document.querySelectorAll('a[href*="/board/"], a[href*="/explore/"], a[href*="/discovery/item/"], a[href*="/search_result/"], a[href*="/note/"]');
      if (!anchors || !anchors.length) anchors = document.querySelectorAll('a[href]');
    } catch(e) { return; }
    // ── DOM 统计诊断（定位 273-242=31 缺失）──
    // 用户 App 实测 273 条 vs 捕获 242。三种可能：① 特殊卡型链接形态不同
    // ② 已删除/私密笔记渲染为无链接占位卡 ③ 虚拟列表移除滚过的节点。
    // 用「唯一 24 位 ID 数」而非 anchors 数来数真实卡片（隐藏链接会重复算）。
    try {
      var all = document.querySelectorAll('a[href]');
      var idSet = {}, idCount = 0, linkBoard = 0, linkExplore = 0, linkOther = 0, noLink = 0;
      var re24 = /[0-9a-f]{24}/;
      for (var ai = 0; ai < all.length; ai++) {
        var h2 = all[ai].getAttribute('href') || '';
        var mm24 = h2.match(re24);
        if (mm24 && !idSet[mm24[0]]) {
          idSet[mm24[0]] = 1; idCount++;
          if (h2.indexOf('/board/') >= 0) linkBoard++;
          else if (h2.indexOf('/explore/') >= 0) linkExplore++;
          else linkOther++;
        }
      }
      // 无链接的卡片容器（已删除/私密占位卡）：含卡片 class 但内部没有含 ID 的链接
      var ph = document.querySelectorAll('section.note-item, div[class*="note-item"], div[class*="feeds-item"]');
      if (!window.__VIB_COUNT_SENT__ || window.__VIB_COUNT_SENT__ !== idCount) {
        window.__VIB_COUNT_SENT__ = idCount;
        sig('diag', { step: 'dom_count', unique_ids: idCount, board_links: linkBoard,
                      explore_links: linkExplore, other_links: linkOther,
                      section_cards: ph.length, anchors_total: all.length,
                      scroll_h: document.documentElement.scrollHeight });
      }
    } catch(e) {}
    for (var i = 0; i < anchors.length; i++) {
      var a = anchors[i];
      var href = a.getAttribute('href') || '';
      // 主形态：/board/{bid}/{nid}；兜底形态：/explore/{nid} 等
      // 用字符串拆分而非 RegExp——Rust raw string 里写正则转义易错
      var nid = '';
      var bPrefix = '/board/' + boardId + '/';
      if (href.indexOf(bPrefix) >= 0) {
        var rest = href.split(bPrefix)[1] || '';
        var mm2 = rest.match(/^[0-9a-f]{24}/);
        if (mm2) nid = mm2[0];
      }
      if (!nid) {
        var m = href.match(/\/(?:explore|discovery\/item|search_result|note)\/([0-9a-f]{24})/);
        if (m) nid = m[1];
      }
      // 隐藏空链接（display:none 且无内容）跳过——它没有可提取的标题封面，
      // 但别 return，继续看同卡片的主链接（后面的分支会命中）
      if (a.style && a.style.display === 'none' && !a.textContent) continue;
      if (!nid) continue;
      // 提取标题与封面（卡片在 <a> 内部或最近父级）
      // 2026-10-18 修复封面误取头像：card_dom 实证封面在 <a class="cover">
      // 内的 <img>（sns-webpic 域），头像在 footer 的 author-wrapper 里；
      // 旧逻辑"root 内第一个 img"会把头像域 sns-avatar 的小图当封面。
      // 且同一张卡 a.cover/a.title 两个锚点都会 post、后端盲覆盖——
      // 必须让每个锚点都能收敛到正确的封面+标题，最后写的才是对的。
      var title = '', cover = '';
      function okImgSrc(s){
        return (s && s.indexOf('data:') !== 0 && s.indexOf('sns-avatar') < 0) ? s : '';
      }
      function imgSrcOf(im){
        if (!im) return '';
        var s = im.currentSrc || im.src || '';
        if (!s && im.getAttribute) s = im.getAttribute('data-src') || '';
        return okImgSrc(s);
      }
      function pickCover(root){
        if (!root || !root.querySelectorAll || cover) return;
        // 封面只认 [class*="cover"] 容器内的 img
        var cv = root.querySelectorAll('[class*="cover"] img');
        for (var c0 = 0; c0 < cv.length && !cover; c0++) cover = imgSrcOf(cv[c0]);
        // 退路：root 内第一个非头像 img
        if (!cover) {
          var ims = root.querySelectorAll('img');
          for (var i0 = 0; i0 < ims.length && !cover; i0++) cover = imgSrcOf(ims[i0]);
        }
        // 最后兜底：CSS 背景图（同样排除头像域）
        if (!cover) {
          var bgs = root.querySelectorAll('[style*="background-image"]');
          for (var bi = 0; bi < bgs.length && !cover; bi++) {
            var bm = ((bgs[bi].getAttribute('style') || '').match(/url\(["']?(https?:\/\/[^"')]+)["']?\)/i) || [])[1];
            if (okImgSrc(bm)) cover = bm;
          }
        }
      }
      function pickTitle(root){
        if (!root || !root.querySelectorAll || title) return;
        // 标题优先 [class*=title] / a.title（实证：footer <a class="title">
        // 内的 <span>）；通用候选集只作兜底——它会先命中作者名/点赞数
        var pri = ['[class*="title"] span', 'a[class*="title"]', '[class*="title"]'];
        for (var p0 = 0; p0 < pri.length && !title; p0++) {
          var tc = root.querySelectorAll(pri[p0]);
          for (var t0 = 0; t0 < tc.length; t0++) {
            var tx = (tc[t0].textContent || '').trim();
            if (tx.length >= 4 && tx.length <= 200 && !/^[\d.\s万+]+$/.test(tx)) { title = tx; break; }
          }
        }
        if (!title) {
          var cands = root.querySelectorAll('[class*="Title"], .name, span, div');
          for (var ci = 0; ci < cands.length; ci++) {
            var t2 = (cands[ci].textContent || '').trim();
            // 注意：raw string 里必须写 \d 而非 \\d——后者进 JS 后
            // 匹配的是字面反斜杠/字母d，纯数字标题过滤完全失效（存量bug已修）
            if (t2.length >= 4 && t2.length <= 200 && !/^[\d.\s万+]+$/.test(t2)) { title = t2; break; }
          }
        }
      }
      function pick(root){
        if (!root || (title && cover)) return;
        if (!cover) pickCover(root);
        if (!title) pickTitle(root);
      }
      pick(a);
      if (!title || !cover) {
        var up = a.parentElement;
        for (var lv = 0; lv < 3 && up && (!title || !cover); lv++) { pick(up); up = up.parentElement; }
      }
      if (!title) title = (a.getAttribute('aria-label') || '').trim();
      // 一次性诊断：把第一张卡的真实结构发回日志，便于校准选择器
      if (!window.__VIB_CARD_DUMP__) {
        window.__VIB_CARD_DUMP__ = 1;
        try {
          sig('diag', { step: 'card_dom',
            html: ((a.parentElement || a).outerHTML || '').slice(0, 1500),
            gotTitle: title.slice(0, 60), gotCover: cover.slice(0, 100) });
        } catch(e) {}
      }
      // 有标题或封面才 post（覆盖 API 路径的"未命名"记录，后端 upsert）
      if (!title && !cover) continue;
      domSeen[nid] = 1;
      post('/ingest_board_notes', { board_id: boardId, notes: [{
        note_id: nid,
        title: title || '未命名',
        type: '',
        cover: cover,
        xsec_token: (function(){ var q = href.split('?')[1] || ''; var mm = q.match(/xsec_token=([^&]+)/); return mm ? decodeURIComponent(mm[1]) : ''; })(),
        xsec_source: 'pc_board_dom'
      }] }, function(){ ingested[nid] = 1; });
    }
  }
  // 导航控件：←返回/→前进/↻刷新（翻自己收藏随时回退）
  var navReady = false;
  function mountNav(){
    if (navReady || !document.body) return;
    navReady = true;
    var bar = document.createElement('div');
    bar.setAttribute('style',
      'position:fixed!important;top:8px!important;right:8px!important;z-index:2147483647!important;' +
      'display:flex!important;gap:6px!important;background:rgba(20,20,20,.88)!important;' +
      'border-radius:10px!important;padding:6px 8px!important;box-shadow:0 4px 16px rgba(0,0,0,.4)!important;');
    function btn(txt, title, fn){
      var b = document.createElement('button');
      b.textContent = txt; b.title = title;
      b.setAttribute('style', 'width:34px;height:34px;border:none;border-radius:8px;background:#fff;color:#111;font-size:16px;cursor:pointer;line-height:1;');
      b.addEventListener('click', function(e){ e.stopPropagation(); fn(); });
      return b;
    }
    // ── 自动滚动遍历（2026-10-17 用户需求）──
    // 目的：一次性把专辑滚到底，触发懒加载渲染全部卡片，让 DOM 扫描能抓到。
    // 全程由用户点击触发；滚动是页面内的正常浏览行为（不是接口调用/爬取）。
    //
    // 2026-10-17 修正"滚到 74 条就自动关窗"：原来用「滚动高度连续 6 次不变」
    // 判定到底，但懒加载有延迟——高度暂时不变≠没有更多数据。改为**双条件**：
    //   ① 页面高度不再增长 且 ② 已捕获数量不再增长（都需连续多次确认）
    // 只有两者同时稳定才算真的到底。
    // 2026-10-19：autoScrolling / autoBtnRef / autoScrollFn 提到外层，
    // 供登录态守卫暂停与恢复遍历（登录失效时不能继续滚，更不能关窗）。
    // 这里直接复用外层变量，不要再 var 遮蔽。
    // 2026-10-18 修复"页面滚不动、13 秒就误判到底关窗"：实测小红书 board 页
    // 存在两种布局——document 自身可滚（scrollHeight 可到 6 万 px），或内容
    // 放在**内层容器**里滚（documentElement.scrollHeight ≈ 视口高）。后者
    // window.scrollTo 一步都滚不动 → 高度/数量双稳定 → 8 秒就误判到底。
    // 每拍滚动前重新探测真正的滚动容器：document 滚不动就找
    // overflow:auto/scroll 且 scrollHeight 明显大于 clientHeight 的最大元素。
    function findScroller(){
      var doc = document.documentElement;
      var vh = window.innerHeight || 800;
      if (doc.scrollHeight > vh + 50)
        return { el: null, h: doc.scrollHeight, top: window.scrollY || window.pageYOffset || 0 };
      var els;
      try { els = document.querySelectorAll('div,section,main'); } catch(e) { els = []; }
      var best = null;
      for (var fi = 0; fi < els.length; fi++) {
        var fel = els[fi];
        var fsh = fel.scrollHeight, fch = fel.clientHeight;
        if (!fsh || fsh <= fch + 50) continue;
        var foy = '';
        try { foy = getComputedStyle(fel).overflowY; } catch(e) { continue; }
        if ((foy === 'auto' || foy === 'scroll') && (!best || fsh > best.h))
          best = { el: fel, h: fsh, top: fel.scrollTop };
      }
      return best || { el: null, h: doc.scrollHeight, top: window.scrollY || window.pageYOffset || 0 };
    }
    function autoScrollAll(btnEl){
      // 登录态失效时禁止启动/继续（用户正在输密码，绝不打扰）
      if (LOGIN_LOST || scrollPausedByLogin) {
        btnEl.textContent = '⏸';
        btnEl.title = '登录态失效：登录后将自动继续遍历';
        return;
      }
      if (autoScrolling) { autoScrolling = false; btnEl.textContent = '⇩'; btnEl.title = '自动滚动到底部（遍历全部笔记）'; return; }
      autoScrolling = true;
      btnEl.textContent = '⏸'; btnEl.title = '暂停自动滚动';
      hideStuckBanner();
      // 每次启动/恢复用一个自增 runId 作废上一轮遗留的 setTimeout 链，
      // 避免用户暂停后重开时两轮恢复动作互相打架。
      var runId = ++autoRunSeq;
      var lastH = 0, sameH = 0, lastCount = -1, sameCount = 0, steps = 0;
      var stuckTries = 0;      // 卡住恢复尝试次数（上限 8）
      var reconDone = false;   // 总数对账的二次确认是否已做过
      var entered = false;     // 已进入终态（waitImages / stuck），防重复
      var timer = null;
      function alive(){ return autoScrolling && !entered && runId === autoRunSeq; }
      // 恢复/对账用的是 setTimeout 链，绕过了主 interval 的守卫；
      // 这里补上同样的登录态 / 页面守卫，保持 v3.4 语义不变。
      function stillOk(){
        if (LOGIN_LOST || scrollPausedByLogin) {
          stopLoop();
          autoScrolling = false;
          try { btnEl.textContent = '⏸'; btnEl.title = '登录态失效：登录后将自动继续遍历'; } catch(e) {}
          return false;
        }
        if (!onBoardPage()) {
          stopLoop();
          autoScrolling = false;
          scrollPausedByLogin = true;
          closeBlocked = true;
          showLoginBanner('已离开收藏专辑页 —— 请点右上角「←」返回，返回后自动继续捕获（窗口不会自动关闭）');
          return false;
        }
        return alive();
      }
      function stopLoop(){ if (timer) { clearInterval(timer); timer = null; } }
      // 统一进入「等封面」路径：这是唯一允许最终发 auto_scroll_done 的门。
      function goWaitImages(captured){
        if (entered || runId !== autoRunSeq) return;
        entered = true;
        stopLoop();
        autoScrolling = false;
        hideStuckBanner();
        btnEl.textContent = '⏳';
        btnEl.title = '滚动完成，等待封面图片加载…';
        // ── 等封面图片加载完（2026-10-17 用户要求）──
        // 2026-10-17 修复"永远不关窗"：原判断 `!autoScrolling && attempt>0`
        // 在第一次递归就成立（autoScrolling 已置 false），导致永久卡死。
        // 改为无中断的有限轮询：最多 30 次 × 600ms = 18 秒，超时也收尾。
        //
        // ── 2026-10-20 修核心矛盾（用户点破）：⏳ 语义="滚动结束等封面"，
        // 但懒加载可能只是**暂时停顿**——双稳定判定（15s 不长高）在懒加载
        // 慢时会提前宣告"滚动结束"，且停滚后懒加载更没理由自己恢复。
        // 三层处理：
        //  ① 每拍微滚"踢"一脚懒加载（滚轮事件触发平台的加载逻辑）
        //  ② 页面又长高/卡片又变多 → **立即恢复自动滚动**（暂停≠结束）
        //  ③ 底部还挂着「加载中」指示时绝不判完成；等满超时仍在加载 →
        //     emitStuck 把状态交还用户（宁可手动续捕，不假装完成）
        var waitH = 0, waitCnt = -1;
        try {
          var sc0 = findScroller();
          waitH = sc0.el ? sc0.el.scrollHeight : document.documentElement.scrollHeight;
          waitCnt = pageCardCount();
        } catch(e0) {}
        (function waitImages(attempt){
          var loadingHint = false;
          try { loadingHint = bodyText().indexOf('加载中') >= 0; } catch(e1) {}
          if (attempt > 30) {
            // 超时：页面自己还显示「加载中」= 平台还在磨 → 不是完成态，交还用户
            if (loadingHint) {
              // emitStuck 的重入 guard 检查 entered——先复位再转交（否则静默吞掉，
              // 窗口既不关也不提示，实测 S8 抓出的坑）
              entered = false;
              emitStuck(0, findScroller(), capturedTotal() || captured);
              return;
            }
            // 无加载指示且有图永远不加载（虚拟列表外）→ 正常收尾
            btnEl.textContent = '✓';
            btnEl.title = '已遍历完成（' + captured + ' 条），即将自动关闭窗口';
            setTimeout(function(){
              // 2026-10-19：关窗信号统一过 emitDone 的登录态门禁
              emitDone({ steps: steps, captured: captured, imgs_timeout: true });
            }, 800);
            return;
          }
          // ① 踢一脚懒加载：微小滚轮脉冲（模拟真人停顿时的无意识滚动）
          try {
            var kick = findScroller();
            (kick.el || document).dispatchEvent(
              new WheelEvent('wheel', { deltaY: 60, deltaX: 0, bubbles: true, cancelable: true }));
          } catch(e2) {}
          // ② 页面生长检测：高度/卡片数超过等待基线 → 懒加载缓过来了，恢复滚动
          var hNow = 0, cntNow = 0;
          try {
            var scw = findScroller();
            hNow = scw.el ? scw.el.scrollHeight : document.documentElement.scrollHeight;
            cntNow = pageCardCount();
          } catch(e3) {}
          if (hNow > waitH + 40 || cntNow > waitCnt) {
            entered = false;
            sameH = 0; sameCount = 0; lastH = 0; lastCount = -1;
            btnEl.textContent = '⏸';
            btnEl.title = '检测到懒加载恢复，继续滚动…';
            startLoop();
            return;
          }
          if (hNow > waitH) waitH = hNow;
          if (cntNow > waitCnt) waitCnt = cntNow;
          var imgs;
          try { imgs = document.querySelectorAll('img'); } catch(e) { imgs = []; }
          var pending = 0;
          for (var i = 0; i < imgs.length; i++) {
            var im = imgs[i];
            if (im.src && !im.complete) pending++;
          }
          if (pending > 0 || loadingHint) {
            btnEl.title = loadingHint
              ? '页面仍在加载更多内容，持续观察中…'
              : '滚动完成，等待封面加载（' + pending + ' 张）…';
            setTimeout(function(){ waitImages(attempt + 1); }, 600);
            return;
          }
          btnEl.textContent = '✓';
          btnEl.title = '已遍历完成（' + captured + ' 条），即将自动关闭窗口';
          setTimeout(function(){
            // 2026-10-19：关窗信号统一过 emitDone 的登录态门禁
            emitDone({ steps: steps, captured: captured, imgs_waited: attempt });
          }, 1000);
          setTimeout(function(){ btnEl.textContent = '⇩'; btnEl.title = '自动滚动到底部（遍历全部笔记）'; }, 4000);
        })(0);
      }
      // 卡住终态：**绝不发 auto_scroll_done**，只发诊断信号 + 页面提示。
      // 窗口保持打开，等用户手动滚动/刷新或重新点自动滚。
      function emitStuck(pos, sc, cntNow){
        if (entered || runId !== autoRunSeq) return;
        entered = true;
        stopLoop();
        autoScrolling = false;
        // 累计数用 capturedTotal（seen ∪ domSeen 去重并集）：专辑页主链接是
        // /board/{bid}/{nid}，循环里的 cnt 会漏掉 DOM 通道 → 报少。
        var captured = capturedTotal() || cntNow || 0;
        var total = boardTotalFromDom();
        btnEl.textContent = '⇩';
        btnEl.title = '加载似乎卡住了 —— 可手动滚动或点 ↻ 重试（窗口不会自动关闭）';
        showStuckBanner('加载似乎卡住了（已捕获 ' + captured + ' / 共 ' + (total || '?') +
                        '）—— 可手动滚动或点 ↻ 重试，窗口不会自动关闭');
        sig('scroll_stuck', {
          captured: captured, total: total,
          pos: Math.round(pos || 0), h: (sc && sc.h) || 0,
          scroller: sc && sc.el ? String(sc.el.className || sc.el.tagName).slice(0, 60) : 'doc',
          tries: stuckTries
        });
      }
      // 卡住恢复：回滚 1.5 屏 → 停 800ms → 下滚 2 屏（WheelEvent 兜底）→
      // 视口前进了就回正常滚动；没前进则按递增间隔重试，最多 8 次。
      function recoverFromStuck(sc0){
        if (!stillOk()) return;
        stuckTries++;
        stopLoop();
        var vh = window.innerHeight || 800;
        var sc = sc0 || findScroller();
        var posBefore = sc.el ? sc.el.scrollTop : (window.scrollY || window.pageYOffset || 0);
        try {
          btnEl.title = '加载卡住，正在尝试恢复（第 ' + stuckTries + '/8 次）…';
          var back = Math.max(0, posBefore - vh * 1.5);
          if (sc.el) sc.el.scrollTop = back; else window.scrollTo(0, back);
        } catch(e) {}
        setTimeout(function(){
          if (!stillOk()) return;
          try { _domScanAt = 0; scanBoardDom(); } catch(e) {}
          var sc2 = findScroller();
          var vh2 = window.innerHeight || 800;
          var p0 = sc2.el ? sc2.el.scrollTop : (window.scrollY || window.pageYOffset || 0);
          var tgt = p0 + vh2 * 2;
          try {
            if (sc2.el) sc2.el.scrollTop = Math.min(tgt, sc2.h);
            else window.scrollTo(0, Math.min(tgt, sc2.h));
          } catch(e) {}
          // WheelEvent 兜底（保留原行为：transform 平移型虚拟列表）
          try {
            (sc2.el || document).dispatchEvent(
              new WheelEvent('wheel', { deltaY: vh2 * 2, deltaX: 0, bubbles: true, cancelable: true }));
            document.dispatchEvent(
              new WheelEvent('wheel', { deltaY: vh2 * 2, deltaX: 0, bubbles: true, cancelable: true }));
          } catch(e2) {}
          setTimeout(function(){
            if (!stillOk()) return;
            var sc3 = findScroller();
            var p1 = sc3.el ? sc3.el.scrollTop : (window.scrollY || window.pageYOffset || 0);
            if (p1 > posBefore + 20) {
              // 恢复了：清空稳定性计数，回到正常逐屏滚动
              var triesUsed = stuckTries;
              stuckTries = 0;
              sameH = 0; sameCount = 0; lastH = 0; lastCount = -1;
              try { _domScanAt = 0; scanBoardDom(); } catch(e) {}
              sig('diag', { step: 'scroll_recovered', pos: Math.round(p1), tries: triesUsed,
                            scroller: sc3.el ? String(sc3.el.className || sc3.el.tagName).slice(0, 60) : 'doc' });
              btnEl.textContent = '⏸';
              btnEl.title = '暂停自动滚动';
              startLoop();
              return;
            }
            if (stuckTries >= 8) { emitStuck(p1, sc3); return; }
            // 间隔递增 0.8s → 3s
            var waitMs = Math.min(3000, 800 + (stuckTries - 1) * 315);
            setTimeout(function(){
              if (!stillOk()) return;
              recoverFromStuck(findScroller());
            }, waitMs);
          }, 600);
        }, 800);
      }
      // 总数对账：DOM 里能读到「共 N 条」且累计捕获 < N-5 时，即使三条件都
      // 满足也先做一轮二次确认（回滚两屏 → 扫 → 再滚到底），仍无新增才完成。
      // 容差 5 是给平台不渲染条目留的（先例：273 收藏只有 242 渲染）。
      function reconcileThenFinish(sc){
        if (!stillOk()) return;
        var total = boardTotalFromDom();
        var captured = capturedTotal();
        // 2026-10-20：总数解析失败（页面形态变化 / SSR 壳 bodyLen=198）时
        // **绝不静默判完成**——上一轮 139/249 漏网正是 total=0 直接走了
        // goWaitImages。总数未知时把状态交还用户（emitStuck 已处理 '?' 显示）。
        if (total <= 0) {
          try {
            sig('diag', { step: 'total_unknown_stuck', captured: captured,
                          bodyHead: bodyText().slice(0, 200) });
          } catch(e0) {}
          emitStuck(0, sc, captured);
          return;
        }
        if (total > 0 && captured < total - 5 && !reconDone) {
          reconDone = true;
          var vh = window.innerHeight || 800;
          btnEl.title = '已到底但仅捕获 ' + captured + '/' + total + '，正在二次确认…';
          try {
            var cur = sc.el ? sc.el.scrollTop : (window.scrollY || window.pageYOffset || 0);
            var back = Math.max(0, cur - vh * 2);
            if (sc.el) sc.el.scrollTop = back; else window.scrollTo(0, back);
          } catch(e) {}
          setTimeout(function(){
            if (!stillOk()) return;
            try { _domScanAt = 0; scanBoardDom(); } catch(e) {}
            var sc2 = findScroller();
            try {
              if (sc2.el) sc2.el.scrollTop = sc2.h; else window.scrollTo(0, sc2.h);
              (sc2.el || document).dispatchEvent(
                new WheelEvent('wheel', { deltaY: (window.innerHeight || 800) * 2, deltaX: 0, bubbles: true, cancelable: true }));
              document.dispatchEvent(
                new WheelEvent('wheel', { deltaY: (window.innerHeight || 800) * 2, deltaX: 0, bubbles: true, cancelable: true }));
            } catch(e2) {}
            setTimeout(function(){
              if (!stillOk()) return;
              try { _domScanAt = 0; scanBoardDom(); } catch(e) {}
              var nowCap = capturedTotal();
              try {
                sig('diag', { step: 'total_reconcile', before: captured, after: nowCap, total: total,
                              scroller: sc2.el ? String(sc2.el.className || sc2.el.tagName).slice(0, 60) : 'doc' });
              } catch(e3) {}
              if (nowCap > captured) {
                // 二次确认捞到新条目：回正常滚动继续
                sameH = 0; sameCount = 0; lastH = 0; lastCount = -1;
                btnEl.textContent = '⏸';
                btnEl.title = '暂停自动滚动';
                startLoop();
                return;
              }
              // 2026-10-20 硬闸门：二次确认也捞不到新条目、总数已知且仍差 > 容差
              // → 判定「页面已加载不出更多」：发 scroll_stuck 把状态交还用户
              // （手动滚/刷新/再点自动滚续捕），**绝不静默完成关窗**。
              // 先例：163/249 误报完成就是软对账（二次确认必然通过）放行的。
              if (total > 0 && nowCap < total - 5) {
                emitStuck(0, sc2, nowCap);
                return;
              }
              goWaitImages(nowCap);
            }, 1200);
          }, 900);
          return;
        }
        goWaitImages(captured);
      }
      function startLoop(){
        stopLoop();
        timer = setInterval(function(){
        if (!alive()) { stopLoop(); return; }
        // 登录态在滚动途中失效：立刻停，不判"到底"，不发 done
        if (LOGIN_LOST || scrollPausedByLogin) {
          clearInterval(timer);
          autoScrolling = false;
          try { btnEl.textContent = '⏸'; btnEl.title = '登录态失效：登录后将自动继续遍历'; } catch(e) {}
          return;
        }
        // 被导航到非专辑页（如登录后跳个人主页）：同样停，绝不自动导航回去
        if (!onBoardPage()) {
          clearInterval(timer);
          autoScrolling = false;
          scrollPausedByLogin = true;
          closeBlocked = true;
          showLoginBanner('已离开收藏专辑页 —— 请点右上角「←」返回，返回后自动继续捕获（窗口不会自动关闭）');
          return;
        }
        // ── 逐屏滚动（2026-10-17 修复"少 30 条"）──
        // 原来一步滚到 pageHeight 底部：虚拟列表会把滚出视口的卡片**从 DOM
        // 移除**，快速直达底部时中间批次的卡片在 DOM 里停留太短、扫描间隔
        // （1.5s）抓不全 → 272 条只捕获到 242。
        // 改为每次只滚一屏，滚前先强制扫描当前屏，确保每张卡都被看到。
        try { _domScanAt = 0; scanBoardDom(); } catch(e) {}
        var vh = window.innerHeight || 800;
        var sc = findScroller();
        var before = sc.el ? sc.el.scrollTop : (window.scrollY || window.pageYOffset || 0);
        var target = before + vh;
        if (sc.el) sc.el.scrollTop = Math.min(target, sc.h);
        else window.scrollTo(0, Math.min(target, sc.h));
        // 2026-10-18 修复"滚到一半卡住"：部分虚拟列表用 transform 平移内容，
        // 原生 scrollTop 赋值无效（高度涨满但视口不动，~40% 卡片漏扫）。
        // 原生滚动没前进时用滚轮事件驱动页面自己的滚动逻辑。
        var after = sc.el ? sc.el.scrollTop : (window.scrollY || window.pageYOffset || 0);
        if (after - before < vh / 2 && (sc.el ? sc.h - after > vh : true)) {
          try {
            (sc.el || document).dispatchEvent(
              new WheelEvent('wheel', { deltaY: vh, deltaX: 0, bubbles: true, cancelable: true }));
            document.dispatchEvent(
              new WheelEvent('wheel', { deltaY: vh, deltaX: 0, bubbles: true, cancelable: true }));
          } catch(e2) {}
          // 滚轮兜底可能已让页面自己的滚动逻辑移动视口：重读真实位置，
          // 否则「到底判定」会用兜底前的旧值（少判/误判都会关错窗）。
          try { after = sc.el ? sc.el.scrollTop : (window.scrollY || window.pageYOffset || 0); } catch(e4) {}
        }
        // 每 5 拍上报滚动位置（定位"高度涨/视口不动"类卡死）
        if (steps % 5 === 0) {
          try {
            sig('diag', { step: 'scroll_pos', top: Math.round(after), h: sc.h,
                          scroller: sc.el ? String(sc.el.className || sc.el.tagName).slice(0, 40) : 'doc' });
          } catch(e3) {}
        }
        steps++;
        // 高度从**实际滚动容器**读（内层容器布局时 document 高度恒等于视口）
        var h = sc.el ? sc.el.scrollHeight : document.documentElement.scrollHeight;
        // 2026-10-17 修复"打开窗口不滚动"：原代码引用 seenBoard.size，
        // 但本模板的去重集合叫 seen（普通对象）——ReferenceError 让整个
        // 自动滚动挂掉。改为遍历计数，并把 DOM 卡片数也算进稳定判据。
        var cnt = 0; for (var ck in seen) cnt++;
        try { cnt += document.querySelectorAll('a[href*="/explore/"], a[href*="/discovery/item/"]').length; } catch(e) {}
        if (h === lastH) { sameH++; } else { sameH = 0; lastH = h; }
        if (cnt === lastCount) { sameCount++; } else { sameCount = 0; lastCount = cnt; }
        // ── 完成判定（2026-10-19 修复「卡在 116/249 仍报完成」）──
        // 旧判定只看「高度稳定 + 数量稳定」，从不看视口位置：懒加载停摆 /
        // 虚拟列表推不动时，视口卡在中间、高度数量双稳定 → 误判到底 → 关窗。
        // 现在加第三条件：视口确实接近容器真底部。
        // 用本轮**刚读过**的 h（与 sc.h 同一来源、但包含本拍懒加载带来的增长），
        // 避免用滚动前的旧高度把「差一屏」误算成到底。
        var atBottom = (h - after) <= vh * 1.5;
        var settled = sameH >= 10 && sameCount >= 10 && steps >= 12 && atBottom;
        if (settled) {
          stopLoop();
          btnEl.textContent = '⏳';
          btnEl.title = '滚动完成，等待封面图片加载…';
          reconcileThenFinish(sc);
        } else if (sameH >= 10 && sameCount >= 10 && steps >= 12 && !atBottom) {
          // 双稳定但视口不在底部 = 卡住：**绝不判完成**，进恢复循环
          recoverFromStuck(sc);
        } else if (steps > 600) {
          // 步数硬上限：到了真底部就正常收尾；没到底一律按卡住处理（不关窗）
          if (atBottom) { stopLoop(); reconcileThenFinish(sc); }
          else { emitStuck(after, sc, cnt); }
        }
      }, 700);
      }
      startLoop();
    }
    var autoBtn = btn('⇩', '自动滚动到底部（遍历全部笔记）', function(){ autoScrollAll(autoBtn); });
    autoBtnRef = autoBtn;
    autoScrollFn = autoScrollAll;
    navMountedAt = Date.now();
    bar.appendChild(autoBtn);
    bar.appendChild(btn('←', '返回上一页', function(){ history.back(); }));
    bar.appendChild(btn('→', '前进', function(){ history.forward(); }));
    bar.appendChild(btn('↻', '刷新当前页', function(){ location.reload(); }));
    document.body.appendChild(bar);
    // ── 自动开始遍历（2026-10-17 用户要求："让捕获窗口自己完整跑完"）──
    // 2026-10-19 修正：原来是 4 秒一次性判断，未登录/列表未渲染就永久放弃；
    // 现在先 4 秒试一次，之后由 tick 在"已登录且有卡片"时继续尝试。
    setTimeout(tryAutoStart, 4000);
  }
  var lastCount = -1;
  function tick(){
    try {
      if (!boardId) boardId = (location.pathname.match(/\/board\/([0-9a-f]{24})/) || [])[1] || '';
      scanState();
      scanBoardDom();
      mountNav();
      // 登录态守卫：每秒采样一次，连续 2 拍一致才切换状态（防瞬时误判）
      checkLoginState();
      if (LOGIN_LOST) {
        // 失效期间持续保证横幅在（SPA 重渲染可能把节点冲掉）
        showLoginBanner('检测到登录态失效 —— 请登录后将继续捕获，此窗口不会自动关闭');
        // 后端信号槽只保留最新一条事件，其他事件会覆盖 login_lost；
        // 每 5 秒重发一次，确保 Rust 侧也能拿到「暂停关窗」状态（双保险）。
        if (Date.now() - _lastLoginSigAt > 5000) {
          _lastLoginSigAt = Date.now();
          sig('login_lost', { cards: pageCardCount(), path: (location.pathname || '').slice(0, 80) });
        }
      } else if (scrollPausedByLogin) {
        maybeResumeScroll();
      } else if (closeBlocked) {
        // 曾离开专辑页：用户自己导航回来后解除封锁（不自动导航）
        rearmOnBoardPage();
      } else if (window.__VIB_PENDING_DONE_AT) {
        // 2026-10-20：完成信号因离开专辑页被暂存——回到专辑页即重发关窗
        if (onBoardPage() && Date.now() - window.__VIB_PENDING_DONE_AT < 120000) {
          window.__VIB_PENDING_DONE_AT = 0;
          emitDone({ resumed_after_nav: true });
        } else if (Date.now() - window.__VIB_PENDING_DONE_AT >= 120000) {
          window.__VIB_PENDING_DONE_AT = 0; // 超时放弃，等用户手动关
        }
      } else if (!window.__VIB_AUTO_STARTED__) {
        // 首屏没渲染出列表（懒加载/慢网）时继续等，不要永久放弃自动遍历
        tryAutoStart();
      }
      var c = 0; for (var k in seen) c++;
      if (c !== lastCount) { lastCount = c; sig('phase', { msg: '专辑已顺路记录 ' + c + ' 条笔记' }); }
    } catch(e) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scanState);
  else scanState();
  setInterval(tick, 1000);
})()
"#;
    let js = BOARD_JS_TEMPLATE.replace("__LABEL__", &label);
    // 导航探针（Rust 侧一手数据）：窗口的每次导航都 POST 给后端日志——
    // 「白屏零信号」时能看到窗口到底导航到了哪、有没有被重定向/拦截
    let nav_label = label.clone();
    // 白屏兜底：窗口加载完成后若 body 仍为空，把诊断信息写进页面，
    // 用户至少能看到「为什么白」而不是一片空白（2026-10-17 实测白屏事故）。
    let diag_label = label.clone();
    let diag_js = format!(
        r#"(function(){{
  function post(ev, data){{
    try {{
      fetch('http://127.0.0.1:8483/api/xhs/harvest_signal', {{
        method:'POST', headers:{{'Content-Type':'text/plain'}},
        body: JSON.stringify({{ label:'{l}', event:ev, data:data||null }})
      }}).catch(function(){{}});
    }} catch(e) {{}}
  }}
  post('diag', {{ step:'window_opened', url: location.href, ready: document.readyState }});
  var tries = 0;
  var timer = setInterval(function(){{
    tries++;
    var bodyLen = document.body ? (document.body.innerText || '').length : -1;
    var htmlLen = document.body ? document.body.innerHTML.length : -1;
    if (bodyLen > 50 || tries > 40) {{
      clearInterval(timer);
      post('diag', {{ step:'load_check', bodyLen: bodyLen, htmlLen: htmlLen,
                     title: (document.title||'').slice(0,80), href: location.href.slice(0,120),
                     blank: bodyLen <= 50 }});
      // 仍然空白 → 在页面里画出诊断信息，避免用户面对纯白屏
      if (bodyLen <= 50) {{
        try {{
          document.body.innerHTML =
            '<div style="font:14px/1.7 system-ui;padding:40px;color:#333;max-width:760px;margin:0 auto">' +
            '<h2 style="color:#c0392b;margin:0 0 12px">页面未能加载</h2>' +
            '<p>捕获窗口打开了，但小红书页面没有渲染出内容。可能原因：</p>' +
            '<ul style="line-height:2">' +
            '<li>网络被拦截（代理/VPN/防火墙）</li>' +
            '<li>小红书对该内置浏览器返回了空页面</li>' +
            '<li>页面要求登录或安全验证</li>' +
            '</ul>' +
            '<p style="margin-top:20px"><b>当前地址：</b>' + (location.href||'').slice(0,150) + '</p>' +
            '<p><b>文档标题：</b>' + (document.title||'(空)') + '</p>' +
            '<p><b>DOM 长度：</b>' + htmlLen + ' 字符</p>' +
            '<p style="margin-top:24px"><button onclick="location.reload()" ' +
            'style="padding:10px 22px;font-size:14px;cursor:pointer;border:1px solid #ccc;border-radius:6px;background:#fff">重新加载</button></p>' +
            '<p style="color:#888;font-size:12px;margin-top:20px">诊断已记录到应用日志。若持续空白，可先在本机浏览器打开同一链接确认网络可达。</p>' +
            '</div>';
        }} catch(e) {{}}
      }}
    }}
  }}, 500);
}})()"#,
        l = diag_label
    );
    // 反自动化检测兼容（2026-10-17 实测：B站窗口正常、仅小红书白屏 →
    // 说明 WebView2 本身没问题，是小红书页面 JS 检测到自动化环境后不渲染）。
    // 这里在页面脚本之前抹掉最常被检测的自动化特征。**不做任何伪装登录、
    // 不伪造身份**，只是让内置浏览器表现得像普通浏览器——用户本来就是在
    // 自己的设备上、以自己（或匿名）身份正常浏览公开内容。
    const STEALTH_JS: &str = r#"
(function(){
  try {
    // WebView2 会暴露 window.chrome.webview；部分检测脚本据此判定"非浏览器"
    // 不删除（Tauri IPC 需要），但把 webdriver 标记去掉
    Object.defineProperty(navigator, 'webdriver', { get: function(){ return false; } });
    // 语言/平台补齐，避免 undefined 触发指纹异常
    if (!navigator.languages || !navigator.languages.length) {
      Object.defineProperty(navigator, 'languages', { get: function(){ return ['zh-CN','zh','en']; } });
    }
    // 插件列表：无头/内嵌环境常为空数组，普通浏览器有值
    if (!navigator.plugins || navigator.plugins.length === 0) {
      Object.defineProperty(navigator, 'plugins', {
        get: function(){ return [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}]; }
      });
    }
    // 通知权限（部分检测用其状态判断无头）
    if (navigator.permissions && navigator.permissions.query) {
      var origQuery = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = function(p){
        if (p && p.name === 'notifications') return Promise.resolve({ state: 'default', onchange: null });
        return origQuery(p);
      };
    }
    // 屏蔽常见的内嵌浏览器标识（不影响功能）
    try {
      var origUA = navigator.userAgent;
      if (origUA && origUA.indexOf('Edg/') >= 0) {
        // WebView2 的 UA 已带 Edg/，与真 Edge 一致，无需改；这里只兜底去掉
        // 可能被追加的 Electron/WebView 标记
        Object.defineProperty(navigator, 'userAgent', {
          get: function(){ return origUA.replace(/\s(WebView2?|Electron)\/[\d.]+/gi, ''); }
        });
      }
    } catch(e) {}
  } catch(e) {}
})();
"#;
    // 关窗前导出登录态用（builder 会 move parsed）
    let cookie_url = parsed.clone();
    let win = WebviewWindowBuilder::new(
        &app,
        label.clone(),
        WebviewUrl::External(parsed),
    )
    .additional_browser_args(WEBVIEW_ARGS)
    .initialization_script(STEALTH_JS)
    .initialization_script(&js)
    .initialization_script(&diag_js)
    .on_navigation(move |nav_url| {
        let body = serde_json::json!({
            "label": format!("{nav_label}-nav"),
            "event": "diag",
            "data": { "navigation": nav_url.as_str().chars().take(160).collect::<String>() }
        })
        .to_string();
        let _ = ureq::post("http://127.0.0.1:8483/api/xhs/harvest_signal")
            .timeout(Duration::from_secs(2))
            .send_string(&body);
        true // 允许导航继续
    })
    .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
    .title("VidInsight 收藏专辑捕获：像平常一样翻你的收藏（支持无限滚动），记录完成后直接关闭本窗口")
    .inner_size(1100.0, 800.0)
    .position(80.0, 40.0)
    .focused(true)
    .visible(true)
    .build()
    .map_err(|e| format!("打开专辑捕获窗口失败: {e}"))?;

    // ── 自动滚动完成后自动关窗（2026-10-17 用户需求）──
    // 页面点「⇩」滚到底后会 POST harvest_signal{event:"auto_scroll_done"}；
    // 这里轮询该信号，收到即关窗，前端随即刷新清单（关窗=捕获完成）。
    //
    // 2026-10-19 修复「登录被打断」事故：页面 JS 已在登录态失效时不再发
    // auto_scroll_done，这里再做一层 Rust 侧门禁（不信任单一信号）：
    //   ① 必须**精确匹配** event == "auto_scroll_done"（旧代码用 contains，
    //      会被 auto_scroll_done_blocked 误命中，直接关掉正在登录的窗口）
    //   ② data.login_lost == true / captured == 0 一律不关
    //   ③ 收到 login_lost 时暂停关窗判定并延长看护窗口；收到 login_resumed
    //      或 login_resumed_scroll 后恢复。
    {
        let auto_app = app.clone();
        let auto_label = label.clone();
        let auto_cookie_url = cookie_url.clone();
        tauri::async_runtime::spawn_blocking(move || {
            let mut deadline = Instant::now() + Duration::from_secs(15 * 60); // 上限 15 分钟
            // login_lost 带时效：页面在失效期间每 5 秒重发一次，超过 15 秒
            // 没有新 heartbeat 即视为已恢复（否则一旦恢复后事件被 phase 覆盖，
            // 这个永久标志会让正常流程永远不关窗）。
            let mut last_login_lost_at: Option<Instant> = None;
            let mut login_extensions = 0u32;
            while Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(1200));
                if auto_app.get_webview_window(&auto_label).is_none() {
                    return; // 用户已手动关闭
                }
                let url = format!(
                    "http://127.0.0.1:8483/api/xhs/harvest_signal?label={}",
                    auto_label
                );
                if let Ok(resp) = ureq::get(&url).timeout(Duration::from_secs(3)).call() {
                    if let Ok(body) = resp.into_string() {
                        let Ok(v) = serde_json::from_str::<serde_json::Value>(&body) else {
                            continue;
                        };
                        let event = v["data"]["event"].as_str().unwrap_or("");
                        let login_lost_recent = last_login_lost_at
                            .map(|t| t.elapsed() < Duration::from_secs(15))
                            .unwrap_or(false);
                        match event {
                            "login_lost" => {
                                // 登录态失效：暂停自动关窗，并延长看护时间（最多 3 次）
                                last_login_lost_at = Some(Instant::now());
                                if login_extensions < 3 {
                                    login_extensions += 1;
                                    deadline = Instant::now() + Duration::from_secs(10 * 60);
                                }
                            }
                            "login_resumed" | "login_resumed_scroll" => {
                                last_login_lost_at = None;
                                if Instant::now() + Duration::from_secs(120) > deadline {
                                    deadline = Instant::now() + Duration::from_secs(120);
                                }
                            }
                            // 2026-10-19 修复「卡在 116/249 仍报完成」：
                            // 页面侧判定滚动卡住时发 scroll_stuck，**绝不再发
                            // auto_scroll_done**。这里只更新窗口标题提示用户，
                            // 不关窗、不导航——等用户手动滚动/刷新或重试。
                            "scroll_stuck" => {
                                let data = &v["data"]["data"];
                                let captured = data["captured"].as_u64().unwrap_or(0);
                                let total = data["total"].as_u64().unwrap_or(0);
                                let total_txt = if total > 0 { total.to_string() } else { "?".to_string() };
                                if let Some(w) = auto_app.get_webview_window(&auto_label) {
                                    let _ = w.set_title(&format!(
                                        "⚠️ 加载似乎卡住了（已捕获 {captured} / 共 {total_txt}）—— 可手动滚动或点 ↻ 重试，窗口不会自动关闭"
                                    ));
                                }
                            }
                            "auto_scroll_done" if !login_lost_recent => {
                                let data = &v["data"]["data"];
                                let still_lost = data["login_lost"].as_bool().unwrap_or(false);
                                let captured = data["captured"].as_u64().unwrap_or(0);
                                if still_lost || captured == 0 {
                                    // 页面侧已挡过一次，这里再挡一次（宁可不关）
                                    continue;
                                }
                                // 2026-10-20 总数终裁（双保险）：JS 侧对账漏网
                                // （总数解析失败的页面形态）时，已知总数且
                                // 差距 > 容差 → 绝不关窗，改标题把状态交还用户。
                                let total = data["total"].as_u64().unwrap_or(0);
                                if total > 0 && captured + 5 < total {
                                    if let Some(w) = auto_app.get_webview_window(&auto_label) {
                                        let _ = w.set_title(&format!(
                                            "⚠️ 已捕获 {captured} / 共 {total} 条，未捕获完不自动关窗——可继续滚动或点 ↻ 重试，完成后手动关闭即可"
                                        ));
                                    }
                                    continue;
                                }
                                // 留 1.2s 让最后一批 ingest 请求落库，再关窗
                                std::thread::sleep(Duration::from_millis(1200));
                                if let Some(w) = auto_app.get_webview_window(&auto_label) {
                                    export_xhs_cookies(&w, &auto_cookie_url);
                                    let _ = w.destroy();
                                }
                                if let Some(main) = auto_app.get_webview_window("main") {
                                    let _ = main.set_focus();
                                }
                                return;
                            }
                            _ => {}
                        }
                    }
                }
            }
        });
    }

    // 关闭兜底（2026-10-17 实测事故：白屏窗口点 × 无反应，且主窗口随后卡死）。
    // WebView2 渲染进程在加载失败时挂起，正常的关闭消息队列不处理。
    // 用 spawn_blocking 延迟销毁，避免在事件回调里递归触发。
    {
        let close_app = app.clone();
        let close_label = label.clone();
        let close_cookie_url = cookie_url.clone();
        let main_win = win.clone();
        main_win.on_window_event(move |event| {
            let should_close = matches!(event, tauri::WindowEvent::CloseRequested { .. });
            if !should_close {
                return;
            }
            let app2 = close_app.clone();
            let label2 = close_label.clone();
            let cookie_url2 = close_cookie_url.clone();
            tauri::async_runtime::spawn_blocking(move || {
                // 给正常关闭留 300ms；若窗口还在（渲染挂起），强制销毁
                std::thread::sleep(std::time::Duration::from_millis(300));
                if let Some(w) = app2.get_webview_window(&label2) {
                    export_xhs_cookies(&w, &cookie_url2);
                    let _ = w.destroy();
                }
                // 主窗口恢复可交互，避免"整个应用点不动"的假死观感
                if let Some(main) = app2.get_webview_window("main") {
                    let _ = main.set_focus();
                    let _ = main.unminimize();
                }
            });
        });
    }

    // Rust 侧健康探针（不依赖页面 JS）：窗口创建后 3s / 8s 各记一次状态。
    // 2026-10-17 白屏事故：页面零信号、日志里连 navigation 都没有，无法判断
    // 是「窗口没建起来」「导航被拦」还是「HTML 没渲染」。这里用 eval 打一条
    // 必然执行的探针——若连 eval 都失败，说明 webview 本身没起来。
    {
        let probe_label = label.clone();
        let probe_app = app.clone();
        tauri::async_runtime::spawn_blocking(move || {
            for (delay_ms, tag) in [(3000u64, "t+3s"), (5000u64, "t+8s")] {
                std::thread::sleep(std::time::Duration::from_millis(delay_ms));
                let alive = probe_app.get_webview_window(&probe_label).is_some();
                // 关键：用 Rust 侧 eval 拿状态，不依赖 initialization_script 是否执行
                let js = r#"(function(){try{return JSON.stringify({
                    href:(location.href||'').slice(0,150),
                    title:(document.title||'').slice(0,80),
                    ready:document.readyState,
                    bodyLen:document.body?(document.body.innerText||'').length:-1,
                    htmlLen:document.body?document.body.innerHTML.length:-1,
                    hasBody:!!document.body,
                    ua:(navigator.userAgent||'').slice(0,90)
                });}catch(e){return 'ERR:'+e.message;}})()"#;
                let mut eval_result = String::from("(eval skipped)");
                if let Some(w) = probe_app.get_webview_window(&probe_label) {
                    // Tauri 2 的 eval 是 fire-and-forget；这里仅用于触发页面内诊断上报
                    eval_result = "eval_sent".to_string();
                    let _ = w.eval(js);
                }
                let probe_body = serde_json::json!({
                    "label": format!("{}-probe", probe_label),
                    "event": "diag",
                    "data": { "stage": tag, "window_alive": alive, "eval": eval_result }
                }).to_string();
                let _ = ureq::post("http://127.0.0.1:8483/api/xhs/harvest_signal")
                    .timeout(Duration::from_secs(2))
                    .send_string(&probe_body);
            }
        });
    }
    Ok(label)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // 2026-10-20 网络链路修复：WebView2 默认走系统代理（本机=Clash 127.0.0.1:7890），
    // 出口随节点漂移——切到海外节点时小红书/B站页面资源大面积加载失败（实测
    // 2026-10-20 05:46-06:00：封面裂图 + 页面内嵌「网络连接失败」横幅 + 滚动循环
    // 因页面加载不出而卡死 14 分钟），且海外出口 IP 本身就加重国内平台风控。
    // 本应用的 WebView 全部访问国内平台（xhs/bilibili，hellotik 明确不支持
    // YouTube），对后端 API 的回环访问本就 bypass 代理——所以整个进程的
    // WebView2 一律 direct 直连：不依赖 Clash 健康，出口恢复国内住宅 IP。
    // 这是进程级环境变量（首个 WebView2 环境创建时读取），对 wry 各窗口自身的
    // additional args 是追加语义，不影响 stealth 参数。
    #[cfg(target_os = "windows")]
    std::env::set_var(
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "--proxy-server=direct://",
    );
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let exe_path = env::current_exe().expect("无法获取当前可执行文件路径");

            // 安装路径诊断：PyInstaller sidecar 在含非 ASCII / 空格的路径下经常炸（README 已警告但缺主动防御）
            // 命中时把诊断信息 emit 给前端，由顶端横幅展示，不阻断启动
            let diag = analyze_install_path(&exe_path);
            if diag.path_has_non_ascii || diag.path_has_space || !diag.parent_writable {
                let app_handle = app.handle().clone();
                // 等前端首屏挂载好 listener；setup 阶段 window 已存在但 React 还没 render
                // 用独立线程 + 标准 sleep，不引入 tokio 依赖
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_millis(1500));
                    if let Some(window) = app_handle.get_webview_window("main") {
                        let _ = window.emit("backend-warning", &diag);
                    }
                });
            }

            // 检查 ffmpeg 是否在 PATH 中可用
            check_ffmpeg_availability();

            // 启动前清掉占用后端端口的孤儿进程：上次实例/更新未退干净的旧 sidecar
            // 还占着 8483 时，新 sidecar bind 失败立刻退出（退出码 1），横幅误报
            // 「后端进程已退出」——这正是孤儿场景，启动时就该自动处理而不是让用户点重启
            let startup_port: u16 = env::var("BACKEND_PORT")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(BACKEND_DEFAULT_PORT);
            kill_port_orphans(startup_port);

            // 启动 Sidecar 并把 child handle 存到 state，方便后续 restart_backend_sidecar 使用
            let child = spawn_backend_sidecar(app.handle()).map_err(|e| {
                eprintln!("Sidecar 启动失败: {}", e);
                e
            })?;
            app.manage(SidecarHandle(Mutex::new(Some(child))));

            // 启动 ready probe：异步轮询本地 BACKEND_PORT 是否在监听，
            // 解决前端 useCheckBackend 在 PyInstaller 解压期瞎猜后端起没起的问题。
            spawn_backend_ready_probe(app.handle().clone());

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_system_env_vars,
            find_executable_path,
            run_command_with_env,
            test_ffmpeg_access,
            get_install_path_diagnostics,
            restart_backend_sidecar,
            capture_site_cookie,
            fetch_bilibili_meta,
            fetch_hellotik_direct,
            fetch_xhs_collection_detail,
            close_xhs_windows,
            xhs_capture_window_open,
            open_xhs_passive_window
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        // 用 build()+run() 拿到 RunEvent 流，关键诉求：app 退出前必须 kill 掉 PyInstaller
        // sidecar，否则它会变成持有 8483 端口的孤儿进程，下次启动 VidInsight 直接 bind 失败。
        // 之前漏掉这一步导致用户 PID 96739 那种「上次没关干净 → 这次起不来」的死循环。
        .run(|app_handle, event| {
            match event {
                // ExitRequested 在用户 Cmd-Q / 点关闭 / Dock 退出时触发，先于实际进程结束。
                // Exit 是兜底——任何走到 Tauri 主循环结束的路径都会经过它。
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit => {
                    kill_backend_sidecar(app_handle);
                }
                _ => {}
            }
        });
}

// 关闭期统一杀 sidecar，take() 把 child 从 state 拿走避免重复 kill。
fn kill_backend_sidecar(app_handle: &tauri::AppHandle) {
    if let Some(state) = app_handle.try_state::<SidecarHandle>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.take() {
                eprintln!("[shutdown] killing backend sidecar before app exit");
                let _ = child.kill();
            }
        }
    }
}

// 获取额外的二进制路径
fn get_additional_binary_paths() -> Vec<String> {
    if cfg!(target_os = "windows") {
        vec![
            "C:\\ffmpeg\\bin".to_string(),
            "C:\\Program Files\\ffmpeg\\bin".to_string(),
            "C:\\Program Files (x86)\\ffmpeg\\bin".to_string(),
            "C:\\tools\\ffmpeg\\bin".to_string(),
            "C:\\ProgramData\\chocolatey\\bin".to_string(),
        ]
    } else if cfg!(target_os = "macos") {
        vec![
            "/usr/local/bin".to_string(),
            "/opt/homebrew/bin".to_string(),
            "/usr/bin".to_string(),
            "/bin".to_string(),
            "/opt/local/bin".to_string(), // MacPorts
        ]
    } else {
        vec![
            "/usr/local/bin".to_string(),
            "/usr/bin".to_string(),
            "/bin".to_string(),
            "/snap/bin".to_string(),
            "/opt/bin".to_string(),
            "/usr/local/sbin".to_string(),
        ]
    }
}

// 增强 PATH 环境变量
fn enhance_path_variable(current_path: &str, additional_paths: &[String]) -> String {
    let path_separator = if cfg!(target_os = "windows") { ";" } else { ":" };

    let mut paths: Vec<String> = additional_paths.to_vec();

    // 添加当前 PATH
    if !current_path.is_empty() {
        paths.push(current_path.to_string());
    }

    paths.join(path_separator)
}

// 检查 ffmpeg 可用性
fn check_ffmpeg_availability() {
    use std::process::Command;

    match Command::new("ffmpeg").arg("-version").output() {
        Ok(output) => {
            if output.status.success() {
                println!("✓ FFmpeg is available in PATH");
                let version_info = String::from_utf8_lossy(&output.stdout);
                let first_line = version_info.lines().next().unwrap_or("Unknown version");
                println!("FFmpeg version: {}", first_line);
            } else {
                println!("✗ FFmpeg found but returned error");
            }
        }
        Err(e) => {
            println!("✗ FFmpeg not found in PATH: {}", e);

            // 尝试在常见路径中查找
            let common_paths = get_additional_binary_paths();
            for path in common_paths {
                let ffmpeg_path = if cfg!(target_os = "windows") {
                    format!("{}\\ffmpeg.exe", path)
                } else {
                    format!("{}/ffmpeg", path)
                };

                if std::path::Path::new(&ffmpeg_path).exists() {
                    println!("✓ Found FFmpeg at: {}", ffmpeg_path);
                    return;
                }
            }
            println!("✗ FFmpeg not found in common installation paths");
        }
    }
}

// Tauri 命令：获取系统环境变量
#[tauri::command]
fn get_system_env_vars() -> HashMap<String, String> {
    env::vars().collect()
}

// Tauri 命令：查找可执行文件路径
#[tauri::command]
fn find_executable_path(executable_name: String) -> Option<String> {
    use std::process::Command;

    // 首先尝试直接执行
    if Command::new(&executable_name).arg("--version").output().is_ok() {
        return Some(executable_name);
    }

    // 使用 which/where 命令查找
    let which_cmd = if cfg!(target_os = "windows") { "where" } else { "which" };

    if let Ok(output) = Command::new(which_cmd).arg(&executable_name).output() {
        if output.status.success() {
            let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !path.is_empty() {
                return Some(path);
            }
        }
    }

    // 在常见路径中搜索
    let common_paths = get_additional_binary_paths();
    for base_path in common_paths {
        let executable_path = if cfg!(target_os = "windows") {
            format!("{}\\{}.exe", base_path, executable_name)
        } else {
            format!("{}/{}", base_path, executable_name)
        };

        if std::path::Path::new(&executable_path).exists() {
            return Some(executable_path);
        }
    }

    None
}

// Tauri 命令：使用完整环境变量运行命令
#[tauri::command]
async fn run_command_with_env(
    program: String,
    args: Vec<String>
) -> Result<String, String> {
    use std::process::Command;

    let mut cmd = Command::new(&program);
    cmd.args(&args);

    // 设置所有环境变量
    for (key, value) in env::vars() {
        cmd.env(key, value);
    }

    // 增强 PATH
    let current_path = env::var("PATH").unwrap_or_default();
    let additional_paths = get_additional_binary_paths();
    let enhanced_path = enhance_path_variable(&current_path, &additional_paths);
    cmd.env("PATH", enhanced_path);

    match cmd.output() {
        Ok(output) => {
            if output.status.success() {
                Ok(String::from_utf8_lossy(&output.stdout).to_string())
            } else {
                Err(String::from_utf8_lossy(&output.stderr).to_string())
            }
        }
        Err(e) => Err(format!("Failed to execute {}: {}", program, e))
    }
}

// Tauri 命令：测试 ffmpeg 访问
#[tauri::command]
async fn test_ffmpeg_access() -> Result<String, String> {
    run_command_with_env("ffmpeg".to_string(), vec!["-version".to_string()]).await
}

// 启动后端 Sidecar：负责装环境变量、spawn、挂 stdout/stderr/terminated 监听并 emit 给前端。
// 第一次启动 + restart_backend_sidecar 都走这里，保持单一启动路径。
fn spawn_backend_sidecar(app_handle: &tauri::AppHandle) -> Result<CommandChild, String> {
    let exe_path = env::current_exe().map_err(|e| format!("无法获取可执行文件路径: {}", e))?;
    let sidecar_dir = exe_path
        .parent()
        .ok_or("无法获取可执行文件父目录")?
        .to_path_buf();

    // 收集所有系统环境变量并增强 PATH（含 ffmpeg 常见安装位置）
    let mut all_env_vars = HashMap::new();
    for (key, value) in env::vars() {
        all_env_vars.insert(key, value);
    }
    let current_path = all_env_vars.get("PATH").cloned().unwrap_or_default();
    let additional_paths = get_additional_binary_paths();
    let enhanced_path = enhance_path_variable(&current_path, &additional_paths);
    all_env_vars.insert("PATH".to_string(), enhanced_path);

    let mut sidecar_command = app_handle
        .shell()
        .sidecar("VidInsightBackend")
        .map_err(|e| format!("找不到 VidInsightBackend sidecar: {}", e))?;
    for (key, value) in &all_env_vars {
        sidecar_command = sidecar_command.env(key, value);
    }

    let (mut rx, child) = sidecar_command
        .current_dir(sidecar_dir)
        .spawn()
        .map_err(|e| format!("spawn sidecar 失败: {}", e))?;

    // 异步监听 stdout / stderr / terminated 事件，转发到前端 webview
    let app_handle_for_listener = app_handle.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            // window 句柄每次重新取，允许窗口关闭重开
            let window = app_handle_for_listener.get_webview_window("main");
            match event {
                CommandEvent::Stdout(line) => {
                    let output = String::from_utf8_lossy(&line).to_string();
                    println!("Backend stdout: {}", output);
                    if let Some(w) = window {
                        let _ = w.emit("backend-message", Some(output));
                    }
                }
                CommandEvent::Stderr(line) => {
                    let error = String::from_utf8_lossy(&line).to_string();
                    eprintln!("Backend stderr: {}", error);
                    if let Some(w) = window {
                        let _ = w.emit("backend-error", Some(error));
                    }
                }
                CommandEvent::Terminated(payload) => {
                    println!("Backend terminated with code: {:?}", payload.code);
                    if let Some(w) = window {
                        let _ = w.emit("backend-terminated", Some(payload.code));
                    }
                    break;
                }
                _ => {
                    println!("Backend event: {:?}", event);
                }
            }
        }
    });

    Ok(child)
}

// 清掉占用后端端口的孤儿进程（典型成因：应用更新/重启时旧实例没退干净，
// 留下的 sidecar 还占着 8483，新 sidecar bind 不上立刻退出）。
// 用 PowerShell 精确按「监听该端口的进程」杀，不误伤无关进程。
fn kill_port_orphans(port: u16) {
    let script = format!(
        "Get-NetTCPConnection -LocalPort {} -State Listen -ErrorAction SilentlyContinue | \
         Select-Object -ExpandProperty OwningProcess -Unique | \
         ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }}",
        port
    );
    let _ = std::process::Command::new("powershell")
        .args(["-NoProfile", "-Command", &script])
        .creation_flags(0x08000000) // CREATE_NO_WINDOW
        .output();
}

// 重启 sidecar：清端口孤儿 → 杀旧 child → spawn 新 child，回写到 state。
#[tauri::command]
fn restart_backend_sidecar(
    state: State<'_, SidecarHandle>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    // 0. 先告诉前端「我们要重启了」。前端可以借此忽略接下来 N 秒内的 backend-terminated
    //    事件——那是我们主动 kill 老 sidecar 的副作用，不是真异常。否则会出现：
    //    terminated 事件延迟到达 → 覆盖掉 'running' 状态 → 面板永远显示「已退出」。
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.emit("backend-restarting", ());
    }
    // 0.5 先清掉占用端口的孤儿进程——不然新 sidecar bind 不上立刻退出，
    //     「一键重启」就变成无效操作（这正是横幅误报的成因之一）
    let port: u16 = env::var("BACKEND_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(BACKEND_DEFAULT_PORT);
    kill_port_orphans(port);
    // 1. 拿出旧 child 并 kill（kill 失败也继续，可能进程已经退了）
    {
        let mut guard = state.0.lock().map_err(|e| format!("锁 sidecar state 失败: {}", e))?;
        if let Some(child) = guard.take() {
            let _ = child.kill();
        }
    }
    // 2. 重新 spawn
    let new_child = spawn_backend_sidecar(&app)?;
    {
        let mut guard = state.0.lock().map_err(|e| format!("锁 sidecar state 失败: {}", e))?;
        *guard = Some(new_child);
    }
    // 3. emit 一个事件让前端知道已重启
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.emit("backend-restarted", ());
    }
    // 4. 重启后同样起一次 ready probe，让前端能及时退出失败态
    spawn_backend_ready_probe(app);
    Ok(())
}

// 后端就绪探测：异步轮询 GET /api/sys_check，要求 HTTP 200 才算就绪。
//
// 旧实现只做 TcpStream::connect_timeout——但端口被另一个孤儿 sidecar 占着时也会
// 连得通，导致 emit('backend-ready') 误判：前端进入主界面，但真正的新 sidecar
// 没 bind 上立刻就死，banner 永远停在「后端进程已退出」。
//
// 真发一个 HTTP 请求拿 200 才算「这是我们的后端在响应」。
fn spawn_backend_ready_probe(app: tauri::AppHandle) {
    let port: u16 = env::var("BACKEND_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(BACKEND_DEFAULT_PORT);
    let addr: SocketAddr = format!("127.0.0.1:{}", port).parse().expect("invalid backend addr");
    let timeout = Duration::from_secs(BACKEND_STARTUP_TIMEOUT_SECS);

    std::thread::spawn(move || {
        let start = Instant::now();
        let probe_interval = Duration::from_millis(500);
        loop {
            if probe_sys_check(&addr) {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.emit("backend-ready", port);
                    println!("Backend ready on port {} after {:?}", port, start.elapsed());
                }
                return;
            }
            if start.elapsed() >= timeout {
                if let Some(window) = app.get_webview_window("main") {
                    let payload = format!(
                        "后端在 {}s 内 /api/sys_check 未返回 200，疑似启动失败或端口 {} 被其他进程占用",
                        timeout.as_secs(),
                        port
                    );
                    let _ = window.emit("backend-startup-timeout", payload);
                    eprintln!(
                        "Backend startup timeout: /api/sys_check did not return 200 on 127.0.0.1:{} after {:?}",
                        port,
                        start.elapsed()
                    );
                }
                return;
            }
            std::thread::sleep(probe_interval);
        }
    });
}

// 极简 HTTP/1.0 GET /api/sys_check —— 用 std::net 手写避免引 reqwest/ureq 的重依赖。
// 任何错都视为「还没就绪」，下次 tick 再试。
fn probe_sys_check(addr: &SocketAddr) -> bool {
    let connect_timeout = Duration::from_millis(800);
    let rw_timeout = Duration::from_millis(1500);
    let mut stream = match TcpStream::connect_timeout(addr, connect_timeout) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(rw_timeout));
    let _ = stream.set_write_timeout(Some(rw_timeout));
    // HTTP/1.0 + Connection: close 让服务端发完响应就关，免去 chunked / keep-alive 解析
    let req = format!(
        "GET /api/sys_check HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        addr.port()
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    // 只要 status line，64 字节够了
    let mut buf = [0u8; 64];
    let n = match stream.read(&mut buf) {
        Ok(n) => n,
        Err(_) => return false,
    };
    let head = std::str::from_utf8(&buf[..n]).unwrap_or("");
    // 兼容 HTTP/1.0 / 1.1 起始行
    head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
}

// 安装路径诊断：PyInstaller 在含非 ASCII / 空格的路径下加载 _internal/* 经常炸；
// 父目录不可写时模型 / 配置 / 日志也无法落盘
#[derive(Serialize, Clone)]
struct InstallPathDiagnostics {
    exe_path: String,
    path_has_non_ascii: bool,
    path_has_space: bool,
    parent_writable: bool,
    platform: String,
}

fn analyze_install_path(exe_path: &Path) -> InstallPathDiagnostics {
    let path_str = exe_path.to_string_lossy().to_string();
    // 不在 ASCII 范围内的字符（中文 / 日文 / 西里尔等都会命中 PyInstaller 路径解析坑）
    let has_non_ascii = path_str.chars().any(|c| !c.is_ascii());
    // 空格本身在 Windows shell 引号场景偶尔出问题，且 macOS path 里也偶尔触发 sidecar 启动失败
    let has_space = path_str.contains(' ');
    // 父目录可写：PyInstaller 解压 _internal/、写日志、写配置都需要这个
    let parent = exe_path.parent();
    let parent_writable = parent
        .and_then(|p| {
            let probe = p.join(".vidinsight_write_probe");
            match std::fs::write(&probe, b"x") {
                Ok(_) => {
                    let _ = std::fs::remove_file(&probe);
                    Some(true)
                }
                Err(_) => Some(false),
            }
        })
        .unwrap_or(false);

    InstallPathDiagnostics {
        exe_path: path_str,
        path_has_non_ascii: has_non_ascii,
        path_has_space: has_space,
        parent_writable,
        platform: std::env::consts::OS.to_string(),
    }
}

// Tauri 命令：让前端按需重新查询诊断结果（比如用户卸载到新目录后重启）
#[tauri::command]
fn get_install_path_diagnostics() -> InstallPathDiagnostics {
    let exe_path = env::current_exe().unwrap_or_default();
    analyze_install_path(&exe_path)
}

// 可选：添加一个函数来动态更新 sidecar 的环境变量
#[tauri::command]
async fn update_sidecar_environment(
    app_handle: tauri::AppHandle,
    additional_env_vars: HashMap<String, String>
) -> Result<(), String> {
    // 这个函数可以用来在运行时更新环境变量
    // 注意：这需要重启 sidecar 才能生效

    for (key, value) in additional_env_vars {
        env::set_var(key, value);
    }

    Ok(())
}
