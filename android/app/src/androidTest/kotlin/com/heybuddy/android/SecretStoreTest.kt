package com.heybuddy.android

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.heybuddy.android.settings.SecretStore
import com.heybuddy.android.settings.SettingsRepository
import com.heybuddy.android.settings.VoiceMode
import com.heybuddy.core.tools.Decision
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SecretStoreTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext

    @Test fun roundTripIsEncryptedAtRest() {
        val name = "test_secret_${System.nanoTime()}"
        val value = "hb_tok\u00e9n_\u2713_" + "x".repeat(200)
        val store = SecretStore(context)
        store.put(name, value)
        assertTrue(store.has(name))
        assertEquals(value, store.get(name))
        // A fresh instance (same Keystore key) reads it back.
        assertEquals(value, SecretStore(context).get(name))
        // The raw preferences never contain the plaintext.
        val raw = context.getSharedPreferences(SecretStore.FILE, Context.MODE_PRIVATE).getString(name, null)!!
        assertFalse(raw.contains("hb_tok"))
        // Empty value deletes.
        store.put(name, "")
        assertFalse(store.has(name))
        assertEquals("", store.get(name))
    }

    @Test fun corruptValueReadsAsUnset() {
        val name = "test_corrupt_${System.nanoTime()}"
        context.getSharedPreferences(SecretStore.FILE, Context.MODE_PRIVATE).edit().putString(name, "bm90:aXY=").commit()
        assertEquals("", SecretStore(context).get(name))
        SecretStore(context).put(name, "")
    }

    @Test fun settingsRoundTrip() = runBlocking {
        val graph = context.graph
        val before = graph.settings.current()
        try {
            graph.settings.update {
                it.copy(serverUrl = "https://buddy.example.org", voiceMode = VoiceMode.DEVICE, actionPolicy = Decision.DENY, volumePercent = 250)
            }
            val after = SettingsRepository(context).current()
            assertEquals("https://buddy.example.org", after.serverUrl)
            assertEquals(VoiceMode.DEVICE, after.voiceMode)
            assertEquals(Decision.DENY, after.actionPolicy)
            assertEquals(100, after.volumePercent) // clamped
        } finally {
            graph.settings.update { before }
        }
    }
}
