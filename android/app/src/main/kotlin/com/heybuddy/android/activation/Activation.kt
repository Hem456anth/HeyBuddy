package com.heybuddy.android.activation

import android.app.StatusBarManager
import android.app.role.RoleManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.drawable.Icon
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import com.heybuddy.android.R
import com.heybuddy.android.a11y.BuddyAccessibilityService

/**
 * Helpers for the ways HeyBuddy can be summoned and the settings screens
 * behind them. Third-party apps cannot read or intercept the power key; the
 * closest official route is the assistant role, which the user grants in
 * system settings (AOSP marks ROLE_ASSISTANT as not requestable, so the
 * RoleManager dialog is tried first and the settings screen is the fallback).
 */
object Activation {

    fun isAssistantRoleAvailable(context: Context): Boolean =
        Build.VERSION.SDK_INT >= 29 && context.getSystemService(RoleManager::class.java).isRoleAvailable(RoleManager.ROLE_ASSISTANT)

    fun isAssistant(context: Context): Boolean {
        if (Build.VERSION.SDK_INT >= 29) {
            val roles = context.getSystemService(RoleManager::class.java)
            if (roles.isRoleAvailable(RoleManager.ROLE_ASSISTANT)) return roles.isRoleHeld(RoleManager.ROLE_ASSISTANT)
        }
        // API 26-28: the selected voice interaction service is in Settings.Secure.
        val current = Settings.Secure.getString(context.contentResolver, "voice_interaction_service") ?: return false
        return ComponentName.unflattenFromString(current)?.packageName == context.packageName
    }

    /** RoleManager request intent (API 29+). On AOSP-based builds it is answered "cancelled" immediately. */
    fun roleRequestIntent(context: Context): Intent? =
        if (Build.VERSION.SDK_INT >= 29) context.getSystemService(RoleManager::class.java).createRequestRoleIntent(RoleManager.ROLE_ASSISTANT) else null

    /** Where the user picks the digital assistant app. */
    fun assistantSettingsIntent(context: Context): Intent {
        val candidates = listOf(
            Intent(Settings.ACTION_VOICE_INPUT_SETTINGS),
            Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS),
            Intent(Settings.ACTION_SETTINGS),
        )
        return candidates.first { it.resolveActivity(context.packageManager) != null }.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }

    fun accessibilitySettingsIntent(): Intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    /** App info page: where "Allow restricted settings" lives on Android 13+ for sideloaded apps. */
    fun appInfoIntent(context: Context): Intent =
        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    fun overlaySettingsIntent(context: Context): Intent =
        Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:${context.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    fun isIgnoringBatteryOptimizations(context: Context): Boolean =
        context.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(context.packageName)

    /** Only used when the user explicitly opts in (OEM battery killers). */
    @android.annotation.SuppressLint("BatteryLife")
    fun batteryOptOutIntent(context: Context): Intent =
        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${context.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    fun isAccessibilityOn(context: Context): Boolean = BuddyAccessibilityService.isEnabled(context)

    /** Android 13+: ask the system UI to offer adding the tile (user confirms). */
    fun requestAddTile(context: Context, onResult: (Int) -> Unit) {
        if (Build.VERSION.SDK_INT < 33) { onResult(-1); return }
        val manager = context.getSystemService(StatusBarManager::class.java)
        manager.requestAddTileService(
            ComponentName(context, TalkTileService::class.java),
            context.getString(R.string.tile_label),
            Icon.createWithResource(context, R.drawable.ic_buddy),
            context.mainExecutor,
        ) { result -> onResult(result) }
    }

    fun isComponentEnabled(context: Context, component: ComponentName): Boolean =
        context.packageManager.getComponentEnabledSetting(component) != PackageManager.COMPONENT_ENABLED_STATE_DISABLED
}
