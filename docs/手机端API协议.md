# 易码互传 · 手机端 HTTP 协议（安卓 App 对接依据）

全部 API 走电脑端 base URL：`http://<ip>:<port>`（默认端口 8000）。
二维码内容即连接地址：`http://<ip>:<port>/connect?name=<手机名>`。

## 1. 连接 / 探活

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/version` | 探活。返回 `{version, build, version_line, app, company, ...}` |
| GET | `/info` | 连接信息。返回 `{url, dir}`（电脑地址、接收目录） |

## 2. 文字互传

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/text/receive` | JSON `{text, from:"手机"}`（上限 1 万字；`from` 建议传本机设备名，便于电脑端区分多台手机） | `{ok}` 电脑收到并自动复制 |
| GET | `/api/text/phone/messages?after=<id>&did=<设备ID>&name=<设备名>` | `did`/`name` 可省略（旧版兼容） | `{ok, messages:[{id, text, ts, did?}]}`（ts 秒级时间戳；3s 轮询） |

`did`/`name` 同时是**在线心跳**（30 秒无心跳即离线）与**定向依据**：

- 带 `did` 拉取：只返回「广播消息（无 did）+ 发给本机（did 相同）」的消息
- 不带 `did` 拉取（旧版）：返回全部消息，保持兼容
- 电脑端据此维护在线手机表，`GET /api/phones`（仅本机）返回 `{ok, phones:[{did, name, ip, last_seen}]}`
- 推送入口：`POST /api/text/phone` JSON `{text, did?}`；`did` 省略 = 广播给所有手机，指定则只发给该设备（目标离线返回 404）

## 2.1 局域网自动发现（可选能力）

电脑端每 3 秒向 UDP **58887** 广播：

```json
{"app":"LanFileTransfer","id":"<实例ID>","name":"<主机名>","port":8000}
```

手机端只需监听该端口（`SO_REUSEADDR`），收到即得到 `http://<来源IP>:<port>`，无需发送探测包。
鸿蒙 App（`Discovery.ets`）与安卓 App（`Discovery.kt`）均按此实现；未收到广播时回退扫码 / 手输。

## 3. 文件上传（分片 + 断点续传，CHUNK=2MB）

1. `POST /upload/init` JSON `{name, size}` → `{ok, id, offset, done}`
   - `done=true`：电脑已有同名同大小文件，秒传
   - `offset>0`：断点续传起点
2. `POST /upload/chunk?id=<id>&offset=<offset>` body=`application/octet-stream`（原始字节）
   - 成功 → `{ok, offset}`（新的权威偏移）
   - **409 冲突** → JSON 带 `{offset}`，直接对齐后继续
   - 每片重试 4 次，重试前重新 init 对齐进度
3. `POST /upload/complete` JSON `{id}` → `{ok, size}`（服务端校验大小后落盘）

## 4. 电脑上的文件 / 分享给手机

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/files` | `[{name, size}]` 电脑接收目录列表 |
| GET | `/download/<urlencoded name>?inline=1` | 下载/预览 |
| DELETE | `/delete/<urlencoded name>` | 删除 |
| GET | `/api/shared` | `[{id, name, size, folder}]` 电脑主动分享的文件（folder=建议保存目录） |
| GET | `/share/<id>?inline=1` | 下载分享文件 |

## 5. 安卓 App 约定

- 连接信息、历史电脑列表存 SharedPreferences
- 上传跑在前台 Service（通知栏进度，锁屏不中断）
- 收推送文字：app 在前台时轮询（3s），后台降频或停
- 下载到手机：存公共 Downloads 目录（MediaStore.Downloads）
