# VidInsight 桌面端打包说明

本项目的桌面端采用 Tauri：

- 前端：`vidinsight-frontend` 的 React 应用，打包为本地 WebView 页面。
- 后端：`backend` 的 FastAPI 应用，通过 PyInstaller 打包为 Tauri sidecar。
- 运行方式：用户打开 `VidInsight.exe` 后，Tauri 自动启动本地后端 `VidInsightBackend`，前端请求 `http://127.0.0.1:8483/api`。

## 1. 环境准备

Windows 推荐安装：

```bat
python --version
node --version
pnpm --version
rustc -V
cargo -V
```

如果缺少 Rust/Cargo，需要先安装 Rust：

```bat
winget install Rustlang.Rustup
```

重新打开终端后检查：

```bat
rustc -V
cargo -V
```

后端依赖和 PyInstaller 都放在项目根目录 `.venv` 中，不使用系统 Python 环境：

```bat
setup_python_env.bat
```

该脚本会创建：

```text
.venv\Scripts\python.exe
```

并把 `backend\requirements.txt` 里的依赖安装到项目本地环境。

前端依赖：

```bat
cd vidinsight-frontend
pnpm install
```

## 2. 构建后端 sidecar

在项目根目录执行：

```bat
cd backend
build.bat
```

`build.bat` 会强制使用：

```text
..\.venv\Scripts\python.exe
```

如果 `.venv` 不存在，会提示先运行 `setup_python_env.bat`。

成功后会生成类似目录：

```text
vidinsight-frontend\src-tauri\bin\VidInsightBackend\
```

其中应包含：

```text
VidInsightBackend-x86_64-pc-windows-msvc.exe
_internal\
```

## 3. 构建桌面安装包

```bat
cd vidinsight-frontend
pnpm.cmd tauri:build
```

如果 PowerShell 禁止运行 `pnpm.ps1`，使用 `pnpm.cmd`，不要直接用 `pnpm`。

打包产物通常在：

```text
vidinsight-frontend\src-tauri\target\release\bundle\
```

## 4. 开发调试

桌面端前端开发模式：

```bat
cd vidinsight-frontend
pnpm.cmd tauri:dev
```

注意：`tauri:dev` 也需要 sidecar 已经存在。如果提示找不到 `VidInsightBackend`，先执行 `backend\build.bat`。

## 5. 常见问题

### 5.1 找不到 cargo

说明 Rust 工具链没有安装，或者安装后没有重新打开终端。

### 5.2 找不到 VidInsightBackend sidecar

先执行：

```bat
cd backend
build.bat
```

再执行：

```bat
cd ..\vidinsight-frontend
pnpm.cmd tauri:build
```

### 5.3 PowerShell 禁止 pnpm.ps1

用：

```bat
pnpm.cmd tauri:build
```

### 5.4 中文路径问题

PyInstaller sidecar 在中文路径、空格路径下更容易出问题。正式打包建议把项目放到：

```text
C:\VidInsight
```

或：

```text
D:\VidInsight
```

### 5.5 端口被占用

桌面端默认使用后端端口 `8483`。如果启动失败，检查是否已有旧后端进程占用该端口。

```bat
netstat -ano | findstr :8483
```

## 6. 当前桌面端改造点

- Tauri 产品名已改为 `VidInsight`。
- Tauri identifier 已改为 `com.no-professor.vidinsight`。
- sidecar 名称已改为 `VidInsightBackend`。
- Tauri 模式前端请求地址固定为 `http://127.0.0.1:8483/api`。
- 已新增 `tauri:dev` 和 `tauri:build` 脚本。
- 发布窗口默认关闭 devtools。
