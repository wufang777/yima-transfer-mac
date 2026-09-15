# 易码互传 · Windows 版

与 macOS 版**同一套互通协议**，两台电脑（Mac ↔ Windows）在同一局域网内自动发现、互相传文件；手机端页面完全相同（扫码即用，无需装 App）。

## 协议对齐要点（改 Mac 端时务必同步这里）

| 层 | 约定 |
| --- | --- |
| HTTP | 端口 8000（可在设置改），端点与 Mac 版一致：`/upload?name=`、`/files`、`/download/<name>`、`/share/<id>`、`/api/peers`、`/api/scan`、`/api/peers/add`、`/api/share`、`/api/settings`、`/api/version`、`/api/local/*` |
| UDP 发现 | 端口 58887，每 3 秒广播 `{"app":"LanFileTransfer","id","name","port"}`（全局广播 + 逐网卡定向广播），超时 12 秒剔除 |
| HTTP 扫描发现 | 兜底通道：`GET /api/version` 探测本机各 /24 网段（3 秒内扫完 508 个地址），每 2 分钟刷新；手动添加走 `POST /api/peers/add {"ip":...}` |
| 互传方式 | 控制台「发文件」= 浏览器向 `http://对端IP:端口/upload?name=文件名` 直接 POST 原始字节 |
| 网页 | 复用仓库根目录 `console.html` / `index.html` / `settings.html`（单一代码库） |

## 互相发现（重要）

发现设备有两条通道，任一条通即可互传：

1. **UDP 广播自动发现**（默认）—— 需要防火墙放行入站 UDP 58887 / TCP 8000。
   首次运行请右键「以管理员身份运行」`防火墙放行.bat`（程序启动时也会尝试自动放行）。
   若路由器开了「AP 隔离 / 访客网络」，广播会被网络层丢弃。
2. **HTTP 扫描发现**（兜底，不依赖广播）—— 启动 3 秒后自动扫描本机各网段，之后每 2 分钟刷新；
   也可点托盘菜单「扫描局域网设备」手动触发。
   控制台「局域网设备」里还能直接填对方 IP 点「手动添加」，只要对方 8000 端口可达就能互传。


## 本地运行（开发）

```bash
pip install -r win/requirements.txt
python win/yima_win.py              # 托盘模式（Windows）
python win/yima_win.py --headless --port 8010   # 无托盘（Mac 上调试互通用）
```

## 构建 exe

无需 Windows 电脑 —— GitHub Actions 云端构建（`.github/workflows/build-windows.yml`）：

```bash
gh workflow run build-windows                 # 手动触发
gh run watch                                  # 等待完成
gh run download -n yima-transfer-win64 -D dist/win   # 取回 zip（内含 yima-transfer.exe）
```

推 `v*` tag 时也会自动构建。PyInstaller spec 见 `win/yima_win.spec`（onefile、无控制台窗口、内嵌三张网页与图标）。

## Windows 使用说明

1. 先把 `防火墙放行.bat` **右键 → 以管理员身份运行**一次（放行 UDP 58887 与 TCP 8000）
2. 双击 `yima-transfer.exe`（首次运行 Windows 可能弹防火墙提示 —— **务必勾选允许「专用网络」**）
3. 托盘出现马头图标 → 自动打开网页控制台
4. Mac 上打开易码互传控制台，「局域网设备」里会出现这台 Windows 机器，点「发文件」即可互传；Windows 控制台同理
   - 若没自动出现：点「重新扫描局域网」（扫描通道不依赖广播），或填对方 IP 点「手动添加」
5. 接收目录默认 `下载\易码互传`，可在设置页修改；开机自启在设置页开关（写入启动文件夹，不需要管理员权限）

## 已知边界

- Windows 版在线升级（`/api/local/update/*`）暂未实现，返回提示；后续版本接 OSS 清单后补齐
- 微信接收目录切换为 Mac 专属功能，Windows 版暂不提供（设置页该区域为空）
