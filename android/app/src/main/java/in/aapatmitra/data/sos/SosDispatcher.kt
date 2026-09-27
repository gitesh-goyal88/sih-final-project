package `in`.aapatmitra.data.sos

import android.Manifest
import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.telephony.SmsManager
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat
import androidx.room.withTransaction
import dagger.hilt.android.qualifiers.ApplicationContext
import `in`.aapatmitra.BuildConfig
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.jsonOf
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.device.Connectivity
import `in`.aapatmitra.data.device.Locator
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.local.CaseEntity
import `in`.aapatmitra.data.remote.Api
import `in`.aapatmitra.data.session.Session
import `in`.aapatmitra.data.sync.ChangeApplier
import `in`.aapatmitra.data.sync.Outbox
import `in`.aapatmitra.data.sync.Priority
import `in`.aapatmitra.data.sync.SyncScheduler
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.jsonObject
import javax.inject.Inject
import javax.inject.Singleton

/** What the SOS screen shows after sending (UI-UX §6.2, TRD FR-A03). */
sealed interface SosOutcome {
    val caseId: String
    data class SentOnline(override val caseId: String, val shortCode: String?) : SosOutcome
    data class SentBySms(override val caseId: String) : SosOutcome
    /** SEND_SMS denied → composer opened pre-filled, user taps send (FR-A04). */
    data class ComposerOpened(override val caseId: String) : SosOutcome
    /** No SMS path → IVR call placed; the helpline button is the final fallback. */
    data class CallingIvr(override val caseId: String, val helpline: String) : SosOutcome
}

data class ChannelNumbers(val sms: String, val ivr: String, val helpline: String)

/** Category → SMS letter (API-Guide §10.1, `emergency_categories.sms_letter`). */
val SMS_LETTER = mapOf("pregnancy" to "P", "newborn" to "N", "injury" to "I", "breathing" to "B", "unconscious" to "U", "other" to "O")

