# 易码互传 · 安卓原生客户端

Kotlin 原生（传统 View，代码构建 UI，iOS 风格），对接电脑端 HTTP 协议（见 ../docs/手机端API协议.md）。

## 功能
- 局域网自动发现：监听电脑端 UDP 58887 广播，单台自动连接、多台点选（`Discovery.kt`）
- 扫码 / 手输 IP:端口 连接电脑，历史电脑列表（长按删除）
- 发文字到电脑（电脑自动复制，附带本机设备名）
- 发文件：2MB 分片 + 断点续传 + 每片重试，前台 Service 通知栏进度，后台/锁屏不中断
- 电脑推送文字（3 秒轮询，仅前台；上报 did/name 作心跳，电脑端可定向推送给本机）
- 电脑分享文件 / 接收目录文件：点击下载到手机「下载」目录（MediaStore，无需存储权限）

## 构建
```bash
export JAVA_HOME=<jdk17>
export ANDROID_HOME=~/Library/Android/sdk
./gradlew assembleDebug      # 产物 app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease    # 未签名，需签名后才能安装
```
要求：JDK 17、SDK platform-35 + build-tools 35.0.0。

## 结构
- `app/src/main/java/com/yima/transfer/`
  - `Api.kt` — HTTP 协议封装（分片/断点/重试对齐服务端权威偏移）
  - `Discovery.kt` — UDP 58887 局域网电脑自动发现
  - `MainActivity.kt` — 连接页（自动发现/扫码/手输）+ 三 Tab 主界面（代码构建 UI）
  - `ScanActivity.kt` — CameraX + ZXing 扫码
  - `UploadService.kt` — 前台上传服务（dataTransfer 类型）
  - `Prefs.kt` — 连接信息 / 历史 / 消息游标 / 设备身份（did、deviceName）
- minSdk 29（Android 10+），targetSdk 35
