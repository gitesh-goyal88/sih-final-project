package `in`.aapatmitra.data.repo

import android.util.Base64
import androidx.room.withTransaction
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.arr
import `in`.aapatmitra.core.bool
import `in`.aapatmitra.core.int
import `in`.aapatmitra.core.jsonOf
import `in`.aapatmitra.core.obj
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.device.Locator
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.local.ReferenceKv
import `in`.aapatmitra.data.local.RideOfferEntity
import `in`.aapatmitra.data.remote.Api
import `in`.aapatmitra.data.remote.problemCode
import `in`.aapatmitra.data.session.DeviceKeys
import `in`.aapatmitra.data.session.Session
import `in`.aapatmitra.data.sync.ChangeApplier
import `in`.aapatmitra.data.sync.Outbox
import `in`.aapatmitra.data.sync.Priority
import `in`.aapatmitra.data.sync.SyncScheduler
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import java.security.SecureRandom
import javax.inject.Inject
import javax.inject.Singleton

data class Contact(val label: String, val name: String?, val phone: String?)

data class Points(val credits: Int, val rides: Int, val rank: Int?, val entries: List<JsonObject>, val board: List<JsonObject>)

/** Volunteer: availability, offers, leg steps and custody hand-over (API-Guide §6.8). */
@Singleton
class RideRepo @Inject constructor(
    private val db: AppDatabase, private val api: Api, private val session: Session, private val applier: ChangeApplier,
    private val outbox: Outbox, private val locator: Locator, private val scheduler: SyncScheduler,
) {
    val available: Flow<Boolean> = db.reference().flow("ride:available").map { it?.json == "true" }
    fun offers(now: Long) = db.rides().pending(now)
    fun myActiveLegs(me: String) = db.cases().myActiveLegs(me)
    fun leg(caseId: String) = db.cases().legs(caseId)
    fun contactsFlow(legId: String) = db.reference().flow("leg_view:$legId").map { kv ->
        kv?.let { AppJson.parseToJsonElement(it.json).jsonObject }
    }

    private fun loc() = locator.lastKnown()?.let { mapOf("lat" to it.lat, "lng" to it.lng, "accuracyM" to it.accuracyM) }
    private suspend fun freshLoc() = locator.fresh()?.let { mapOf("lat" to it.lat, "lng" to it.lng, "accuracyM" to it.accuracyM) }

    /** Returns null on success or an error code (e.g. VOLUNTEER_NOT_VERIFIED). */
    suspend fun setAvailable(on: Boolean): String? {
        val r = runCatching { api.availability(Ids.uuid7(), jsonOf("available" to on, "location" to loc())) }.getOrNull()
        if (r != null && !r.isSuccessful) return r.problemCode()
        db.withTransaction {
            db.reference().put(ReferenceKv("ride:available", on.toString(), System.currentTimeMillis()))
            if (r == null) outbox.add("volunteer_profile", session.me()?.id ?: "", "update", jsonOf("available" to on), Priority.ROUTINE)
        }
        if (r == null) scheduler.syncSoon()
        return null
    }

    suspend fun pingLocation(legId: String?) {
        val l = loc() ?: return
        runCatching { api.location(Ids.uuid7(), jsonOf("location" to l, "legId" to legId, "at" to Time.iso(System.currentTimeMillis()))) }
    }

    /** No FCM in this build (no google-services.json) → the Ride screen polls every 10 s while ON. */
    suspend fun pollOffers() = runCatching {
        val out = api.offers().body() ?: return@runCatching
        val skew = session.skewMs()
        for (o in out.arr("data") ?: emptyList()) {
            val j = o.jsonObject
            val serverNow = Time.parse(j.str("serverTime")) ?: System.currentTimeMillis()
            val localSkew = serverNow - System.currentTimeMillis()
            db.rides().upsert(RideOfferEntity(id = j.str("offerId")!!, legId = j.str("legId")!!, caseId = j.str("caseId")!!,
                caseShortCode = j.str("caseShortCode") ?: "", category = j.str("category"),
                villageName = j.obj("area")?.str("villageName"), landmark = j.obj("area")?.str("landmark"),
                distanceKm = j.obj("area")?.int("distanceKm"), destinationLabel = j.obj("destination")?.str("label"),
                verified = j.bool("verified") == true, expiresAt = (Time.parse(j.str("expiresAt")) ?: 0) - localSkew,
                serverSkewMs = if (skew != 0L) skew else localSkew, status = "pending"))
        }
        refreshActiveLegs()
    }

    suspend fun refreshActiveLegs() = runCatching {
        val out = api.activeLegs().body() ?: return@runCatching
        for (v in out.arr("data") ?: emptyList()) storeLegView(v.jsonObject)
    }

    private suspend fun storeLegView(view: JsonObject) {
        val leg = view.obj("leg") ?: return
        val me = session.me()?.id
        val next = view.obj("nextCustodian")
        val merged = JsonObject(leg + mapOf("custodian" to jsonOf("userId" to me),
            "nextCustodian" to (next ?: kotlinx.serialization.json.JsonNull)))
        db.withTransaction {
            db.cases().upsertLeg(applier.leg(merged))
            db.reference().put(ReferenceKv("leg_view:${leg.str("id")}", view.toString(), System.currentTimeMillis()))
        }
    }

    suspend fun opened(o: RideOfferEntity) = runCatching { api.offerOpened(o.caseId, o.id, Ids.uuid7()) }

    /** First to accept wins; others get 409 LEG_ALREADY_TAKEN (SEC / API-Guide §6.8.3). */
    suspend fun accept(o: RideOfferEntity): String? {
        val r = runCatching { api.accept(o.caseId, o.legId, Ids.uuid7(), jsonOf("offerId" to o.id, "location" to loc())) }.getOrNull()
            ?: return "NETWORK"
        if (!r.isSuccessful) { db.rides().setStatus(o.id, "expired"); return r.problemCode() ?: "HTTP_${r.code()}" }
        db.rides().setStatus(o.id, "accepted")
        r.body()?.let { storeLegView(it) }
        return null
    }

    suspend fun decline(o: RideOfferEntity) {
        db.rides().setStatus(o.id, "declined")
        val r = runCatching { api.decline(o.caseId, o.legId, Ids.uuid7(), jsonOf("offerId" to o.id)) }.getOrNull()
        if (r == null) {
            outbox.add("transport_leg", o.legId, "command", jsonOf("caseId" to o.caseId, "legId" to o.legId, "offerId" to o.id),
                Priority.CASE_EVENT, name = "DeclineLeg")
            scheduler.syncSoon()
        }
    }

    suspend fun confirmPickup(caseId: String, legId: String, cases: CaseRepo): String? =
        cases.command(caseId, "ConfirmPickup", mapOf("legId" to legId, "location" to loc())).also { refreshActiveLegs() }

    /**
     * Receiver side: build + sign the hand-over assertion shown as a QR (SEC-CUS-01). Online it starts from
     * the server's pre-filled payload; offline it is assembled from the synced leg (same fields).
     */
    suspend fun buildHandoverQr(caseId: String, myLegId: String): String? {
        val here = freshLoc()
        val server = runCatching { api.handoverQrPayload(caseId, myLegId).body() }.getOrNull()
        val assertion = server?.let {
            JsonObject(it + mapOf("gps" to (here?.let { l -> jsonOf("lat" to l["lat"], "lng" to l["lng"]) } ?: kotlinx.serialization.json.JsonNull)))
        } ?: run {
            val legs = db.cases().legs(caseId)
            val mine = db.cases().leg(myLegId) ?: return null
            val prev = legs.first().firstOrNull { it.legOrder == mine.legOrder - 1 } ?: return null
            val nonce = ByteArray(16).also { SecureRandom().nextBytes(it) }
            jsonOf("v" to 1, "legId" to prev.id, "nextLegId" to mine.id, "receiverUserId" to session.me()?.id,
                "receiverDeviceId" to session.deviceId, "nonce" to b64u(nonce),
                "ts" to Time.iso(System.currentTimeMillis() + session.skewMs()),
                "gps" to here?.let { mapOf("lat" to it["lat"], "lng" to it["lng"]) })
        }
        val raw = b64u(assertion.toString().toByteArray())
        val sig = b64u(DeviceKeys.sign(raw.toByteArray()))
        return jsonOf("a" to raw, "s" to sig).toString()
    }

    /** Giver side: submit the scanned QR (or a 6-digit SMS code for an ambulance receiver). */
    suspend fun handover(caseId: String, legId: String, qr: String?, code: String?): String? {
        val here = freshLoc()
        val body = if (qr != null) {
            val j = runCatching { AppJson.parseToJsonElement(qr).jsonObject }.getOrNull() ?: return "HANDOVER_CODE_INVALID"
            jsonOf("method" to "signed_qr", "assertion" to j.str("a"), "signature" to j.str("s"), "location" to here,
                "recordedAt" to Time.iso(System.currentTimeMillis()))
        } else jsonOf("method" to "sms_code", "code" to code, "location" to here, "recordedAt" to Time.iso(System.currentTimeMillis()))
        val r = runCatching { api.handover(caseId, legId, Ids.uuid7(), body) }.getOrNull()
        if (r == null) {
            // Offline: the signed assertion is valid ±30 min — queue it as a P1 leg command (SF-01)
            outbox.add("transport_leg", legId, "command", JsonObject(body + mapOf("caseId" to JsonPrimitive(caseId),
                "legId" to JsonPrimitive(legId))), Priority.CASE_EVENT, name = "Handover")
            scheduler.syncEmergency()
            return null
        }
        if (!r.isSuccessful) return r.problemCode() ?: "HTTP_${r.code()}"
        refreshActiveLegs()
        db.cases().leg(legId)?.let { db.cases().upsertLeg(it.copy(status = "handed_over", handoverState = "verified")) }
        return null
    }

    suspend fun points(): Points? = runCatching {
        val out = api.incentives().body() ?: return null
        val board = runCatching { api.leaderboard().body()?.arr("data")?.map { it.jsonObject } }.getOrNull() ?: emptyList()
        Points(out.int("credits") ?: 0, out.int("activityCount") ?: 0, out.int("rank"),
            out.arr("entries")?.map { it.jsonObject } ?: emptyList(), board.take(5))
    }.getOrNull()

    companion object {
        fun b64u(b: ByteArray): String = Base64.encodeToString(b, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    }
}
