package com.yima.transfer

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * 手机端 HTTP 协议封装（对应电脑端 app.py / yima_win.py）。
 * base 形如 http://192.168.1.5:8000
 */
object Api {
    const val CHUNK_SIZE = 2 * 1024 * 1024   // 2MB 分片，与服务端协议一致

    /** 探活 + 取版本；失败抛异常 */
    fun version(base: String): JSONObject =
        getJson(base, "/api/version")

    fun info(base: String): JSONObject =
        getJson(base, "/info")

    /** 发文字到电脑（电脑自动复制）；from = 本机设备名，便于电脑端区分多台手机 */
    fun sendText(base: String, text: String, from: String = "手机"): JSONObject =
        postJson(base, "/api/text/receive", JSONObject().put("text", text).put("from", from))

    /**
     * 电脑推送的文字（after=上次最大 id）。
     * did/name 同时作为在线心跳：电脑端据此把本机列进「推送到手机」的设备列表，
     * 并只下发「广播 + 发给自己」的消息。
     */
    fun phoneMessages(base: String, after: Long, did: String = "", name: String = ""): JSONObject {
        var path = "/api/text/phone/messages?after=$after"
        if (did.isNotBlank()) {
            path += "&did=${urlEnc(did)}"
            if (name.isNotBlank()) path += "&name=${urlEnc(name)}"
        }
        return getJson(base, path)
    }

    /** 电脑分享给手机的文件 */
    fun shared(base: String): JSONArray =
        getArray(base, "/api/shared")

    /** 电脑接收目录里的文件 */
    fun files(base: String): JSONArray =
        getArray(base, "/files")

    /** 上传初始化：同名同大小未完成 → 断点续传；done=true → 秒传 */
    fun uploadInit(base: String, name: String, size: Long): JSONObject =
        postJson(base, "/upload/init", JSONObject().put("name", name).put("size", size))

    /** 上传一个分片；409 时按服务端权威偏移对齐；成功返回新 offset */
    fun uploadChunk(base: String, id: String, offset: Long, buf: ByteArray, len: Int): Long {
        val url = URL("$base/upload/chunk?id=${urlEnc(id)}&offset=$offset")
        val conn = url.openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 15000
            conn.readTimeout = 60000
            // 服务端按 Content-Length 读分片；若用 chunked 编码它拿不到长度会直接判"空分片"并断连
            conn.setFixedLengthStreamingMode(len.toLong())
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/octet-stream")
            conn.getOutputStream().use { os: OutputStream -> os.write(buf, 0, len) }
            val code = conn.responseCode
            if (code == 409) {
                val j = JSONObject(readStream(conn.errorStream))
                return j.optLong("offset", offset)
            }
            if (code !in 200..299) throw RuntimeException("HTTP $code")
            val j = JSONObject(readStream(conn.inputStream))
            if (!j.optBoolean("ok")) throw RuntimeException(j.optString("error", "分片上传失败"))
            return j.optLong("offset", offset)
        } finally {
            conn.disconnect()
        }
    }

    fun uploadComplete(base: String, id: String): JSONObject =
        postJson(base, "/upload/complete", JSONObject().put("id", id))

    /** 通用下载流（下载到手机用），返回打开的连接（调用方负责读流与断开） */
    fun openDownload(base: String, path: String): HttpURLConnection {
        val conn = URL(base + path).openConnection() as HttpURLConnection
        conn.connectTimeout = 15000
        conn.readTimeout = 60000
        return conn
    }

    // ---------------- 基础工具 ----------------

    private fun getJson(base: String, path: String): JSONObject =
        withHttp(base + path) { JSONObject(readStream(it.inputStream)) }

    private fun getArray(base: String, path: String): JSONArray =
        withHttp(base + path) { JSONArray(readStream(it.inputStream)) }

    private inline fun <T> withHttp(u: String, block: (HttpURLConnection) -> T): T {
        val conn = URL(u).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 6000
            conn.readTimeout = 20000
            return block(conn)
        } finally {
            conn.disconnect()
        }
    }

    private fun postJson(base: String, path: String, body: JSONObject): JSONObject {
        val conn = URL(base + path).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 8000
            conn.readTimeout = 30000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            return JSONObject(readStream(conn.inputStream))
        } finally {
            conn.disconnect()
        }
    }

    private fun readStream(s: java.io.InputStream?): String {
        if (s == null) return ""
        return s.use { it.readBytes().toString(Charsets.UTF_8) }
    }

    fun urlEnc(s: String): String = java.net.URLEncoder.encode(s, "utf-8")

    suspend fun <T> io(block: () -> T): T = withContext(Dispatchers.IO) { block() }
}
