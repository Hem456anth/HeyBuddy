package com.heybuddy.android

import android.Manifest
import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ShortcutManager
import android.service.quicksettings.TileService
import android.speech.RecognitionService
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.heybuddy.android.a11y.BuddyAccessibilityService
import com.heybuddy.android.activation.TalkTileService
import com.heybuddy.android.activation.TalkWidgetReceiver
import com.heybuddy.android.assist.HeyBuddyRecognitionService
import com.heybuddy.android.assist.HeyBuddySessionService
import com.heybuddy.android.assist.HeyBuddyVoiceInteractionService
import com.heybuddy.android.ui.CompanionActivity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** Self-check: every activation surface is declared the way Android expects. */
@RunWith(AndroidJUnit4::class)
class ManifestComponentsTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext
    private val pm = context.packageManager
    private val pkg = context.packageName

    private fun service(action: String, cls: Class<*>) =
        pm.queryIntentServices(Intent(action).setPackage(pkg), PackageManager.GET_META_DATA)
            .map { it.serviceInfo }
            .firstOrNull { it.name == cls.name }

    @Test fun assistantServiceIsBoundOnlyBySystem() {
        val info = service("android.service.voice.VoiceInteractionService", HeyBuddyVoiceInteractionService::class.java)
        assertNotNull("VoiceInteractionService not resolvable", info)
        assertEquals(Manifest.permission.BIND_VOICE_INTERACTION, info!!.permission)
        assertTrue(info.exported)
        assertTrue("android.voice_interaction meta-data missing", info.metaData?.containsKey("android.voice_interaction") == true)

        val session = pm.getServiceInfo(ComponentName(context, HeyBuddySessionService::class.java), 0)
        assertEquals(Manifest.permission.BIND_VOICE_INTERACTION, session.permission)
    }

    @Test fun recognitionServiceDeclared() {
        val info = service(RecognitionService.SERVICE_INTERFACE, HeyBuddyRecognitionService::class.java)
        assertNotNull(info)
        assertEquals(Manifest.permission.BIND_VOICE_INTERACTION, info!!.permission)
    }

    @Test fun quickSettingsTileResolvable() {
        val info = service(TileService.ACTION_QS_TILE, TalkTileService::class.java)
        assertNotNull("QS tile not resolvable", info)
        assertEquals(Manifest.permission.BIND_QUICK_SETTINGS_TILE, info!!.permission)
    }

    @Test fun accessibilityServiceDeclaredAndProtected() {
        val info = service("android.accessibilityservice.AccessibilityService", BuddyAccessibilityService::class.java)
        assertNotNull(info)
        assertEquals(Manifest.permission.BIND_ACCESSIBILITY_SERVICE, info!!.permission)
        assertTrue(info.metaData?.containsKey("android.accessibilityservice") == true)
    }

    @Test fun widgetProviderInstalled() {
        val providers = AppWidgetManager.getInstance(context).getInstalledProvidersForPackage(pkg, null)
        assertTrue(
            "widget provider missing: ${providers.map { it.provider }}",
            providers.any { it.provider.className == TalkWidgetReceiver::class.java.name },
        )
    }

    @Test fun shortcutsAndAssistIntentsResolve() {
        val talk = Intent(CompanionActivity.ACTION_TALK).setPackage(pkg)
        assertEquals(CompanionActivity::class.java.name, pm.resolveActivity(talk, 0)?.activityInfo?.name)
        val assist = Intent(Intent.ACTION_ASSIST).setPackage(pkg)
        assertEquals(CompanionActivity::class.java.name, pm.resolveActivity(assist, 0)?.activityInfo?.name)

        val shortcuts = context.getSystemService(ShortcutManager::class.java).manifestShortcuts
        val ids = shortcuts.map { it.id }.toSet()
        assertTrue("manifest shortcuts: $ids", ids.containsAll(setOf("talk", "type")))
        for (shortcut in shortcuts) {
            val intent = shortcut.intent
            assertNotNull(intent)
            assertNotNull("shortcut ${shortcut.id} does not resolve", pm.resolveActivity(intent!!, 0))
        }
    }

    @Test fun internalComponentsAreNotExported() {
        val approval = pm.getReceiverInfo(ComponentName(pkg, "com.heybuddy.android.companion.ApprovalReceiver"), 0)
        assertFalse("ApprovalReceiver must not be exported", approval.exported)
        val companion = pm.getServiceInfo(ComponentName(pkg, "com.heybuddy.android.service.CompanionService"), 0)
        assertFalse("CompanionService must not be exported", companion.exported)
    }
}
