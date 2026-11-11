package com.yima.transfer

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader
import java.util.concurrent.Executors

/** 扫电脑上的连接二维码；解析出 http://ip:port 后回传 MainActivity */
class ScanActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_BASE = "base"
    }

    private lateinit var previewView: PreviewView
    private val exec = Executors.newSingleThreadExecutor()
    private var done = false

    private val perm = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        if (it) startCamera() else {
            Toast.makeText(this, "需要相机权限才能扫码，也可手动输入地址", Toast.LENGTH_LONG).show()
            finish()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = FrameLayout(this)
        root.setBackgroundColor(0xFF000000.toInt())
        previewView = PreviewView(this)
        previewView.layoutParams = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        root.addView(previewView)
        val tip = TextView(this).apply {
            text = "将二维码放入框内，自动识别"
            setTextColor(0xFFFFFFFF.toInt())
            textSize = 15f
            gravity = android.view.Gravity.CENTER_HORIZONTAL
            setPadding(0, 120, 0, 0)
        }
        root.addView(tip, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        setContentView(root)

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED) startCamera()
        else perm.launch(Manifest.permission.CAMERA)
    }

    private fun startCamera() {
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            val provider = providerFuture.get()
            val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
            analysis.setAnalyzer(exec, ::analyze)
            try {
                provider.unbindAll()
                provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
            } catch (e: Exception) {
                Toast.makeText(this, "相机启动失败：${e.message}", Toast.LENGTH_LONG).show()
                finish()
            }
        }, ContextCompat.getMainExecutor(this))
    }

    @androidx.camera.core.ExperimentalGetImage
    private fun analyze(proxy: ImageProxy) {
        if (!done) {
            val base = decodeQr(proxy)
            if (base != null) {
                done = true
                runOnUiThread {
                    setResult(RESULT_OK, Intent().putExtra(EXTRA_BASE, base))
                    Toast.makeText(this, "已识别：$base", Toast.LENGTH_SHORT).show()
                    finish()
                }
            }
        }
        proxy.close()
    }

    /** YUV_420_888 → RGB → zxing；识别 http(s):// 开头的二维码内容 */
    @androidx.camera.core.ExperimentalGetImage
    private fun decodeQr(proxy: ImageProxy): String? {
        return try {
            val img = proxy.image ?: return null
            val w = proxy.width
            val h = proxy.height
            val y = img.planes[0].buffer
            val u = img.planes[1].buffer
            val v = img.planes[2].buffer
            val yRow = img.planes[0].rowStride
            val uvRow = img.planes[1].rowStride
            val uvPix = img.planes[1].pixelStride

            val rgb = IntArray(w * h)
            for (row in 0 until h) {
                for (col in 0 until w) {
                    val yv = (y.get(row * yRow + col).toInt() and 0xFF)
                    val uvRowBase = row / 2 * uvRow + col / 2 * uvPix
                    val uvVal = (u.get(uvRowBase).toInt() and 0xFF) - 128
                    val vvVal = (v.get(uvRowBase).toInt() and 0xFF) - 128
                    var r = yv + 1.402 * vvVal
                    var g = yv - 0.344 * uvVal - 0.714 * vvVal
                    var b = yv + 1.772 * uvVal
                    if (r < 0) r = 0.0; if (r > 255) r = 255.0
                    if (g < 0) g = 0.0; if (g > 255) g = 255.0
                    if (b < 0) b = 0.0; if (b > 255) b = 255.0
                    rgb[row * w + col] = (0xFF shl 24) or (r.toInt() shl 16) or (g.toInt() shl 8) or b.toInt()
                }
            }
            val src = RGBLuminanceSource(w, h, rgb)
            val bmp = BinaryBitmap(HybridBinarizer(src))
            val hints = mapOf(DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE))
            val res = QRCodeReader().decode(bmp, hints)
            val text = res.text
            if (text.startsWith("http://") || text.startsWith("https://")) {
                // http://ip:port/connect?name=xxx → http://ip:port
                val cut = text.split("/connect?")[0]
                cut.ifBlank { null }
            } else null
        } catch (e: Exception) {
            null
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        exec.shutdown()
    }
}
