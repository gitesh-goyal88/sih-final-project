package `in`.aapatmitra.data.device

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import androidx.core.content.ContextCompat
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

data class Fix(val lat: Double, val lng: Double, val accuracyM: Int?, val ageMs: Long)

/**
 * Last-known location only on the SOS path — never waits for a fresh fix (help must not wait, UI-UX §6.2).
 * LocationManager works without Play Services (TRD §3.1). No background location (SECURITY §10.3).
 */
@Singleton
class Locator @Inject constructor(@ApplicationContext private val ctx: Context) {
    fun granted(): Boolean = ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) ==
        PackageManager.PERMISSION_GRANTED || ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) ==
        PackageManager.PERMISSION_GRANTED

    @Suppress("MissingPermission")
    fun lastKnown(maxAgeMs: Long = 15 * 60_000): Fix? {
        if (!granted()) return null
        val lm = ctx.getSystemService(LocationManager::class.java) ?: return null
        val best: Location = lm.getProviders(true).mapNotNull { runCatching { lm.getLastKnownLocation(it) }.getOrNull() }
            .maxByOrNull { it.time } ?: return null
        val age = System.currentTimeMillis() - best.time
        if (age > maxAgeMs) return null
        return Fix(best.latitude, best.longitude, if (best.hasAccuracy()) best.accuracy.toInt() else null, age)
    }

    /**
     * A fresh fix for custody hand-over (GPS plausibility, SEC-CUS-02) — never used on the SOS path.
     * Falls back to the last-known fix after [timeoutMs].
     */
    @Suppress("MissingPermission", "DEPRECATION")
    suspend fun fresh(timeoutMs: Long = 5_000): Fix? {
        if (!granted()) return null
        val lm = ctx.getSystemService(LocationManager::class.java) ?: return lastKnown()
        val provider = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER).firstOrNull { lm.isProviderEnabled(it) }
            ?: return lastKnown()
        val loc = kotlinx.coroutines.withTimeoutOrNull(timeoutMs) {
            kotlinx.coroutines.suspendCancellableCoroutine<Location> { cont ->
                val l = android.location.LocationListener { if (cont.isActive) cont.resumeWith(Result.success(it)) }
                lm.requestSingleUpdate(provider, l, ctx.mainLooper)
                cont.invokeOnCancellation { lm.removeUpdates(l) }
            }
        } ?: return lastKnown()
        return Fix(loc.latitude, loc.longitude, if (loc.hasAccuracy()) loc.accuracy.toInt() else null, 0)
    }

    /** Ask the providers for one fresh fix in the background (used on screens that are not the SOS path). */
    @Suppress("MissingPermission", "DEPRECATION")
    fun warmUp() {
        if (!granted()) return
        val lm = ctx.getSystemService(LocationManager::class.java) ?: return
        for (p in listOf(LocationManager.NETWORK_PROVIDER, LocationManager.GPS_PROVIDER)) {
            if (lm.isProviderEnabled(p)) runCatching { lm.requestSingleUpdate(p, { }, ctx.mainLooper) }
        }
    }
}
