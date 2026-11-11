package com.yima.transfer

import android.Manifest
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 易码互传 · 安卓客户端（Kotlin 原生，iOS 风格 UI）
 * 未连接 → 连接页（扫码 / 手输 / 历史）；已连接 → 三 Tab（首页 / 消息 / 文件）
 */
class MainActivity : AppCompatActivity() {

    private object C {
        val blue = Color.parseColor("#007AFF")
        val bg = Color.parseColor("#F2F2F7")
        val text = Color.parseColor("#1C1C1E")
        val text2 = Color.parseColor("#8E8E93")
        val sep = Color.parseColor("#E5E5EA")
        val fill = Color.parseColor("#E9E9EB")
    }

    private val scope = CoroutineScope(Dispatchers.Main)
    private val handler = Handler(Looper.getMainLooper())
    private var base: String = ""

    private lateinit var root: FrameLayout
    private lateinit var titleStatus: TextView
    private lateinit var pageHome: ScrollView
    private lateinit var pageMsgs: ScrollView
    private lateinit var pageFiles: ScrollView
    private lateinit var msgList: LinearLayout
    private lateinit var sharedList: LinearLayout
    private lateinit var pcFilesList: LinearLayout
    private lateinit var uploadList: LinearLayout
    private lateinit var textInput: EditText
    private lateinit var tabViews: List<TextView>

    private val uploadRows = HashMap<String, UploadRow>()
    private val uploadUris = HashMap<String, Uri>()
    private var currentTab = 0
    private var unread = 0
    private var firstMsgLoad = true

    /** 局域网自动发现：base -> (名称, IP) */
    private val foundPcs = LinkedHashMap<String, Pair<String, String>>()
    private var discovering = false
    private var connecting = false
    private var autoConnectUsed = false
    private var autoConnectTask: Runnable? = null
    private var discStatus: TextView? = null
    private var discList: LinearLayout? = null

    /** 一行上传进度：百分比 / 进度条 / 操作按钮（取消、重试） */
    private class UploadRow(val pct: TextView, val pb: ProgressBar, val act: TextView, val tip: TextView)

    private val pollMsgs = object : Runnable {
        override fun run() {
            if (base.isNotBlank()) loadMessages()
            handler.postDelayed(this, 3000)
        }
    }

