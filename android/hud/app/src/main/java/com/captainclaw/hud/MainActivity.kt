package com.captainclaw.hud

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.graphics.Bitmap
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.lifecycle.lifecycleScope
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.ScriptHandler
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The HUD: one WebView on `<deck>/hud/`. Everything the HUD does is same-origin
 * with the deck (relative /fd/... calls, the chat WebSocket built from
 * window.location, the httpOnly fd_refresh cookie), so the app adds no auth of
 * its own — it keeps that cookie, keeps navigation on the deck, handles Back
 * and the window insets, and lends the page a speech engine.
 *
 * Every start (a new activity) loads /hud/ from the deck — no-cache, and the
 * HUD skips its offline service worker in the app — and coming back to the
 * app reloads it if the deck has deployed a different HUD build since.
 */
class MainActivity : ComponentActivity() {
    private lateinit var webView: WebView
    private lateinit var errorPanel: View
    private lateinit var errorText: TextView
    private lateinit var origin: String
    private var speech: Speech? = null
    private var replyProxy: JavaScriptReplyProxy? = null
    private var shimHandle: ScriptHandler? = null
    /** The /assets/… script of the HUD build on screen (to spot a new deploy). */
    private var shownBuild: String? = null
    private var resumedOnce = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        super.onCreate(savedInstanceState)
        val saved = Deck.saved(this)
        if (saved == null) {
            startActivity(Intent(this, SettingsActivity::class.java))
            finish()
            return
        }
        origin = saved
        setContentView(R.layout.activity_main)
        webView = findViewById(R.id.web)
        errorPanel = findViewById(R.id.error_panel)
        errorText = findViewById(R.id.error_text)
        findViewById<Button>(R.id.error_retry).setOnClickListener { loadHud() }
        findViewById<Button>(R.id.error_settings).setOnClickListener { openSettings() }

        // Edge-to-edge (targetSdk 35+): pad the content by the system bars,
        // cutout and keyboard, so the HUD's bottom composer sits above the IME
        // (adjustResize alone no longer resizes the window).
        val root = findViewById<View>(R.id.root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            WindowInsetsCompat.CONSUMED
        }

        if (0 != applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) WebView.setWebContentsDebuggingEnabled(true)
        setUpWebView()

