package com.captainclaw.hud

import android.content.Context
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetAddress
import java.net.SocketTimeoutException
import java.net.URI
import java.net.URL
import java.net.UnknownHostException
import javax.net.ssl.SSLException

/**
 * The Flight Deck this app shows: its origin (scheme://host[:port], no path —
 * the HUD lives at /hud/ and calls /fd/... on the same origin), kept in
 * SharedPreferences.
 *
 * Clear-text http is for decks on the local network only: a literal private /
 * loopback / link-local / CGNAT (Tailscale) address, a local name (.local,
 * .lan, a single label…), or a name that resolves to such addresses only.
 * network_security_config.xml has to allow clear text app-wide (it can't name
 * IP ranges), so this is where the rule is enforced.
 */
object Deck {
    private const val PREFS = "deck"
    private const val KEY_ORIGIN = "origin"
    private const val TIMEOUT_MS = 10_000

    fun saved(context: Context): String? =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_ORIGIN, null)

    fun save(context: Context, origin: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_ORIGIN, origin).apply()
    }

    /** What the user typed, as origins to try in order, or why it can't be one. */
    sealed interface Parsed {
        data class Candidates(val origins: List<String>) : Parsed
        data class Invalid(val message: String) : Parsed
    }

    fun parse(input: String): Parsed {
        val text = input.trim()
        if (text.isEmpty()) return Parsed.Invalid("Enter your Flight Deck's address.")
        val hasScheme = Regex("^[a-zA-Z][a-zA-Z0-9+.-]*://").containsMatchIn(text)
        val uri = try {
            URI(if (hasScheme) text else "http://$text")
        } catch (_: Exception) {
            return Parsed.Invalid("That doesn't look like an address.")
        }
        val scheme = uri.scheme?.lowercase()
        if (scheme != "http" && scheme != "https") return Parsed.Invalid("Use an http:// or https:// address.")
        val host = uri.host?.lowercase()?.trimEnd('.')?.removePrefix("[")?.removeSuffix("]")
        if (host.isNullOrEmpty()) return Parsed.Invalid("That doesn't look like an address.")
        val authority = (if (host.contains(':')) "[$host]" else host) + if (uri.port > 0) ":${uri.port}" else ""
        val public = isLocalName(host) == false
        if (hasScheme) {
            if (scheme == "http" && public) return Parsed.Invalid(PLAIN_HTTP_REFUSED)
            return Parsed.Candidates(listOf("$scheme://$authority"))
        }
        // No scheme: https first; plain http only where it may be allowed.
        return Parsed.Candidates(
            if (public) listOf("https://$authority") else listOf("https://$authority", "http://$authority"),
        )
    }

    /**
     * true: a local-network address or name (.local, a single label…); false:
     * a public IP address; null: any other name — local only if it resolves
     * to local addresses ([resolvesLocally]).
     */
    fun isLocalName(host: String): Boolean? {
        val h = host.lowercase().trimEnd('.').removePrefix("[").removeSuffix("]")
        literalAddress(h)?.let { return isLocalAddress(it) }
        if (h == "localhost" || !h.contains('.')) return true
        if (LOCAL_SUFFIXES.any { h.endsWith(it) }) return true
        return null
    }

    /** Every address `host` resolves to is on the local network. Blocking (DNS). */
    fun resolvesLocally(host: String): Boolean = try {
        val all = InetAddress.getAllByName(host.removePrefix("[").removeSuffix("]"))
        all.isNotEmpty() && all.all(::isLocalAddress)
    } catch (_: IOException) {
        false
    }

    /** May the app use clear text to this origin? Blocking (DNS) for names. */
    fun plainHttpAllowed(origin: String): Boolean {
        val uri = URI(origin)
        if (uri.scheme != "http") return true
        val host = uri.host ?: return false
        val h = host.removePrefix("[").removeSuffix("]")
        return when (isLocalName(h)) {
            true -> literalAddress(h) != null || resolvesLocallyOrUnresolved(h)
            false -> false
            null -> resolvesLocally(h)
        }
    }

    // A .local name only answers on the LAN (mDNS); one that doesn't resolve
    // right now is not a reason to refuse it.
    private fun resolvesLocallyOrUnresolved(host: String): Boolean = try {
        InetAddress.getAllByName(host).all(::isLocalAddress)
    } catch (_: IOException) {
        true
    }

    fun literalAddress(host: String): InetAddress? {
        val ipv4 = Regex("^\\d{1,3}(\\.\\d{1,3}){3}$")
        if (!ipv4.matches(host) && !host.contains(':')) return null
        return try {
            InetAddress.getByName(host) // a literal: parsed, no DNS
        } catch (_: Exception) {
            null
        }
    }

    fun isLocalAddress(a: InetAddress): Boolean {
        if (a.isLoopbackAddress || a.isLinkLocalAddress || a.isSiteLocalAddress) return true
        val b = a.address
        return when (a) {
            // 100.64.0.0/10 — carrier-grade NAT, where Tailscale puts its nodes.
            is Inet4Address -> (b[0].toInt() and 0xff) == 100 && (b[1].toInt() and 0xc0) == 64
            // fc00::/7 — unique local addresses.
            is Inet6Address -> (b[0].toInt() and 0xfe) == 0xfc
            else -> false
        }
    }

    /** The outcome of asking a candidate origin for GET /fd/auth/status. */
    sealed interface Check {
        data class Ok(val origin: String, val authEnabled: Boolean) : Check
        data class Failed(val message: String) : Check
    }

    /** Try each candidate in turn; the first that answers like Flight Deck wins. Blocking. */
    fun check(input: String): Check {
        val candidates = when (val p = parse(input)) {
            is Parsed.Invalid -> return Check.Failed(p.message)
            is Parsed.Candidates -> p.origins
        }
        val errors = mutableListOf<Check.Failed>()
        for (origin in candidates) {
            when (val r = checkOrigin(origin, redirectsLeft = 1)) {
                is Check.Ok -> return r
                is Check.Failed -> errors += r
            }
        }
        // No scheme typed: the http attempt's error says more about a LAN deck
        // (it has no https) — unless http was not allowed there at all.
        return errors.lastOrNull { it.message != PLAIN_HTTP_REFUSED } ?: errors.firstOrNull()
            ?: Check.Failed("Couldn't reach that address.")
    }

    private fun checkOrigin(origin: String, redirectsLeft: Int): Check {
        if (!plainHttpAllowed(origin)) return Check.Failed(PLAIN_HTTP_REFUSED)
        val conn = try {
            URL("$origin/fd/auth/status").openConnection() as HttpURLConnection
        } catch (e: Exception) {
            return Check.Failed("That doesn't look like an address.")
        }
        try {
            conn.connectTimeout = TIMEOUT_MS
            conn.readTimeout = TIMEOUT_MS
            conn.instanceFollowRedirects = false
            conn.setRequestProperty("Accept", "application/json")
            val code = conn.responseCode
            if (code in 300..399) {
                val location = conn.getHeaderField("Location")
                val next = location?.let { runCatching { URI(origin).resolve(it) }.getOrNull() }
                // http → https on the same host (a TLS proxy): follow it once.
                if (next != null && redirectsLeft > 0 && next.scheme == "https" && next.host == URI(origin).host) {
                    val port = if (next.port > 0) ":${next.port}" else ""
                    return checkOrigin("https://${next.host}$port", redirectsLeft - 1)
                }
                return Check.Failed("$origin redirects to ${location ?: "another address"} — enter that address instead.")
            }
            val body = (if (code >= 400) conn.errorStream else conn.inputStream)?.use { s ->
                s.bufferedReader().readText().take(4_000)
            }.orEmpty()
            if (code == 403 || code == 400) {
                return Check.Failed(
                    "Flight Deck at $origin refused this address (HTTP $code). A deck reached through its own " +
                        "domain needs that name in FD_ALLOWED_HOSTS or FD_PUBLIC_URL.",
                )
            }
            if (code != 200) return Check.Failed("$origin answered HTTP $code — is this a Flight Deck?")
            val json = try {
                JSONObject(body)
            } catch (_: Exception) {
                return Check.Failed("$origin answered, but not like Flight Deck.")
            }
            if (!json.has("auth_enabled")) return Check.Failed("$origin answered, but not like Flight Deck.")
            return Check.Ok(origin, json.optBoolean("auth_enabled", true))
        } catch (e: UnknownHostException) {
            return Check.Failed("Can't find ${URI(origin).host} — check the address and your network.")
        } catch (e: SSLException) {
            return Check.Failed("$origin has no HTTPS certificate this phone trusts.")
        } catch (e: SocketTimeoutException) {
            return Check.Failed("$origin didn't answer in time.")
        } catch (e: IOException) {
            return Check.Failed("Can't reach $origin (${e.message ?: e.javaClass.simpleName}).")
        } finally {
            conn.disconnect()
        }
    }

    const val PLAIN_HTTP_REFUSED =
        "Plain http:// is only for a deck on your local network. Use https:// for a deck on the internet."

    private val LOCAL_SUFFIXES = listOf(".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".localhost")
}
