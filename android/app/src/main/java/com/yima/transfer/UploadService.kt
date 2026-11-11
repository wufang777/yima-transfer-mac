package com.yima.transfer

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import java.io.InputStream

/**
 * 前台服务：文件分片上传（2MB/片 + 断点续传 + 每片重试），通知栏显示进度。
 * 多选文件时排队串行上传，避免多路抢带宽；传错可取消，失败可重试。
 */
class UploadService : Service() {

    companion object {
        const val CHANNEL_ID = "upload"
        const val NOTIF_ID = 1
        const val EVT = "com.yima.transfer.UPLOAD_EVENT"
        const val EVT_NAME = "name"
        const val EVT_STATE = "state"       // queued / sending / done / failed / canceled / instant
        const val EVT_SENT = "sent"
        const val EVT_SIZE = "size"
        const val EVT_ERROR = "error"

        private const val ACTION_CANCEL_ONE = "com.yima.transfer.CANCEL_ONE"
        private const val ACTION_CANCEL_ALL = "com.yima.transfer.CANCEL_ALL"

        /** 发起上传：外部把文件 Uri 交给服务 */
        fun startUpload(ctx: Context, base: String, uri: Uri) {
            val i = Intent(ctx, UploadService::class.java)
                .putExtra("base", base)
                .putExtra("uri", uri)
            ctx.startForegroundService(i)
        }

        /** 取消某个文件的上传（排队中直接移除，传输中在分片边界停下） */
        fun cancel(ctx: Context, uri: Uri) {
            val i = Intent(ctx, UploadService::class.java)
                .setAction(ACTION_CANCEL_ONE)
                .putExtra("uri", uri)
            ctx.startService(i)
        }

        /** 取消全部 */
        fun cancelAll(ctx: Context) {
            val i = Intent(ctx, UploadService::class.java).setAction(ACTION_CANCEL_ALL)
            ctx.startService(i)
        }
    }

    private data class Job(val base: String, val uri: Uri, val name: String, val size: Long)

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val lock = Any()
    private val queue = ArrayList<Job>()          // 待上传（含正在传的第一个）
    private val canceled = HashSet<String>()      // 被取消的 uri 字符串
    private var workerActive = false

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "文件传输", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 取消动作
        when (intent?.action) {
            ACTION_CANCEL_ONE -> {
                val u = intent.getParcelableExtra<Uri>("uri")?.toString() ?: return START_NOT_STICKY
                synchronized(lock) {
                    canceled.add(u)
                    queue.firstOrNull { it.uri.toString() == u }?.let { j ->
                        if (j !== queue.firstOrNull() || !workerActive) {
                            queue.remove(j)
                            evt(j.name, "canceled", 0, j.size, null)
                        }
                    }
                }
                if (queue.isEmpty()) finishUp()
                return START_NOT_STICKY
            }
            ACTION_CANCEL_ALL -> {
                synchronized(lock) {
                    for (j in queue) { canceled.add(j.uri.toString()); evt(j.name, "canceled", 0, j.size, null) }
                    queue.clear()
                }
                finishUp()
                return START_NOT_STICKY
            }
        }

        val base = intent?.getStringExtra("base")?.takeIf { it.isNotBlank() } ?: Prefs.base(this)
        val uri = intent?.getParcelableExtra<Uri>("uri")
        if (base.isBlank() || uri == null) {
            if (queue.isEmpty()) stopSelf()
            return START_NOT_STICKY
        }

