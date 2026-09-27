package `in`.aapatmitra.data.repo

import androidx.room.withTransaction
import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.arr
import `in`.aapatmitra.core.bool
import `in`.aapatmitra.core.dbl
import `in`.aapatmitra.core.int
import `in`.aapatmitra.core.jsonOf
import `in`.aapatmitra.core.obj
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.remote.Api
import `in`.aapatmitra.data.remote.problemCode
import `in`.aapatmitra.data.sync.ChangeApplier
import `in`.aapatmitra.data.sync.Outbox
import `in`.aapatmitra.data.sync.Priority
import `in`.aapatmitra.data.sync.SyncScheduler
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.coroutines.flow.map
import javax.inject.Inject
import javax.inject.Singleton

data class FacilityOption(
    val id: String, val name: String, val level: String, val distanceKm: Double, val etaMin: Int, val matched: List<String>,
    val missing: List<String>, val stale: Boolean, val beds: Int,
)

data class MatchResult(val options: List<FacilityOption>, val noCapable: Boolean, val needed: List<String>, val computedAt: Long)

/** Case tracking, commands and ASHA referrals (API-Guide §6.4–6.6). */
@Singleton
class CaseRepo @Inject constructor(
    private val db: AppDatabase, private val api: Api, private val applier: ChangeApplier, private val outbox: Outbox,
    private val scheduler: SyncScheduler,
) {
    fun openCases() = db.cases().open()
    fun case(id: String) = db.cases().flow(id)
    /** Follows a local SOS id to the server case it was merged into (see ChangeApplier.rekeyCase). */
    fun resolved(id: String) = db.reference().flow("case_alias:$id").map { it?.json ?: id }
    fun legs(id: String) = db.cases().legs(id)
    fun events(id: String) = db.cases().events(id)
    fun casesFor(patientId: String) = db.cases().forPatient(patientId)

    /** Poll target while a case screen is visible (`track.pollAfterS` = 5 s); writes Room, screens observe Room. */
    suspend fun refresh(id: String): Boolean = runCatching {
        val r = api.case(id)
        r.body()?.let { applier.upsertCase(id, it); true } ?: false
    }.getOrDefault(false)

    suspend fun refreshMine() = runCatching {
        api.myCases().body()?.arr("data")?.forEach { c -> (c as? JsonObject)?.let { applier.upsertCase(it.str("id")!!, it) } }
    }

    /** Online first; offline → the same command rides the outbox (P1) and is validated on replay (TRD §6.4). */
    suspend fun command(caseId: String, name: String, args: Map<String, Any?>): String? {
        val key = Ids.uuid7()
        val body = jsonOf("command" to name, "args" to args, "recordedAt" to Time.iso(System.currentTimeMillis()))
        val r = runCatching { api.caseCommand(caseId, key, body) }.getOrNull()
        if (r != null) {
            if (r.isSuccessful) { r.body()?.let { applier.upsertCase(caseId, it) }; return null }
            return r.problemCode() ?: "HTTP_${r.code()}"
        }
        db.withTransaction {
            outbox.add("case", caseId, "command", jsonOf("caseId" to caseId, "args" to args), Priority.CASE_EVENT, name = name, opId = key)
        }
        scheduler.syncEmergency()
        return null
    }

    suspend fun cancel(caseId: String, reason: String) = command(caseId, "Cancel", mapOf("reason" to reason))

    /** Referral matching needs the server (live capability); shown with "Updated N min ago" (UI-UX §6.3). */
    suspend fun match(patientId: String, needs: List<String>): Result<MatchResult> = runCatching {
        val r = api.match(patientId, needs.joinToString(","))
        val out = r.body() ?: error(r.problemCode() ?: "HTTP_${r.code()}")
        MatchResult(
            options = out.arr("data")!!.take(3).map { it.jsonObject }.map { m ->
                val f = m.obj("facility")!!
                val why = m.obj("reasons")!!
                FacilityOption(f.str("id")!!, f.str("name")!!, f.str("level") ?: "", why.dbl("distanceKm") ?: 0.0, why.int("etaMin") ?: 0,
                    why.arr("capabilitiesMatched")!!.map { it.toString().trim('"') }, why.arr("capabilitiesMissing")!!.map { it.toString().trim('"') },
                    why.bool("stale") == true, why.int("beds") ?: 0)
            },
            noCapable = out.bool("noCapableFacility") == true,
            needed = out.arr("neededCapabilities")?.map { it.toString().trim('"') } ?: needs,
            computedAt = Time.parse(out.str("computedAt")) ?: System.currentTimeMillis())
    }

    /** POST /cases (online) or CreateReferral via outbox (offline); both use the same case id + key. */
    suspend fun createReferral(patientId: String, needs: List<String>, preferredFacilityId: String?, reason: String?): String {
        val id = Ids.uuid7()
        val body = jsonOf("id" to id, "idempotencyKey" to id, "type" to "referral", "patientId" to patientId,
            "neededCapabilities" to needs, "preferredFacilityId" to preferredFacilityId, "referralReason" to reason,
            "recordedAt" to Time.iso(System.currentTimeMillis()))
        val r = runCatching { api.createReferral(id, body) }.getOrNull()
        if (r?.isSuccessful == true) {
            r.body()?.let { applier.upsertCase(id, it) }
        } else {
            db.withTransaction {
                applier.upsertCase(id, jsonOf("id" to id, "type" to "referral", "patientId" to patientId, "status" to "created"))
                db.cases().get(id)?.let { db.cases().upsert(it.copy(serverConfirmed = false, statusIsOptimistic = true, sentVia = null)) }
                outbox.add("case", id, "command", body, Priority.EMERGENCY, name = "CreateReferral", opId = id)
            }
            scheduler.syncEmergency()
        }
        return id
    }
}
