package com.captainclaw.hud

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.InetAddress

class DeckTest {
    private fun origins(input: String) = (Deck.parse(input) as Deck.Parsed.Candidates).origins

    @Test
    fun keepsOnlyTheOrigin() {
        assertEquals(listOf("https://deck.example.com"), origins("https://Deck.Example.com/hud/?v=agents"))
        assertEquals(listOf("http://192.168.1.20:25080"), origins("http://192.168.1.20:25080/"))
    }

    @Test
    fun noSchemeTriesHttpsThenLocalHttp() {
        assertEquals(listOf("https://192.168.1.20:25080", "http://192.168.1.20:25080"), origins("192.168.1.20:25080"))
        assertEquals(listOf("https://mymac.local:25080", "http://mymac.local:25080"), origins("mymac.local:25080"))
        assertEquals(listOf("https://8.8.8.8"), origins("8.8.8.8"))
        // A dotted name may resolve to a LAN address: http is decided by DNS at check time.
        assertEquals(listOf("https://deck.example.com", "http://deck.example.com"), origins("deck.example.com"))
    }

    @Test
    fun refusesPlainHttpToPublicAddresses() {
        assertTrue(Deck.parse("http://8.8.8.8:25080") is Deck.Parsed.Invalid)
        assertTrue(Deck.parse("ftp://deck") is Deck.Parsed.Invalid)
        assertTrue(Deck.parse("   ") is Deck.Parsed.Invalid)
    }

    @Test
    fun bracketsIpv6() {
        assertEquals(listOf("http://[fd00::1]:25080"), origins("http://[fd00::1]:25080"))
    }

    @Test
    fun localNames() {
        assertEquals(true, Deck.isLocalName("10.0.2.2"))
        assertEquals(true, Deck.isLocalName("100.101.102.103")) // Tailscale
        assertEquals(true, Deck.isLocalName("localhost"))
        assertEquals(true, Deck.isLocalName("deckbox"))
        assertEquals(true, Deck.isLocalName("deck.home.arpa"))
        assertEquals(false, Deck.isLocalName("100.128.0.1"))
        assertEquals(false, Deck.isLocalName("1.1.1.1"))
        assertNull(Deck.isLocalName("deck.example.com"))
    }

    @Test
    fun localAddresses() {
        assertTrue(Deck.isLocalAddress(InetAddress.getByName("fd12:3456::1")))
        assertTrue(Deck.isLocalAddress(InetAddress.getByName("fe80::1")))
        assertTrue(Deck.isLocalAddress(InetAddress.getByName("172.20.0.5")))
        assertFalse(Deck.isLocalAddress(InetAddress.getByName("2001:4860:4860::8888")))
        assertFalse(Deck.isLocalAddress(InetAddress.getByName("172.32.0.1")))
    }

    @Test
    fun httpsIsAlwaysAllowed() {
        assertTrue(Deck.plainHttpAllowed("https://deck.example.com"))
        assertTrue(Deck.plainHttpAllowed("http://192.168.1.20:25080"))
        assertFalse(Deck.plainHttpAllowed("http://8.8.8.8"))
    }
}
