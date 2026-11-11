# 易码互传 · 鸿蒙版（HarmonyOS NEXT）

手机 ⇄ 电脑 局域网互传客户端，ArkTS + ArkUI 原生实现，功能与安卓版对齐。

## 功能清单（与安卓版 v1.15.0 对齐）

| 模块 | 说明 |
|---|---|
| 连接电脑 | 扫一扫（ScanKit，QR 内容 `http://ip:port/connect?name=xx`）/ 手动输入 IP / 最近连接（长按移除） |
| 发文字 | POST `/api/text/receive`，电脑自动复制到剪贴板 |
| 收消息 | 3 秒轮询 `/api/text/phone/messages`，未读角标、冷启动拉历史、一键清空 |
| 发文件 | 2MB 分片 + 断点续传（409 对齐服务端偏移）+ 每片重试 4 次；队列串行、可取消、可重试 |
| 后台传输 | backgroundTaskManager 长时任务（dataTransfer 模式）+ 通知栏进度 |
| 下载电脑文件 | `/download/<name>`、`/share/<id>`，流式下载带实时进度，DocumentSavePicker 落盘 |
| 复制 | pasteboard 一键复制消息文字 |

## 工程结构

```
harmony/
├── AppScope/                  应用级配置（bundleName: com.yima.transfer, 1.15.0）
├── build-profile.json5        SDK 5.0.0(12) / debug+release
├── entry/
│   └── src/main/
│       ├── module.json5       权限：INTERNET + KEEP_BACKGROUND_RUNNING；后台模式 dataTransfer
│       ├── ets/
│       │   ├── entryability/EntryAbility.ets
│       │   ├── model/Api.ets           # HTTP 协议层（对齐 docs/手机端API协议.md）
│       │   ├── model/Prefs.ets         # preferences 持久化
│       │   ├── model/UploadManager.ets # 上传队列/断点续传/后台任务/通知
│       │   └── pages/Index.ets         # 主界面（连接页 + 三 Tab）
│       └── resources/         图标、颜色、字符串、页面路由
```

## 构建步骤（需 DevEco Studio）

1. 到 https://developer.huawei.com/consumer/cn/deveco-studio/ 登录华为账号下载 macOS 版并安装到 /Applications
2. 首次启动 DevEco Studio（同意 SDK 组件下载，SDK 内置于 IDE）
3. 命令行构建（或直接 IDE 打开 harmony/ 目录）：
   ```bash
   # hvigorw 在 DevEco Studio 安装目录 tools/hvigor/bin 下
   cd harmony
   ~/Applications/DevEco-Studio.app/contents/tools/hvigor/bin/hvigorw assembleHap --mode module -p product=default debuggable=true
   # 产物：entry/build/default/outputs/default/entry-default-unsigned.hap
   ```
4. 签名（需要华为开发者账号 + 实名认证，AGC 创建证书/Profile 后填入 build-profile.json5 的 signingConfigs）

## 已知注意点

- **签名是硬门槛**：鸿蒙连 debug 包都强制签名，真机安装前必须完成 AGC 证书
- 服务端分片接口只认 `Content-Length` 定长，ArkTS 端用 ArrayBuffer 作为 extraData（网络库自动设置长度），**不要**改成流式/chunked
- `docs/手机端API协议.md` 是协议事实标准，改接口先改它
- 首次编译若个别 API 名有出入（SDK 版本差异），以 DevEco Studio 报错提示为准修正
- ScanKit 扫码在模拟器上不可用，需真机验证