        // Back (predictive back too — onBackPressed is never called at
        // targetSdk 36): the HUD's previous screen, else close the app, so the
        // next launch is a start that loads the HUD afresh (see goBack).
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() = goBack()
        })

        loadHud()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setUpWebView() {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            setSupportMultipleWindows(false)
            allowFileAccess = false
            allowContentAccess = false
            builtInZoomControls = false
            userAgentString = "$userAgentString $UA_TOKEN/${BuildConfig.VERSION_NAME}"
        }
        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(webView, false)
        }
        webView.webChromeClient = WebChromeClient()
        webView.webViewClient = HudClient()

        // The bridge object, for the deck's origin only (never
        // addJavascriptInterface, which every frame of every origin would see).
        val rules = setOf(origin)
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            WebViewCompat.addWebMessageListener(webView, BRIDGE, rules) { _, message, sourceOrigin, isMainFrame, proxy ->
                if (isMainFrame && sourceOrigin.toString().trimEnd('/') == origin) onBridgeMessage(message, proxy)
            }
            if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                shimHandle = WebViewCompat.addDocumentStartJavaScript(webView, Speech.SHIM, rules)
            }
        }
    }

    private fun onBridgeMessage(message: WebMessageCompat, proxy: JavaScriptReplyProxy) {
        val msg = try {
            JSONObject(message.data ?: return)
        } catch (_: Exception) {
            return
        }
        when (msg.optString("t")) {
            "settings" -> openSettings()
            "tts" -> {
                replyProxy = proxy
                val s = speech ?: Speech(this) { json -> replyProxy?.postMessage(json) }.also { speech = it }
                s.handle(msg)
            }
        }
    }

    /**
     * Back asks the page first, as the Meta Display's shell does: history.back()
     * while `navigation.canGoBack`. WebView.canGoBack() alone would skip the
     * entries the HUD pushes without a tap (Chromium's history intervention) —
     * the last agent a launch resumes, the parents of a deep link — and close
     * the app from there. Then webView.goBack() when it can, else finish.
     */
    private fun goBack() {
        var answered = false
        val native = Runnable {
            if (webView.canGoBack()) webView.goBack() else finish()
        }
        val timeout = Runnable {
            if (!answered) {
                answered = true
                native.run()
            }
        }
        webView.evaluateJavascript(PAGE_BACK) { result ->
            if (answered) return@evaluateJavascript
            answered = true
            webView.removeCallbacks(timeout)
            if (result != "true") native.run()
        }
        webView.postDelayed(timeout, 600)
    }

    /** Load the deck's current HUD from the network (never a cached page). */
    private fun loadHud() {
        errorPanel.isVisible = false
        speech?.cancel()
        lifecycleScope.launch {
            // Clear text only while the deck is still on the local network.
            if (!withContext(Dispatchers.IO) { Deck.plainHttpAllowed(origin) }) {
                showError(Deck.PLAIN_HTTP_REFUSED)
                return@launch
            }
            webView.loadUrl("$origin/hud/", mapOf("Cache-Control" to "no-cache", "Pragma" to "no-cache"))
        }
    }

    /** Note which HUD build the page on screen runs. */
    private fun noteShownBuild(view: WebView) {
        view.evaluateJavascript(
            "(function(){var s=document.querySelector('script[type=\"module\"][src]');return s?s.getAttribute('src'):''})()",
        ) { result ->
            val src = result?.trim('"').orEmpty()
            shownBuild = src.takeIf { it.startsWith("/assets/") }
        }
    }

    /** The HUD build the deck serves now: its /assets/… entry script, or null. Blocking. */
    private fun hudBuild(): String? = try {
        val conn = URL("$origin/hud/").openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 5_000
            conn.readTimeout = 5_000
            conn.setRequestProperty("Cache-Control", "no-cache")
            if (conn.responseCode != 200) {
                null
            } else {
                val html = conn.inputStream.use { it.bufferedReader().readText().take(64_000) }
                Regex("""<script[^>]+src="(/assets/[^"]+\.js)"""").find(html)?.groupValues?.get(1)
            }
        } finally {
            conn.disconnect()
        }
    } catch (_: Exception) {
        null
    }

    override fun onResume() {
        super.onResume()
        if (!::webView.isInitialized) return
        if (Deck.saved(this) != origin) {
            recreate() // the deck was changed in Settings
            return
        }
        if (!resumedOnce) {
            resumedOnce = true
            return
        }
        // Back from the background: pick up a HUD the deck deployed meanwhile.
        lifecycleScope.launch {
            val now = withContext(Dispatchers.IO) { hudBuild() } ?: return@launch
            if (shownBuild != null && now != shownBuild && !errorPanel.isVisible) {
                shownBuild = now
                webView.reload()
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.getBooleanExtra(EXTRA_RELOAD, false) && ::webView.isInitialized && Deck.saved(this) == origin) loadHud()
    }

    override fun onPause() {
        super.onPause()
        // The httpOnly fd_refresh cookie is the session: get it to disk now.
        CookieManager.getInstance().flush()
    }

    override fun onDestroy() {
        speech?.shutdown()
        speech = null
        if (::webView.isInitialized) {
            shimHandle?.remove()
            (webView.parent as? android.view.ViewGroup)?.removeView(webView)
            webView.destroy()
        }
        super.onDestroy()
    }

    private fun openSettings() {
        startActivity(Intent(this, SettingsActivity::class.java))
    }

    private fun showError(text: String) {
        errorText.text = text
        errorPanel.isVisible = true
    }

    private fun isDeck(uri: Uri): Boolean {
        val u = uri.toString()
        return u == origin || u.startsWith("$origin/") || u.startsWith("$origin?") || u.startsWith("$origin#")
    }

    private inner class HudClient : WebViewClient() {
        /** Pages of the deck stay here; anything else opens in the browser. */
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val uri = request.url
            if (isDeck(uri)) return false
            if (uri.scheme == "about" || uri.scheme == "blob" || uri.scheme == "data") return false
            if (request.isForMainFrame && uri.scheme in setOf("http", "https", "mailto", "tel", "geo")) {
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE))
                } catch (_: ActivityNotFoundException) {
                    // nothing can open it
                }
            }
            return true
        }

        override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
            speech?.cancel()
        }

        override fun onPageFinished(view: WebView, url: String) {
            if (isDeck(Uri.parse(url))) noteShownBuild(view)
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (!request.isForMainFrame) return
            showError("Can't reach Flight Deck at $origin\n(${error.description})")
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
            if (!request.isForMainFrame) return
            val code = response.statusCode
            showError(
                if (code == 403 || code == 400) {
                    "Flight Deck at $origin refused this address (HTTP $code). A deck reached through its own " +
                        "domain needs that name in FD_ALLOWED_HOSTS or FD_PUBLIC_URL."
                } else {
                    "Flight Deck at $origin answered HTTP $code."
                },
            )
        }

        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            // The page's renderer died (killed for memory): start over cleanly.
            recreate()
            return true
        }
    }

    companion object {
        /** User-agent token the HUD detects the app by (flight-deck/src/hud/device.ts). */
        const val UA_TOKEN = "CaptainClawHUD"
        /** The page-side name of the bridge object (device.ts, Speech.SHIM). */
        const val BRIDGE = "CaptainClawHUD"
        /** Settings → "Reload": load the HUD afresh. */
        const val EXTRA_RELOAD = "reload"
        /** Back inside the page when it has somewhere to go back to (true), else false. */
        private const val PAGE_BACK =
            "(function(){try{if(window.navigation&&navigation.canGoBack){history.back();return true}}catch(e){}return false})()"
    }
}
