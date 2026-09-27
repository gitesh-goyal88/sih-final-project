package `in`.aapatmitra.data.repo

import androidx.room.withTransaction
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.jsonOf
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.device.Locator
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.local.HouseholdEntity
import `in`.aapatmitra.data.local.PatientEntity
import `in`.aapatmitra.data.session.Session
import `in`.aapatmitra.data.sync.Outbox
import `in`.aapatmitra.data.sync.Priority
import `in`.aapatmitra.data.sync.SyncScheduler
import `in`.aapatmitra.domain.Names
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.jsonObject
import java.time.LocalDate
import javax.inject.Inject
import javax.inject.Singleton

data class MemberDraft(
    val name: String = "", val sex: String = "F", val ageYears: Int? = null, val phone: String = "",
    val relationship: String = "self", val cohorts: Set<String> = emptySet(),
)

data class Village(val id: String, val name: String)

/** ASHA household registration — fully offline (TRD M1 "household registration offline"). */
@Singleton
class HouseholdRepo @Inject constructor(
    private val db: AppDatabase, private val outbox: Outbox, private val session: Session,
    private val locator: Locator, private val scheduler: SyncScheduler,
) {
    fun households() = db.households().all()
    fun members(hh: String) = db.households().members(hh)
    fun patients() = db.households().allPatients()
    fun search(q: String) = db.households().search(Names.ftsQuery(q))
    fun patientFlow(id: String) = db.households().patientFlow(id)
    suspend fun patient(id: String) = db.households().patient(id)
    suspend fun household(id: String) = db.households().get(id)

    suspend fun villages(): List<Village> = db.reference().byPrefix("village:").map {
        val j = AppJson.parseToJsonElement(it.json).jsonObject
        Village(j.str("id")!!, j.str("name") ?: "")
    }.sortedBy { it.name }

    suspend fun villageName(id: String?): String? = id?.let { db.reference().get("village:$it") }
        ?.let { AppJson.parseToJsonElement(it.json).jsonObject.str("name") }

    /**
     * Household + members + verbal consent in ONE outbox op, one Room transaction (API-Guide §6.3, sync
     * `household/create` with `members[]` and `consents[]`). Consent for continuity of care is recorded
     * as verbal, witnessed by the ASHA (TRD §14.4).
     */
    suspend fun create(villageId: String, houseNumber: String, members: List<MemberDraft>, consentGiven: Boolean): String {
        val hid = Ids.uuid7()
        val fix = locator.lastKnown(60 * 60_000)
        val now = System.currentTimeMillis()
        val lang = session.language() ?: "hi"
        val drafts = members.filter { it.name.isNotBlank() }.map { Ids.uuid7() to it }
        val memberJson = drafts.map { (pid, m) ->
            val dob = m.ageYears?.let { LocalDate.now().minusYears(it.toLong()).withDayOfYear(1).toString() }
            mapOf("id" to pid, "name" to m.name.trim(), "sex" to m.sex, "dateOfBirth" to dob, "dobIsEstimated" to (dob != null),
                "relationshipToHead" to m.relationship, "phone" to m.phone.takeIf { it.isNotBlank() }?.let { AuthRepo.normalise(it) },
                "recordedAt" to Time.iso(now),
                "cohorts" to m.cohorts.map { c -> mapOf("id" to Ids.uuid7(), "cohort" to c, "startedOn" to Time.today()) })
        }
        val consents = if (consentGiven) drafts.map { (pid, _) ->
            mapOf("id" to Ids.uuid7(), "patientId" to pid, "purpose" to "continuity_of_care", "status" to "granted",
                "method" to "verbal_witnessed", "language" to lang, "noticeVersion" to "cc-2026-09", "effectiveAt" to Time.iso(now))
        } else emptyList()
        val data = jsonOf("id" to hid, "villageId" to villageId, "houseNumber" to houseNumber.ifBlank { null },
            "location" to fix?.let { mapOf("lat" to it.lat, "lng" to it.lng) }, "locationSource" to if (fix != null) "gps" else null,
            "headMemberId" to drafts.firstOrNull()?.first, "members" to memberJson, "consents" to consents, "recordedAt" to Time.iso(now))
        db.withTransaction {
            db.households().upsert(HouseholdEntity(id = hid, villageId = villageId, ashaId = session.me()?.id, houseNumber = houseNumber,
                headMemberId = drafts.firstOrNull()?.first, lat = fix?.lat, lng = fix?.lng, locationSource = if (fix != null) "gps" else null,
                registeredPhoneMasked = null, recordedAt = now, dirty = true))
            for ((pid, m) in drafts) {
                val cohorts = JsonArray(m.cohorts.map { jsonOf("cohort" to it, "startedOn" to Time.today(), "endedOn" to null) })
                db.households().upsertPatient(PatientEntity(id = pid, householdId = hid, shortCode = null, name = m.name.trim(),
                    nameNormalised = Names.normalise(m.name), sex = m.sex,
                    dateOfBirth = m.ageYears?.let { LocalDate.now().minusYears(it.toLong()).withDayOfYear(1).toString() },
                    dobIsEstimated = true, relationshipToHead = m.relationship, phone = m.phone.ifBlank { null }, bloodGroup = null,
                    highRisk = false, cohortsJson = cohorts.toString(),
                    consentsJson = if (consentGiven) """[{"purpose":"continuity_of_care","status":"granted"}]""" else "[]",
                    recordedAt = now, dirty = true))
            }
            outbox.add("household", hid, "create", data, Priority.ROUTINE)
        }
        scheduler.syncSoon()
        return hid
    }
}