        val meta = queryMeta(uri)
        val job = Job(base, uri, meta.first, meta.second)
        startForeground(NOTIF_ID, buildNotification(job.name, "准备上传…", 0, job.size, true))
        synchronized(lock) {
            if (queue.none { it.uri.toString() == job.uri.toString() }) {
                queue.add(job)
                evt(job.name, "queued", 0, job.size, null)
            }
        }
        pump()
        return START_NOT_STICKY
    }

    /** 单工作协程串行消费队列 */
    private fun pump() {
        synchronized(lock) {
            if (workerActive) return
            workerActive = true
        }
        scope.launch {
            val snapshot = synchronized(lock) { ArrayList(queue) }
            snapshot.forEachIndexed { i, job ->
                val key = job.uri.toString()
                val wasCanceled = synchronized(lock) { canceled.contains(key) }
                if (wasCanceled) {
                    synchronized(lock) { queue.remove(job); canceled.remove(key) }
                    evt(job.name, "canceled", 0, job.size, null)
                } else {
                    doUpload(job, i + 1, snapshot.size)
                    synchronized(lock) {
                        queue.remove(job)
                        canceled.remove(key)
                    }
                }
            }
            val more = synchronized(lock) {
                workerActive = false
                queue.isNotEmpty()
            }
            if (more) pump() else finishUp()
        }
    }

    private fun finishUp() {
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun queryMeta(uri: Uri): Pair<String, Long> {
        var name = "file"
        var size = 0L
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val iN = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val iS = c.getColumnIndex(OpenableColumns.SIZE)
                if (iN >= 0) name = c.getString(iN)
                if (iS >= 0 && !c.isNull(iS)) size = c.getLong(iS)
            }
        }
        return name to size
    }

    private fun evt(name: String, state: String, sent: Long, size: Long, error: String?) {
        sendBroadcast(
            Intent(EVT).setPackage(packageName)
                .putExtra(EVT_NAME, name)
                .putExtra(EVT_STATE, state)
                .putExtra(EVT_SENT, sent)
                .putExtra(EVT_SIZE, size)
                .putExtra(EVT_ERROR, error)
        )
    }

    private fun isCanceled(uri: Uri): Boolean =
        synchronized(lock) { canceled.contains(uri.toString()) }

    private fun doUpload(job: Job, index: Int, total: Int) {
        val (base, uri, name, size) = job
        fun note(text: String, sent: Long) {
            val nm = getSystemService(NotificationManager::class.java)
            val prefix = if (total > 1) "($index/$total) " else ""
            nm.notify(NOTIF_ID, buildNotification(name, prefix + text, sent, size, sent < size))
        }
        try {
            if (size <= 0) throw RuntimeException("无法获取文件大小")

            // 1) init：秒传 / 断点
            val init = Api.uploadInit(base, name, size)
            if (!init.optBoolean("ok")) throw RuntimeException(init.optString("error", "初始化失败"))
            if (init.optBoolean("done")) {
                evt(name, "instant", size, size, null)
                note("已完成（电脑上已有同文件）", size)
                return
            }
            var offset = init.optLong("offset", 0L)
            val id = init.optString("id")
            evt(name, "sending", offset, size, null)

            // 2) 逐片上传
            val buf = ByteArray(Api.CHUNK_SIZE)
            contentResolver.openInputStream(uri)?.use { ins: InputStream ->
                var skip = offset
                while (skip > 0) {          // 断点：先跳过已传部分
                    val s = ins.skip(skip)
                    if (s <= 0) break
                    skip -= s
                }
                while (offset < size) {
                    if (isCanceled(uri)) {
                        evt(name, "canceled", offset, size, null)
                        note("已取消（已传部分保留，重发同文件可续传）", offset)
                        return
                    }
                    val want = minOf(Api.CHUNK_SIZE.toLong(), size - offset).toInt()
                    var rd = 0
                    while (rd < want) {     // 读满一个分片
                        val n = ins.read(buf, rd, want - rd)
                        if (n < 0) throw RuntimeException("文件读取中断")
                        rd += n
                    }
                    var ok = false
                    var lastErr: Exception? = null
                    for (attempt in 0 until 4) {
                        try {
                            offset = Api.uploadChunk(base, id, offset, buf, rd)
                            ok = true; break
                        } catch (e: Exception) {
                            lastErr = e
                            note("网络波动，重试中…", offset)
                            Thread.sleep(800L * (attempt + 1))
                            // 对齐服务端权威进度
                            try {
                                val st = Api.uploadInit(base, name, size)
                                if (st.optBoolean("ok")) offset = st.optLong("offset", offset)
                            } catch (_: Exception) {}
                        }
                    }
                    if (!ok) throw (lastErr ?: RuntimeException("分片上传失败"))
                    note("${fmt(offset)} / ${fmt(size)}", offset)
                    evt(name, "sending", offset, size, null)
                }
            } ?: throw RuntimeException("无法读取文件")

            // 3) complete
            val fin = Api.uploadComplete(base, id)
            if (!fin.optBoolean("ok")) throw RuntimeException(fin.optString("error", "完成校验失败"))
            evt(name, "done", size, size, null)
            note("已完成 · ${fmt(size)}", size)
        } catch (e: Exception) {
            Log.e("UploadService", "upload failed", e)
            evt(name, "failed", 0, size, e.message ?: "未知错误")
            val nm = getSystemService(NotificationManager::class.java)
            nm.notify(NOTIF_ID, buildNotification(name, "中断：${e.message}（可点重试续传）", 0, size, false))
        }
    }

    private fun buildNotification(
        name: String, text: String, sent: Long, size: Long, ongoing: Boolean
    ): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val b = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("发送「$name」")
            .setContentText(text)
            .setOngoing(ongoing)
            .setContentIntent(open)
            .setOnlyAlertOnce(true)
        if (size > 0) b.setProgress(100, (sent * 100 / size).toInt(), false)
        return b.build()
    }

    private fun fmt(n: Long): String = android.text.format.Formatter.formatFileSize(this, n)

    override fun onDestroy() {
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null
}
