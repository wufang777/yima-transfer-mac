package com.yima.transfer

import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 局域网电脑自动发现。
 *
 * 电脑端（app.py / yima_win.py）每 3 秒向 UDP 58887 广播：
 *   {"app":"LanFileTransfer","id":...,"name":主机名,"port":8000}
 * 手机端只需监听该端口即可拿到电脑地址，无需发送探测包。
 */
object Discovery {
    const val PORT = 58887

    private var socket: DatagramSocket? = null
    private var worker: Thread? = null
    private val running = AtomicBoolean(false)
    private val main = Handler(Looper.getMainLooper())

    fun isRunning(): Boolean = running.get()

    /**
     * 开始监听（重复调用只保留一份）。
     * @return false 表示端口绑定失败（被占用等），调用方应回退到扫码/手动输入。
     */
    fun start(onBeacon: (base: String, name: String) -> Unit): Boolean {
        if (running.get()) return true
        val s = try {
            DatagramSocket(null).apply {
                reuseAddress = true
                broadcast = true
                bind(InetSocketAddress("0.0.0.0", PORT))
                soTimeout = 1500
            }
        } catch (_: Exception) {
            return false
        }
        socket = s
        running.set(true)
        val t = Thread {
            val buf = ByteArray(2048)
            while (running.get()) {
                try {
                    val pkt = DatagramPacket(buf, buf.size)
                    s.receive(pkt)
                    val text = String(pkt.data, 0, pkt.length, Charsets.UTF_8)
                    val obj = try {
                        JSONObject(text)
                    } catch (_: Exception) {
                        null
                    } ?: continue
                    if (obj.optString("app") != "LanFileTransfer") continue
                    val port = obj.optInt("port", 0)
                    val ip = pkt.address?.hostAddress ?: ""
                    if (port !in 1..65535 || ip.isBlank()) continue
                    val name = obj.optString("name").ifBlank { "电脑" }
                    val b = "http://$ip:$port"
                    main.post { if (running.get()) onBeacon(b, name) }
                } catch (_: SocketTimeoutException) {
                    // 超时属正常：继续等下一条广播
                } catch (_: Exception) {
                    if (running.get()) break
                }
            }
        }.apply {
            isDaemon = true
            name = "yima-discovery"
            start()
        }
        worker = t
        return true
    }

    /** 停止监听并释放端口 */
    fun stop() {
        running.set(false)
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        worker = null
    }
}
