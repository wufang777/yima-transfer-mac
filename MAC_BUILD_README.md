# 易码互传 · macOS 原生版

> 基于 Windows 版功能重新规划的 Mac 专用版本。
> 壳层全部重写：菜单栏常驻（rumps 原生）+ 网页控制台，无 tkinter / 无 pystray。

## 功能（与 Windows 版对齐）

| 功能 | 说明 |
|------|------|
| 手机扫码互传 | 手机浏览器扫码上传/下载，无需装 App |
| 电脑 ↔ 电脑互传 | UDP 广播自动发现（端口 58887），控制台里选设备直接发 |
| 网页控制台 | 本机访问 `/console`：连接状态、二维码、文件管理、设备列表 |
| 智能分流 | 本机打开 `/` 进控制台；手机等其它设备打开 `/` 进手机页 |
| 分享文件给手机 | `POST /api/share`（Windows 版同款协议） |
| 设置 | 接收目录 / 端口 / 通知 / 自动打开目录 / 开机自启（`/settings`） |
| 菜单栏 | 显示二维码 / 打开控制台 / 打开接收目录 / 复制地址 / 设置 / 重启 / 退出 |
| 收文件通知 | macOS 系统通知；可配置改为自动打开接收目录 |

## 相比旧 Mac 版修复的问题

1. **IP 识别**：过滤代理 fake-IP（198.18.0.0/15）、CGNAT（100.64/10）等网段，
   优先物理网卡（en*）上的 192.168.x.x —— 开代理软件也能拿到正确地址
2. **单实例**：lsof 按端口精准定位 + 进程名匹配双保险（旧版 pgrep/kill 不生效）
3. **壳层稳定性**：去掉 tkinter 窗口判定 hack 和 pystray run_detached ——
   「打开无界面」这类问题的根源已整体移除

## 运行

- **双击 `dist/易码互传.app`**（建议拖入「应用程序」文件夹）
- 首次打开（未签名）：右键 → 打开 → 再点「打开」；或 `xattr -cr /Applications/易码互传.app`
- 首次运行需要允许两个系统权限弹窗：
  - **防火墙/本地网络**（不允许可在 系统设置 → 隐私与安全性 → 本地网络 中补开）
  - 通知权限（不想收通知可在设置页关闭）
- 启动后会自动打开控制台页面 `http://localhost:8000/console`（仅本机可访问）

## 终端模式（调试用）

```bash
python3 app.py --console        # Ctrl+C 停止，二维码显示在终端
python3 app.py --no-open        # 菜单栏模式但不自动打开浏览器
python3 app.py --install-autostart / --uninstall-autostart
```

## 打包

```bash
bash build_mac.sh    # 产出 dist/易码互传.app + dist/易码互传.dmg
```

依赖：rumps、segno、Pillow、pyobjc-framework-Cocoa（脚本自动安装）。
注意：部分环境下 pip 安装 rumps（sdist 包）会报 EEXIST，此时手动安装：
`pip install segno Pillow pyobjc-framework-Cocoa && pip install rumps`（或从 tar.gz 解压 rumps/ 目录到 site-packages）。

打包踩坑（已解决，重打包遇到报错先看这里）：

1. **必须在非沙箱环境运行**：PyInstaller 在 macOS 上会对产物执行 `codesign --remove`，
   受限沙箱会拒绝该操作并报 `Operation not permitted`，导致打包中断。
2. **先清残留**：上次打包留下的已签名二进制会让 codesign 重签失败。
   `build_mac.sh` 已内置清理（`rm -rf build/易码互传 dist/易码互传.app`）。
3. **若 .app 被占用**：先退出正在运行的实例（菜单栏图标 → 退出），否则替换会提示「正在使用中」。
4. **dmg 生成报「无此文件或目录」**：检查是否有同名旧卷挂载着（`hdiutil info | grep 易码`），
   先 `hdiutil detach` 再重试；脚本会在临时目录组装「应用 + Applications 快捷方式」后打包。

## 数据位置

配置 / 日志 / 二维码 / 接收目录（默认）：
`~/Library/Application Support/易码互传/`

## 架构要点（改代码前必读）

- `app.py` 单文件约 900 行：品牌常量 → 配置 → 网络核心（HTTP + UDP 发现，与 Windows 版协议完全一致）→ 菜单栏（rumps）→ main()
- `/upload` 协议：原始 body + `?name=<urlencoded>`，不是 multipart（前后端及 Windows 版一致）
- 控制台专属接口 `/api/local/*` 带本机校验：客户端 IP 需为回环地址或**本机网卡地址**
  （浏览器访问自身局域网 IP 时来源地址是局域网 IP 而非 127.0.0.1，故两者都视为本机）；
  其它设备访问返回 403。菜单栏「打开控制台/设置」固定用 `http://localhost:<port>` 打开
- 局域网设备发现（UDP 58887）：需在「系统设置 → 隐私与安全性 → 本地网络」中允许本应用，
  否则能绑定端口但收不到广播（表现为设备列表为空）
- PC 发文件给局域网设备：控制台页面直接 `fetch` 到对方 `/upload`（浏览器直发，服务端不中转）
- IP 识别：`ifconfig` 解析 + 评分（en* 优先、192.168 优先），排除保留网段；每 5 秒看门狗自动刷新二维码
- 打包用 `--onedir`（PyInstaller 已弃用 onefile+windowed 组合）；打包后必须覆盖 Info.plist 并 `codesign --force --sign -` 重签
- 日志：`~/Library/Application Support/易码互传/易码互传.log`，启动/单实例/IP 变化/退出均有记录

---
易码互传 · © 易码通科技 · 2026-09-10
