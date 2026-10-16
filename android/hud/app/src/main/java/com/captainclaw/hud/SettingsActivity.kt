package com.captainclaw.hud

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The deck's address: asked on first launch, changed later from the HUD
 * ("App settings"), the launcher shortcut or the can't-reach screen. Saving
 * checks the address with GET /fd/auth/status first.
 */
class SettingsActivity : ComponentActivity() {
    private lateinit var field: EditText
    private lateinit var connect: Button
    private lateinit var status: TextView
    private var checking = false

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        val root = findViewById<View>(R.id.settings_root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            WindowInsetsCompat.CONSUMED
        }

        val saved = Deck.saved(this)
        field = findViewById(R.id.deck_url)
        connect = findViewById(R.id.connect)
        status = findViewById(R.id.status)
        findViewById<TextView>(R.id.settings_title).setText(if (saved == null) R.string.setup_title else R.string.settings_title)
        findViewById<TextView>(R.id.version).text = getString(R.string.version, BuildConfig.VERSION_NAME)
        if (savedInstanceState == null) field.setText(saved.orEmpty())

        connect.setOnClickListener { save() }
        field.setOnEditorActionListener { _, action, event ->
            val enter = event?.keyCode == KeyEvent.KEYCODE_ENTER && event.action == KeyEvent.ACTION_DOWN
            if (action == EditorInfo.IME_ACTION_GO || enter) {
                save()
                true
            } else {
                false
            }
        }

        val reload = findViewById<Button>(R.id.reload)
        val cancel = findViewById<Button>(R.id.cancel)
        reload.isVisible = saved != null
        cancel.isVisible = saved != null
        reload.setOnClickListener { openHud(reload = true) }
        cancel.setOnClickListener { leave() }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() = leave()
        })
    }

    private fun save() {
        if (checking) return
        val input = field.text.toString()
        checking = true
        connect.isEnabled = false
        status.setTextColor(getColor(R.color.text_dim))
        status.setText(R.string.checking)
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) { Deck.check(input) }
            checking = false
            connect.isEnabled = true
            when (result) {
                is Deck.Check.Ok -> {
                    val changed = result.origin != Deck.saved(this@SettingsActivity)
                    Deck.save(this@SettingsActivity, result.origin)
                    openHud(reload = !changed)
                }
                is Deck.Check.Failed -> {
                    status.setTextColor(getColor(R.color.error))
                    status.text = result.message
                }
            }
        }
    }

    /** Back without saving: to the HUD if there is a deck, else out of the app. */
    private fun leave() {
        if (Deck.saved(this) != null) openHud(reload = false) else finish()
    }

    private fun openHud(reload: Boolean) {
        startActivity(
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(MainActivity.EXTRA_RELOAD, reload),
        )
        finish()
    }
}
