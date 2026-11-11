package com.yima.transfer

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/** 连接信息 + 历史电脑列表（SharedPreferences） */
object Prefs {
    private fun sp(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences("yima", Context.MODE_PRIVATE)

    /** 当前连接的电脑 base，如 http://192.168.1.5:8000；空=未连接 */
    fun base(ctx: Context): String = sp(ctx).getString("base", "") ?: ""

    fun setBase(ctx: Context, base: String) {
        sp(ctx).edit().putString("base", base).apply()
    }

    fun clearBase(ctx: Context) {
        sp(ctx).edit().remove("base").apply()
    }

    /** 已收到的推送文字最大 id（增量轮询用） */
    fun lastMsgId(ctx: Context): Long = sp(ctx).getLong("lastMsgId", 0L)

    fun setLastMsgId(ctx: Context, id: Long) {
        sp(ctx).edit().putLong("lastMsgId", id).apply()
    }

    /** 历史电脑：[{base, name, ts}] */
    fun history(ctx: Context): JSONArray =
        JSONArray(sp(ctx).getString("history", "[]"))

    fun addHistory(ctx: Context, base: String, name: String) {
        val arr = history(ctx)
        val out = JSONArray()
        // 去重（同 base 只留最新），最多保留 8 台
        out.put(JSONObject().put("base", base).put("name", name).put("ts", System.currentTimeMillis()))
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            if (o.optString("base") != base && out.length() < 8) out.put(o)
        }
        sp(ctx).edit().putString("history", out.toString()).apply()
    }

    fun removeHistory(ctx: Context, base: String) {
        val arr = history(ctx)
        val out = JSONArray()
        for (i in 0 until arr.length()) {
            if (arr.getJSONObject(i).optString("base") != base) out.put(arr.getJSONObject(i))
        }
        sp(ctx).edit().putString("history", out.toString()).apply()
    }

    /**
     * 本机设备身份（多手机场景下电脑端定向推送用）。
     * did 首次随机生成并持久化；name 取厂商+型号，回退「手机-XXXX」。
     */
    fun deviceId(ctx: Context): String {
        val p = sp(ctx)
        val saved = p.getString("deviceId", "") ?: ""
        if (saved.isNotBlank()) return saved
        val did = String.format(Locale.US, "%08x", (Math.random() * 0xffffffffL).toLong())
        p.edit().putString("deviceId", did).apply()
        return did
    }

    fun deviceName(ctx: Context): String {
        var name = ""
        try {
            val mf = Build.MANUFACTURER.orEmpty().trim()
            val model = Build.MODEL.orEmpty().trim()
            name = when {
                model.isEmpty() -> mf
                mf.isEmpty() || model.lowercase(Locale.US).startsWith(mf.lowercase(Locale.US)) -> model
                else -> "$mf $model"
            }
        } catch (_: Exception) {
            name = ""
        }
        if (name.isBlank()) name = "手机-" + deviceId(ctx)
        return if (name.length > 24) name.substring(0, 24) else name
    }
}