    private val pickFile =
        registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
            if (uris.isNullOrEmpty()) return@registerForActivityResult
            if (base.isBlank()) { toast("请先连接电脑"); return@registerForActivityResult }
            for (u in uris) {
                val nm = displayName(u)
                uploadUris[nm] = u
                UploadService.startUpload(this, base, u)
            }
            gotoTab(0)
        }

    private val scanLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
            val b = r.data?.getStringExtra(ScanActivity.EXTRA_BASE)
            if (r.resultCode == RESULT_OK && !b.isNullOrBlank()) tryConnect(b)
        }

    private val permLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

    private val uploadEvt = object : BroadcastReceiver() {
        override fun onReceive(c: Context?, i: Intent?) {
            i ?: return
            val name = i.getStringExtra(UploadService.EVT_NAME) ?: return
            val state = i.getStringExtra(UploadService.EVT_STATE) ?: return
            val sent = i.getLongExtra(UploadService.EVT_SENT, 0)
            val size = i.getLongExtra(UploadService.EVT_SIZE, 0)
            updateUploadRow(name, state, sent, size)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        base = Prefs.base(this)
        root = FrameLayout(this)
        root.setBackgroundColor(C.bg)
        setContentView(root)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            permLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        registerReceiver(uploadEvt, IntentFilter(UploadService.EVT), RECEIVER_NOT_EXPORTED)
        if (base.isNotBlank()) enterMain() else showConnect()
    }

    override fun onResume() {
        super.onResume()
        handler.post(pollMsgs)
        if (base.isNotBlank()) refreshFiles() else if (discStatus != null) startAutoDiscover()
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacks(pollMsgs)
        stopAutoDiscover()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopAutoDiscover()
        unregisterReceiver(uploadEvt)
    }

    // ================= 连接页 =================

    private fun showConnect() {
        root.removeAllViews()
        foundPcs.clear()
        autoConnectUsed = false
        discStatus = null
        discList = null
        root.addView(connectScreen())
        startAutoDiscover()
    }

    private fun connectScreen(): View {
        val sv = ScrollView(this)
        val v = column(24)
        sv.addView(v)

        v.addView(label("易码互传", 30f, bold = true, center = true).apply { setPadding(0, dp(90), 0, dp(6)) })
        v.addView(label("手机 ⇄ 电脑 · 局域网互传", 15f, color = C.text2, center = true).apply { setPadding(0, 0, 0, dp(48)) })

        v.addView(bigButton("扫一扫 · 连接电脑") {
            scanLauncher.launch(Intent(this, ScanActivity::class.java))
        })

        // 局域网自动发现：电脑每 3 秒广播一次，收到即可一键连接
        val discRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        }
        discStatus = label("正在搜索局域网电脑…", 13f, color = C.text2)
        discRow.addView(discStatus, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        discRow.addView(TextView(this).apply {
            text = "重新搜索"; textSize = 13f; setTextColor(C.blue)
            setPadding(dp(12), dp(8), 0, dp(8))
            setOnClickListener { rescanLan() }
        })
        v.addView(discRow.apply { setPadding(0, dp(14), 0, 0) })
        discList = column(0)
        v.addView(discList)

        v.addView(label("或", 13f, color = C.text2, center = true).apply { setPadding(0, dp(18), 0, dp(18)) })

        // 手动输入
        val card = cardView()
        val cv = column(0)
        cv.addView(label("手动输入电脑地址", 13f, color = C.text2))
        val addr = EditText(this).apply {
            hint = "192.168.1.5:8000"
            setSingleLine()
            background = round(C.fill, 10f)
            setPadding(dp(12), dp(10), dp(12), dp(10))
        }
        cv.addView(addr.apply { lp(this, topMargin = 8) })
        cv.addView(smallButton("连接", blue = true) {
            val a = addr.text.toString().trim()
            if (a.isBlank()) { toast("请输入地址"); return@smallButton }
            tryConnect(if (a.startsWith("http")) a else "http://$a")
        }.apply { lp(this, topMargin = 12) })
        card.addView(cv)
        v.addView(card)

        // 历史电脑
        val hist = Prefs.history(this)
        if (hist.length() > 0) {
            v.addView(label("最近连接", 18f, bold = true).apply { setPadding(0, dp(32), 0, dp(12)) })
            val hcard = cardView()
            val hv = column(0)
            for (i in 0 until hist.length()) {
                val o = hist.getJSONObject(i)
                val name = o.optString("name").ifBlank { o.optString("base") }
                hv.addView(clickableRow(name, subtitle = o.optString("base")) {
                    tryConnect(o.optString("base"))
                }.apply {
                    setOnLongClickListener {
                        Prefs.removeHistory(this@MainActivity, o.optString("base"))
                        showConnect(); true
                    }
                })
                if (i < hist.length() - 1) hv.addView(sep())
            }
            hcard.addView(hv)
            v.addView(hcard)
        }
        return sv
    }

    // ---- 局域网自动发现 ----

    private fun startAutoDiscover() {
        if (base.isNotBlank() || discovering) return
        discovering = true
        renderDiscStatus("正在搜索局域网电脑…")
        val ok = Discovery.start { b, name ->
            if (base.isNotBlank() || connecting) return@start
            if (!foundPcs.containsKey(b)) {
                val ip = b.removePrefix("http://").substringBefore(":")
                foundPcs[b] = name to ip
                renderFound()
            }
            maybeAutoConnect()
        }
        if (!ok) {
            // 端口被占等异常：静默回退到扫码 / 手动输入
            discovering = false
            renderDiscStatus("未开启自动发现，可扫码或手动输入地址")
        }
    }

    private fun stopAutoDiscover() {
        autoConnectTask?.let { handler.removeCallbacks(it) }
        autoConnectTask = null
        if (!discovering) return
        discovering = false
        Discovery.stop()
    }

    /** 只发现一台 → 自动连；多台 → 等用户点选 */
    private fun maybeAutoConnect() {
        if (autoConnectUsed || autoConnectTask != null) return
        if (base.isNotBlank() || connecting || foundPcs.size != 1) return
        val task = Runnable {
            autoConnectTask = null
            if (!autoConnectUsed && foundPcs.size == 1 && base.isBlank() && !connecting) {
                autoConnectUsed = true
                val only = foundPcs.entries.first()
                renderDiscStatus("发现电脑「${only.value.first}」，正在连接…")
                tryConnect(only.key)
            }
        }
        autoConnectTask = task
        // 等 1.2 秒：若同网段还有第二台电脑，避免抢错
        handler.postDelayed(task, 1200)
    }

    private fun rescanLan() {
        stopAutoDiscover()
        foundPcs.clear()
        autoConnectUsed = false
        discList?.removeAllViews()
        startAutoDiscover()
    }

    private fun renderDiscStatus(text: String) {
        val t = discStatus ?: return
        runOnUiThread { t.text = text }
    }

    private fun renderFound() {
        val box = discList ?: return
        runOnUiThread {
            box.removeAllViews()
            if (foundPcs.isEmpty()) return@runOnUiThread
            val card = cardView()
            val cv = column(0)
            cv.addView(label("局域网内发现的电脑", 13f, color = C.text2))
            for ((b, v2) in foundPcs) {
                val rowV = LinearLayout(this).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = Gravity.CENTER_VERTICAL
                    setPadding(dp(4), dp(12), dp(4), dp(12))
                }
                val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
                col.addView(TextView(this).apply {
                    text = v2.first; textSize = 15f; setTextColor(C.text)
                    maxLines = 1; ellipsize = TextUtils.TruncateAt.MIDDLE
                })
                col.addView(TextView(this).apply {
                    text = b; textSize = 13f; setTextColor(C.text2)
                })
                rowV.addView(col, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                rowV.addView(TextView(this).apply {
                    text = "连接"; textSize = 14f; setTextColor(C.blue)
                    setPadding(dp(12), dp(6), 0, dp(6))
                })
                rowV.setOnClickListener { tryConnect(b) }
                cv.addView(rowV.apply { lp(this, topMargin = 6) })
            }
            card.addView(cv)
            box.addView(card)
            renderDiscStatus(if (foundPcs.size == 1) "发现 1 台电脑" else "发现 ${foundPcs.size} 台电脑，点选连接")
        }
    }

    private fun tryConnect(b: String) {
        if (connecting) return
        connecting = true
        toast("正在连接…")
        scope.launch {
            try {
                val ver = withContext(Dispatchers.IO) { Api.version(b) }
                base = b
                Prefs.setBase(this@MainActivity, b)
                Prefs.addHistory(this@MainActivity, b, ver.optString("name", "电脑"))
                stopAutoDiscover()
                runOnUiThread {
                    connecting = false
                    enterMain(); toast("已连接")
                }
            } catch (e: Exception) {
                runOnUiThread {
                    connecting = false
                    toast("连接失败：${e.message}")
                    if (discStatus != null) startAutoDiscover()
                }
            }
        }
    }

    // ================= 主界面 =================

    private fun enterMain() {
        root.removeAllViews()
        root.addView(mainScreen())
        titleStatus.text = "已连接 · $base"
        scope.launch {
            try {
                val info = withContext(Dispatchers.IO) { Api.info(base) }
                runOnUiThread { titleStatus.text = "已连接 · ${info.optString("url", base)}" }
            } catch (_: Exception) {}
        }
        refreshFiles()
    }

    private fun mainScreen(): View {
        val main = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }

        val head = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        head.setPadding(dp(24), dp(18), dp(24), 0)
        head.addView(label("易码互传", 28f, bold = true))
        titleStatus = label("已连接", 14f, color = C.text2)
        head.addView(titleStatus.apply { lp(this, topMargin = 4) })
        main.addView(head)

        val pages = FrameLayout(this)
        pageHome = homePage()
        pageMsgs = msgsPage()
        pageFiles = filesPage()
        pages.addView(pageHome)
        pages.addView(pageMsgs)
        pages.addView(pageFiles)
        pageMsgs.visibility = View.GONE
        pageFiles.visibility = View.GONE
        main.addView(pages, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))

        main.addView(tabBar())
        return main
    }

    private fun tabBar(): View {
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(Color.WHITE)
        }
        val names = listOf("首页", "消息", "文件")
        val icons = listOf("⌂", "✉", "📦")
        tabViews = names.mapIndexed { i, n ->
            val t = TextView(this).apply {
                text = "$icons\n$n"
                gravity = Gravity.CENTER
                textSize = 11f
                setLineSpacing(0f, 1.2f)
                setPadding(0, dp(7), 0, dp(7))
                setTextColor(if (i == 0) C.blue else C.text2)
                setTypeface(null, Typeface.BOLD)
            }
            bar.addView(t, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            t.setOnClickListener { gotoTab(i) }
            t
        }
        return bar
    }

    private fun gotoTab(i: Int) {
        currentTab = i
        listOf(pageHome, pageMsgs, pageFiles).forEachIndexed { j, v ->
            v.visibility = if (i == j) View.VISIBLE else View.GONE
        }
        tabViews.forEachIndexed { j, t -> t.setTextColor(if (i == j) C.blue else C.text2) }
        if (i == 1) { unread = 0; setBadge(); loadMessages() }
        if (i == 2) refreshFiles()
    }

    private fun setBadge() {
        if (tabViews.size < 2) return
        tabViews[1].text = if (unread > 0) "✉ $unread\n消息" else "✉\n消息"
    }

    // ---- 首页 ----

    private fun homePage(): ScrollView {
        val sv = ScrollView(this)
        sv.setPadding(0, dp(8), 0, dp(16))
        val v = column(20)
        sv.addView(v)

        // 连接信息
        val c1 = cardView()
        val c1v = column(0)
        c1v.addView(label("电脑连接", 20f, bold = true))
        val addrRow = row2("地址", base)
        c1v.addView(addrRow.apply { lp(this, topMargin = 10) })
        val verRow = row2("版本", "…")
        c1v.addView(verRow.apply { lp(this, topMargin = 6) })
        c1v.addView(row2("本机名称", Prefs.deviceName(this)).apply { lp(this, topMargin = 6) })
        c1v.addView(label("电脑端「文字」页的推送列表里可看到本机", 12f, color = C.text2).apply {
            lp(this, topMargin = 6)
        })
        c1v.addView(smallButton("断开连接", blue = false) {
            Prefs.clearBase(this@MainActivity); base = ""; showConnect()
        }.apply { lp(this, topMargin = 14) })
        c1.addView(c1v)
        v.addView(c1)

        scope.launch {
            try {
                val ver = withContext(Dispatchers.IO) { Api.version(base) }
                runOnUiThread {
                    (verRow.getChildAt(1) as? TextView)?.text = ver.optString("version_line", "-")
                }
            } catch (_: Exception) {}
        }

        // 发文字
        val c2 = cardView()
        val c2v = column(0)
        c2v.addView(label("发文字到电脑", 20f, bold = true))
        c2v.addView(label("发送后电脑自动复制到剪贴板", 13f, color = C.text2).apply { lp(this, topMargin = 4) })
        textInput = EditText(this).apply {
            hint = "网址、验证码、临时笔记…"
            minLines = 3
            gravity = Gravity.TOP
            background = round(C.fill, 10f)
            setPadding(dp(12), dp(10), dp(12), dp(10))
        }
        c2v.addView(textInput.apply { lp(this, topMargin = 12) })
        c2v.addView(smallButton("发送", blue = true) { sendText() }.apply { lp(this, topMargin = 12) })
        c2.addView(c2v)
        v.addView(c2.apply { lp(this, topMargin = 14) })

        // 发文件
        val c3 = cardView()
        val c3v = column(0)
        c3v.addView(label("发文件到电脑", 20f, bold = true))
        c3v.addView(label("分片传输 · 断点续传 · 后台不中断", 13f, color = C.text2).apply { lp(this, topMargin = 4) })
        c3v.addView(bigButton("选择文件发送") { pickFile.launch(arrayOf("*/*")) }.apply { lp(this, topMargin = 12) })
        uploadList = column(0)
        c3v.addView(uploadList)
        c3.addView(c3v)
        v.addView(c3.apply { lp(this, topMargin = 14) })

        v.addView(label("版权所有：易码通科技", 12f, color = C.text2, center = true).apply {
            setPadding(0, dp(24), 0, 0)
        })
        return sv
    }

    private fun sendText() {
        val txt = textInput.text.toString()
        if (txt.isBlank()) { toast("请先输入内容"); return }
        if (txt.length > 10000) { toast("内容过长（上限 1 万字）"); return }
        scope.launch {
            try {
                val r = withContext(Dispatchers.IO) {
                    Api.sendText(base, txt, Prefs.deviceName(this@MainActivity))
                }
                if (r.optBoolean("ok")) {
                    runOnUiThread { textInput.setText(""); toast("已发送，电脑已收到并复制") }
                } else runOnUiThread { toast("发送失败：${r.optString("error")}") }
            } catch (e: Exception) {
                runOnUiThread { toast("发送失败：网络错误") }
            }
        }
    }

    private fun updateUploadRow(name: String, state: String, sent: Long, size: Long) {
        var row = uploadRows[name]
        if (row == null) {
            val wrap = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(0, dp(14), 0, 0)
            }

            val head = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            }
            head.addView(TextView(this).apply {
                text = name; textSize = 14f; setTextColor(C.text); maxLines = 1
                ellipsize = TextUtils.TruncateAt.MIDDLE
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            })
            val pct = TextView(this).apply {
                textSize = 13f; setTextColor(C.blue); setTypeface(null, Typeface.BOLD)
            }
            head.addView(pct)
            wrap.addView(head)

            val pb = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
                max = 100
                progress = 0
                progressTintList = android.content.res.ColorStateList.valueOf(C.blue)
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, dp(4)
                ).apply { topMargin = dp(8) }
            }
            wrap.addView(pb)

            val foot = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            }
            val tip = TextView(this).apply { textSize = 12f; setTextColor(C.text2) }
            foot.addView(tip, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            val act = TextView(this).apply {
                textSize = 13f; setTextColor(C.blue)
                setPadding(dp(12), dp(8), 0, dp(8))
            }
            foot.addView(act)
            wrap.addView(foot)

            uploadList.addView(wrap)
            row = UploadRow(pct, pb, act, tip)
            uploadRows[name] = row
        }
        val percent = if (size > 0) (sent * 100 / size).toInt() else 0
        when (state) {
            "queued" -> {
                row.pct.text = "排队中"
                row.pb.progress = 0
                row.tip.text = fmt(size)
                setAction(row, "取消") { cancelUpload(name) }
            }
            "sending" -> {
                row.pct.text = "$percent%"
                row.pb.progress = percent
                row.tip.text = "${fmt(sent)} / ${fmt(size)}"
                setAction(row, "取消") { cancelUpload(name) }
            }
            "done" -> {
                row.pct.text = "已完成"
                row.pb.progress = 100
                row.tip.text = fmt(size)
                setAction(row, "") {}
            }
            "instant" -> {
                row.pct.text = "已完成"
                row.pb.progress = 100
                row.tip.text = "电脑上已有同文件（秒传）"
                setAction(row, "") {}
            }
            "canceled" -> {
                row.pct.text = "已取消"
                row.tip.text = "已传部分保留，重试可续传"
                setAction(row, "重试") { retryUpload(name) }
            }
            else -> {
                row.pct.text = "已中断"
                row.tip.text = "可点重试续传"
                setAction(row, "重试") { retryUpload(name) }
            }
        }
    }

    private fun setAction(row: UploadRow, text: String, onClick: () -> Unit) {
        row.act.text = text
        row.act.visibility = if (text.isBlank()) View.GONE else View.VISIBLE
        row.act.setOnClickListener { onClick() }
    }

    private fun cancelUpload(name: String) {
        uploadUris[name]?.let { UploadService.cancel(this, it) }
    }

    private fun retryUpload(name: String) {
        val u = uploadUris[name] ?: return
        if (base.isBlank()) { toast("连接已断开，请重新连接电脑"); return }
        toast("继续上传…")
        UploadService.startUpload(this, base, u)
    }

    private fun displayName(u: Uri): String {
        var n = "file"
        contentResolver.query(u, null, null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val i = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                if (i >= 0) n = c.getString(i)
            }
        }
        return n
    }

    // ---- 消息页 ----

    private fun msgsPage(): ScrollView {
        val sv = ScrollView(this)
        sv.setPadding(0, dp(8), 0, dp(16))
        val v = column(20)
        sv.addView(v)
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(label("电脑推送的文字", 22f, bold = true),
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        head.addView(smallButton("清空", blue = false) {
            msgList.removeAllViews()
            msgList.addView(emptyHint("暂无消息"))
        })
        v.addView(head.apply { setPadding(0, dp(8), 0, dp(14)) })
        msgList = column(0)
        v.addView(msgList)
        v.addView(label("每 3 秒自动刷新 · 仅在前台轮询", 12f, color = C.text2, center = true).apply {
            setPadding(0, dp(20), 0, 0)
        })
        return sv
    }

    private fun loadMessages() {
        scope.launch {
            try {
                // 首次进入（冷启动）拉最近 50 条做历史；之后只拉新增
                // did/name 同时作为心跳：电脑端「推送到手机」列表据此识别本机
                val after = if (firstMsgLoad) 0L else Prefs.lastMsgId(this@MainActivity)
                val did = Prefs.deviceId(this@MainActivity)
                val devName = Prefs.deviceName(this@MainActivity)
                val r = withContext(Dispatchers.IO) { Api.phoneMessages(base, after, did, devName) }
                firstMsgLoad = false
                if (!r.optBoolean("ok")) return@launch
                val msgs = r.optJSONArray("messages") ?: return@launch
                if (msgs.length() == 0) {
                    if (msgList.childCount == 0) runOnUiThread { msgList.addView(emptyHint("暂无消息")) }
                    return@launch
                }
                runOnUiThread {
                    if (msgList.childCount == 1 && msgList.getChildAt(0).tag == "empty") msgList.removeAllViews()
                    var fresh = 0
                    for (i in 0 until msgs.length()) {
                        val m = msgs.getJSONObject(i)
                        val id = m.optLong("id", 0)
                        msgList.addView(msgCard(m), 0)
                        if (id > Prefs.lastMsgId(this@MainActivity)) Prefs.setLastMsgId(this@MainActivity, id)
                        fresh++
                    }
                    if (fresh > 0 && currentTab != 1) { unread += fresh; setBadge() }
                }
            } catch (_: Exception) {}
        }
    }

    private fun msgCard(m: JSONObject): View {
        val card = cardView()
        val v = column(0)
        v.addView(TextView(this).apply {
            text = m.optString("text"); textSize = 15f; setTextColor(C.text)
            setTextIsSelectable(true)
        })
        val bottom = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        bottom.addView(label(fmtTime(m.optLong("ts") * 1000), 12f, color = C.text2),
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        bottom.addView(smallButton("复制", blue = false) { copyText(m.optString("text")) }.apply {
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { leftMargin = dp(10) }
        })
        v.addView(bottom.apply { lp(this, topMargin = 10) })
        card.addView(v)
        lp(card, bottomMargin = 12)
        return card
    }

    // ---- 文件页 ----

    private fun filesPage(): ScrollView {
        val sv = ScrollView(this)
        sv.setPadding(0, dp(8), 0, dp(16))
        val v = column(20)
        sv.addView(v)
        val head = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        }
        head.addView(label("文件", 22f, bold = true),
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        head.addView(smallButton("刷新", blue = false) {
            toast("正在刷新…"); refreshFiles()
        })
        v.addView(head.apply { setPadding(0, dp(8), 0, dp(14)) })

        v.addView(label("电脑分享给我的", 16f, bold = true))
        sharedList = column(0)
        val sc = cardView(); sc.addView(sharedList)
        v.addView(sc.apply { lp(this, topMargin = 10) })

        v.addView(label("电脑接收目录", 16f, bold = true).apply { lp(this, topMargin = 24) })
        pcFilesList = column(0)
        val fc = cardView(); fc.addView(pcFilesList)
        v.addView(fc.apply { lp(this, topMargin = 10) })

        v.addView(label("点击文件即可下载到手机「下载」目录", 12f, color = C.text2, center = true).apply {
            setPadding(0, dp(20), 0, 0)
        })
        return sv
    }

    private fun refreshFiles() {
        if (base.isBlank()) return
        scope.launch {
            try {
                val shared = withContext(Dispatchers.IO) { Api.shared(base) }
                runOnUiThread { renderList(sharedList, shared, share = true) }
            } catch (_: Exception) {}
            try {
                val files = withContext(Dispatchers.IO) { Api.files(base) }
                runOnUiThread { renderList(pcFilesList, files, share = false) }
            } catch (_: Exception) {}
        }
    }

    private fun renderList(container: LinearLayout, arr: JSONArray, share: Boolean) {
        container.removeAllViews()
        if (arr.length() == 0) {
            container.addView(emptyHint(if (share) "电脑尚未分享文件" else "暂无文件"))
            return
        }
        for (i in 0 until arr.length()) {
            val f = arr.getJSONObject(i)
            val name = f.optString("name")
            val size = f.optLong("size", 0)
            val path = if (share) "/share/" + Api.urlEnc(f.optString("id"))
                       else "/download/" + Api.urlEnc(name)
            container.addView(clickableRow(name, subtitle = fmt(size)) { downloadToPhone(path, name) })
            if (i < arr.length() - 1) container.addView(sep())
        }
    }

    private fun downloadToPhone(path: String, name: String) {
        val bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            progress = 0
            progressTintList = android.content.res.ColorStateList.valueOf(C.blue)
        }
        val tv = label("准备下载…", 13f, color = C.text2)
        val box = column(20).apply {
            addView(tv)
            addView(bar, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(6)
            ).apply { topMargin = dp(10) })
        }
        val dlg = AlertDialog.Builder(this)
            .setTitle("下载到手机")
            .setView(box)
            .setCancelable(false)
            .create()
        dlg.show()

        scope.launch {
            try {
                var saved: Uri? = null
                withContext(Dispatchers.IO) {
                    val conn = Api.openDownload(base, path)
                    if (conn.responseCode !in 200..299) throw RuntimeException("HTTP ${conn.responseCode}")
                    val total = conn.contentLengthLong
                    val mime = conn.contentType ?: "application/octet-stream"
                    val (destUri, os) = createDownloadTarget(name, mime)
                    var done = 0L
                    var lastUi = 0L
                    os.use { out ->
                        conn.inputStream.use { ins ->
                            val buf = ByteArray(1 shl 16)
                            while (true) {
                                val n = ins.read(buf)
                                if (n < 0) break
                                out.write(buf, 0, n)
                                done += n
                                val now = System.currentTimeMillis()
                                if (now - lastUi > 200) {
                                    lastUi = now
                                    val p = if (total > 0) (done * 100 / total).toInt() else -1
                                    val d = done
                                    runOnUiThread {
                                        if (p >= 0) {
                                            bar.progress = p
                                            tv.text = "$p% · ${fmt(d)} / ${fmt(total)}"
                                        } else tv.text = fmt(d)
                                    }
                                }
                            }
                        }
                    }
                    saved = destUri
                }
                dlg.dismiss()
                val u = saved
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("下载完成")
                    .setMessage("已保存到手机「下载」目录：\n$name")
                    .setPositiveButton("打开") { _, _ -> u?.let { openFile(it) } }
                    .setNegativeButton("关闭", null)
                    .show()
            } catch (e: Exception) {
                dlg.dismiss()
                toast("下载失败：${e.message}")
            }
        }
    }

    /** 生成下载落盘目标：Android 10+ 走 MediaStore，老系统写公共下载目录 */
    private fun createDownloadTarget(name: String, mime: String): Pair<Uri, java.io.OutputStream> {
        if (Build.VERSION.SDK_INT >= 29) {
            val values = android.content.ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, mime)
            }
            val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw RuntimeException("无法创建下载文件")
            val os = contentResolver.openOutputStream(uri) ?: throw RuntimeException("无法写入下载文件")
            return uri to os
        }
        val dir = android.os.Environment.getExternalStoragePublicDirectory(
            android.os.Environment.DIRECTORY_DOWNLOADS
        )
        dir.mkdirs()
        val f = java.io.File(dir, name)
        return Uri.fromFile(f) to java.io.FileOutputStream(f)
    }

    private fun openFile(u: Uri) {
        try {
            val mime = contentResolver.getType(u) ?: "*/*"
            startActivity(
                Intent(Intent.ACTION_VIEW).setDataAndType(u, mime)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            )
        } catch (e: Exception) {
            toast("没有可打开该文件的应用")
        }
    }

    // ================= 视图工具 =================

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun round(color: Int, radiusDp: Float): GradientDrawable = GradientDrawable().apply {
        setColor(color)
        cornerRadius = radiusDp * resources.displayMetrics.density
    }

    private fun column(paddingDp: Int): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(paddingDp), dp(paddingDp), dp(paddingDp), dp(paddingDp))
    }

    private fun cardView(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        background = round(Color.WHITE, 12f)
        setPadding(dp(16), dp(16), dp(16), dp(16))
    }

    private fun label(text: String, size: Float, bold: Boolean = false, color: Int = C.text,
                      center: Boolean = false): TextView =
        TextView(this).apply {
            this.text = text
            textSize = size
            setTextColor(color)
            setTypeface(null, if (bold) Typeface.BOLD else Typeface.NORMAL)
            if (center) gravity = Gravity.CENTER_HORIZONTAL
        }

    private fun bigButton(text: String, onClick: () -> Unit): Button = Button(this).apply {
        this.text = text
        setTextColor(Color.WHITE)
        textSize = 16f
        background = round(C.blue, 12f)
        setPadding(dp(16), dp(14), dp(16), dp(14))
        stateListAnimator = null
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    private fun smallButton(text: String, blue: Boolean, onClick: () -> Unit): Button = Button(this).apply {
        this.text = text
        setTextColor(if (blue) Color.WHITE else C.blue)
        textSize = 14f
        background = round(if (blue) C.blue else C.fill, 8f)
        setPadding(dp(18), dp(9), dp(18), dp(9))
        minHeight = 0
        minimumHeight = 0
        stateListAnimator = null
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    private fun clickableRow(title: String, subtitle: String, onClick: () -> Unit): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        }
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        col.addView(TextView(this).apply {
            text = title; textSize = 15f; setTextColor(C.text); maxLines = 1
            ellipsize = TextUtils.TruncateAt.MIDDLE
        })
        col.addView(TextView(this).apply {
            text = subtitle; textSize = 13f; setTextColor(C.text2); maxLines = 1
        })
        row.addView(col, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        row.addView(TextView(this).apply {
            text = "⇩"; setTextColor(C.blue); textSize = 18f; setPadding(dp(8), 0, 0, 0)
        })
        row.setPadding(dp(16), dp(12), dp(16), dp(12))
        row.setOnClickListener { onClick() }
        return row
    }

    private fun row2(k: String, v: String): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            addView(TextView(this@MainActivity).apply { text = k; textSize = 14f; setTextColor(C.text2) })
            addView(TextView(this@MainActivity).apply {
                text = v; textSize = 14f; setTextColor(C.text)
                gravity = Gravity.RIGHT
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            })
        }

    private fun sep(): View = View(this).apply {
        setBackgroundColor(C.sep)
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)).apply {
            leftMargin = dp(16); rightMargin = dp(16)
        }
    }

    private fun emptyHint(text: String): TextView = label(text, 14f, color = C.text2, center = true).apply {
        setPadding(0, dp(24), 0, dp(24)); tag = "empty"
    }

    private fun lp(v: View, topMargin: Int = 0, bottomMargin: Int = 0, leftMargin: Int = 0) {
        val p = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        p.topMargin = dp(topMargin); p.bottomMargin = dp(bottomMargin); p.leftMargin = dp(leftMargin)
        v.layoutParams = p
    }

    private fun fmt(n: Long): String = android.text.format.Formatter.formatShortFileSize(this, n)

    private fun fmtTime(millis: Long): String =
        SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(millis))

    private fun copyText(text: String) {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("yima", text))
        toast("已复制")
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
