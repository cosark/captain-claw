package com.captainclaw.hud

import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import org.json.JSONObject
import java.util.Locale

/**
 * Android TextToSpeech behind the HUD's "Read aloud": Android WebView has no
 * `window.speechSynthesis`, so [SHIM] (injected for the deck origin only)
 * defines one that posts to the app's bridge object, and this speaks it.
 *
 * Page → app:  {"t":"tts","op":"speak","id","text","lang","rate","pitch"} · {"t":"tts","op":"cancel"}
 * App → page:  {"t":"tts","ev":"start"|"end"|"error","id"}
 */
class Speech(context: Context, private val reply: (String) -> Unit) {
    private val main = Handler(Looper.getMainLooper())
    private var ready = false
    private var failed = false
    private val waiting = mutableListOf<JSONObject>()
    private val tts: TextToSpeech = TextToSpeech(context.applicationContext) { status ->
        main.post { onInit(status) }
    }

    init {
        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(id: String) = send("start", id)
            override fun onDone(id: String) = send("end", id)
            @Deprecated("Deprecated in Java")
            override fun onError(id: String) = send("error", id)
            override fun onError(id: String, errorCode: Int) = send("error", id)
            override fun onStop(id: String, interrupted: Boolean) = send("end", id)
        })
    }

    private fun onInit(status: Int) {
        if (status == TextToSpeech.SUCCESS) {
            ready = true
            waiting.forEach(::speak)
        } else {
            failed = true
            waiting.forEach { send("error", it.optString("id")) }
        }
        waiting.clear()
    }

    /** A message from the page (main thread). */
    fun handle(msg: JSONObject) {
        when (msg.optString("op")) {
            "speak" -> when {
                failed -> send("error", msg.optString("id"))
                ready -> speak(msg)
                else -> waiting += msg
            }
            "cancel" -> cancel()
        }
    }

    fun cancel() {
        waiting.clear()
        if (ready) tts.stop()
    }

    fun shutdown() {
        cancel()
        tts.shutdown()
    }

    private fun speak(msg: JSONObject) {
        val id = msg.optString("id")
        val text = msg.optString("text")
        if (text.isBlank()) {
            send("end", id)
            return
        }
        val lang = msg.optString("lang")
        if (lang.isNotBlank()) {
            val locale = Locale.forLanguageTag(lang)
            if (tts.isLanguageAvailable(locale) >= TextToSpeech.LANG_AVAILABLE && tts.voice?.locale != locale) {
                tts.language = locale
            }
        }
        tts.setSpeechRate(msg.optDouble("rate", 1.0).toFloat().coerceIn(0.1f, 4f))
        tts.setPitch(msg.optDouble("pitch", 1.0).toFloat().coerceIn(0.1f, 2f))
        val result = tts.speak(text.take(TextToSpeech.getMaxSpeechInputLength()), TextToSpeech.QUEUE_ADD, Bundle(), id)
        if (result != TextToSpeech.SUCCESS) send("error", id)
    }

    private fun send(ev: String, id: String) {
        val json = JSONObject().put("t", "tts").put("ev", ev).put("id", id).toString()
        main.post { reply(json) }
    }

    companion object {
        /**
         * Runs at document start in the deck's pages. Only when the WebView
         * lacks speechSynthesis; the HUD uses speak(), cancel() and
         * `new SpeechSynthesisUtterance(text)` with `lang`.
         */
        val SHIM = """
            (function () {
              'use strict';
              var bridge = window.${MainActivity.BRIDGE};
              if (!bridge || (window.speechSynthesis && window.SpeechSynthesisUtterance)) return;
              var pending = {};
              var seq = 0;
              function post(m) { try { bridge.postMessage(JSON.stringify(m)); } catch (e) {} }
              function fire(u, type) {
                var ev = { type: type, utterance: u, charIndex: 0, elapsedTime: 0 };
                try { if (typeof u['on' + type] === 'function') u['on' + type](ev); } catch (e) {}
                (u._l[type] || []).slice().forEach(function (f) { try { f.call(u, ev); } catch (e) {} });
              }
              function Utterance(text) {
                this.text = text == null ? '' : String(text);
                this.lang = ''; this.rate = 1; this.pitch = 1; this.volume = 1; this.voice = null;
                this.onstart = null; this.onend = null; this.onerror = null;
                this.onpause = null; this.onresume = null; this.onboundary = null; this.onmark = null;
                this._l = {};
              }
              Utterance.prototype.addEventListener = function (t, f) { (this._l[t] = this._l[t] || []).push(f); };
              Utterance.prototype.removeEventListener = function (t, f) {
                this._l[t] = (this._l[t] || []).filter(function (g) { return g !== f; });
              };
              function busy() { return Object.keys(pending).length > 0; }
              var synth = {
                speaking: false, pending: false, paused: false, onvoiceschanged: null,
                speak: function (u) {
                  if (!(u instanceof Utterance)) return;
                  var id = 'u' + (++seq);
                  pending[id] = u;
                  synth.pending = true;
                  post({ t: 'tts', op: 'speak', id: id, text: u.text,
                         lang: u.lang || document.documentElement.lang || '', rate: u.rate, pitch: u.pitch });
                },
                cancel: function () {
                  pending = {};
                  synth.speaking = false; synth.pending = false;
                  post({ t: 'tts', op: 'cancel' });
                },
                pause: function () {}, resume: function () {},
                getVoices: function () { return []; },
                addEventListener: function () {}, removeEventListener: function () {}
              };
              bridge.addEventListener('message', function (e) {
                var m;
                try { m = JSON.parse(e.data); } catch (err) { return; }
                if (!m || m.t !== 'tts') return;
                var u = pending[m.id];
                if (!u) return;
                if (m.ev === 'start') { synth.speaking = true; fire(u, 'start'); return; }
                delete pending[m.id];
                synth.speaking = busy(); synth.pending = busy();
                fire(u, m.ev === 'error' ? 'error' : 'end');
              });
              try {
                Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });
              } catch (e) { window.speechSynthesis = synth; }
              window.SpeechSynthesisUtterance = Utterance;
            })();
        """.trimIndent()
    }
}