@Singleton
class SosDispatcher @Inject constructor(
    @ApplicationContext private val ctx: Context,
    private val db: AppDatabase,
    private val api: Api,
    private val outbox: Outbox,
    private val session: Session,
    private val net: Connectivity,
    private val locator: Locator,
    private val applier: ChangeApplier,
    private val scheduler: SyncScheduler,
) {
    /**
     * Step 1 of FR-A03: write Case + P0 outbox row in one Room transaction BEFORE any network — the request
     * survives process death and is later merged server-side by its idempotency key.
     */
    suspend fun raise(activity: Activity?, category: String, patientId: String?, householdId: String?,
                      patientShortCode: String?, flags: List<String> = emptyList()): SosOutcome {
        val caseId = Ids.uuid7()
        val key = caseId  // idempotencyKey = caseId (API-Guide §6.4.1)
        val tag = Ids.idemTag(key)
        val fix = locator.lastKnown()
        val now = System.currentTimeMillis()
        val payload = jsonOf(
            "caseId" to caseId, "idempotencyKey" to key, "patientId" to patientId, "householdId" to householdId,
            "category" to category, "flags" to flags,
            "pickup" to fix?.let { mapOf("lat" to it.lat, "lng" to it.lng, "accuracyM" to it.accuracyM) },
            "locationSource" to if (fix != null) "gps" else null, "recordedAt" to Time.iso(now),
        )
        db.withTransaction {
            db.cases().upsert(CaseEntity(id = caseId, shortCode = null, type = "sos", channel = "app", patientId = patientId,
                householdId = householdId, category = category, status = "created", statusChangedAt = now,
                statusIsOptimistic = true, currentFacilityName = null, currentFacilityPhone = null, legCurrent = null,
                legTotal = null, etaMinutes = null, pickupLat = fix?.lat, pickupLng = fix?.lng,
                locationSource = if (fix != null) "gps" else "household", idempotencyKey = key, idemTag = tag,
                sentVia = null, serverConfirmed = false, recordedAt = now))
            outbox.add("case", caseId, "command", payload, Priority.EMERGENCY, name = "CreateSOS", opId = key)
        }

        // Step 2: validated internet → POST /sos with an 8 s budget
        if (net.isValidated() && (session.accessToken != null || session.refreshToken() != null)) {
            val resp = withTimeoutOrNull(8_000) { runCatching { api.sos(key, payload) }.getOrNull() }
            if (resp != null && resp.isSuccessful) {
                val body = resp.body()!!
                db.withTransaction {
                    db.outbox().ack(listOf(key), System.currentTimeMillis())
                    db.cases().get(caseId)?.let {
                        db.cases().upsert(it.copy(shortCode = body.str("shortCode"), status = body.str("status") ?: it.status,
                            sentVia = "api", serverConfirmed = true, statusIsOptimistic = false))
                    }
                }
                val serverId = body.str("caseId") ?: caseId
                applier.rekeyCase(caseId, serverId, body.str("shortCode"), body.str("status"))  // dedupe may return an existing case
                refresh(serverId)
                return SosOutcome.SentOnline(serverId, body.str("shortCode"))
            }
        }
        scheduler.syncEmergency()  // keeps retrying the same key in the background (TRD §6.5)

        // Step 3: SMS — the outbox row stays pending so the app sync later merges by the same key
        val numbers = channelNumbers()
        val text = smsText(patientShortCode, category, fix?.lat, fix?.lng, tag)
        if (simReady()) {
            if (granted(Manifest.permission.SEND_SMS)) {
                if (sendSms(numbers.sms, text)) {
                    markSms(caseId, key)
                    return SosOutcome.SentBySms(caseId)
                }
            } else if (activity != null) {
                activity.startActivity(Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:${numbers.sms}")).putExtra("sms_body", text))
                markSms(caseId, key)
                return SosOutcome.ComposerOpened(caseId)
            }
        }
        // Step 4: IVR call, then helpline as final fallback
        activity?.let { callIvr(it, numbers.ivr) }
        db.cases().get(caseId)?.let { db.cases().upsert(it.copy(sentVia = "ivr")) }
        return SosOutcome.CallingIvr(caseId, numbers.helpline)
    }

    /** `SOS <patientShortCode|-> <category> <lat>,<lng> #<tag>` (TRD §7.1). */
    fun smsText(shortCode: String?, category: String, lat: Double?, lng: Double?, tag: String): String {
        val loc = if (lat != null && lng != null) " %.5f,%.5f".format(java.util.Locale.US, lat, lng) else ""
        return "SOS ${shortCode ?: "-"} ${SMS_LETTER[category] ?: "O"}$loc #$tag"
    }

    suspend fun channelNumbers(): ChannelNumbers {
        val rows = db.reference().byPrefix("channel_number:").map { AppJson.parseToJsonElement(it.json).jsonObject }
        val district = session.me()?.districtCode ?: BuildConfig.DEFAULT_DISTRICT
        fun pick(kind: String) = rows.firstOrNull { it.str("kind") == kind && it.str("districtCode") == district }?.str("numberE164")
            ?: rows.firstOrNull { it.str("kind") == kind }?.str("numberE164")
        return ChannelNumbers(sms = pick("sms") ?: BuildConfig.SMS_NUMBER, ivr = pick("ivr") ?: BuildConfig.IVR_NUMBER,
            helpline = pick("helpline") ?: BuildConfig.HELPLINE)
    }

    private suspend fun markSms(caseId: String, key: String) {
        db.withTransaction {
            db.outbox().markSms(key)
            db.cases().get(caseId)?.let { db.cases().upsert(it.copy(sentVia = "sms", channel = "sms")) }
        }
    }

    private suspend fun refresh(caseId: String) {
        runCatching { api.case(caseId).body()?.let { applier.upsertCase(caseId, it) } }
    }

    private fun granted(p: String) = ContextCompat.checkSelfPermission(ctx, p) == PackageManager.PERMISSION_GRANTED

    private fun simReady(): Boolean {
        val tm = ctx.getSystemService(TelephonyManager::class.java) ?: return false
        return tm.simState == TelephonyManager.SIM_STATE_READY
    }

    /** Waits up to 20 s for the SENT PendingIntent (FR-A03); a delivery report is UI-only. */
    private suspend fun sendSms(to: String, text: String): Boolean {
        val action = "in.aapatmitra.SMS_SENT.${System.nanoTime()}"
        val done = CompletableDeferred<Boolean>()
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) { done.complete(resultCode == Activity.RESULT_OK) }
        }
        ContextCompat.registerReceiver(ctx, receiver, IntentFilter(action), ContextCompat.RECEIVER_NOT_EXPORTED)
        return try {
            val sent = PendingIntent.getBroadcast(ctx, 0, Intent(action).setPackage(ctx.packageName),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_ONE_SHOT)
            @Suppress("DEPRECATION")
            val sms = if (Build.VERSION.SDK_INT >= 31) ctx.getSystemService(SmsManager::class.java) else SmsManager.getDefault()
            sms.sendTextMessage(to, null, text, sent, null)
            withTimeoutOrNull(20_000) { done.await() } ?: false
        } catch (_: Exception) {
            false
        } finally {
            runCatching { ctx.unregisterReceiver(receiver) }
        }
    }

    fun callIvr(activity: Activity, number: String) = call(activity, number)

    fun call(activity: Activity, number: String) {
        val action = if (granted(Manifest.permission.CALL_PHONE)) Intent.ACTION_CALL else Intent.ACTION_DIAL
        runCatching { activity.startActivity(Intent(action, Uri.parse("tel:$number"))) }
    }
}
