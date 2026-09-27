package `in`.aapatmitra.data.sync

import androidx.room.withTransaction
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.arr
import `in`.aapatmitra.core.bool
import `in`.aapatmitra.core.dbl
import `in`.aapatmitra.core.int
import `in`.aapatmitra.core.long
import `in`.aapatmitra.core.obj
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.local.CaseEntity
import `in`.aapatmitra.data.local.CaseEventEntity
import `in`.aapatmitra.data.local.FacilityEntity
import `in`.aapatmitra.data.local.HealthEntryEntity
import `in`.aapatmitra.data.local.HouseholdEntity
import `in`.aapatmitra.data.local.IncentiveEntity
import `in`.aapatmitra.data.local.LegEntity
import `in`.aapatmitra.data.local.PatientEntity
import `in`.aapatmitra.data.local.ReferenceKv
import `in`.aapatmitra.data.local.RideOfferEntity
import `in`.aapatmitra.data.local.TaskEntity
import `in`.aapatmitra.data.session.Session
import `in`.aapatmitra.domain.Names
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import javax.inject.Inject
import javax.inject.Singleton

/** Server rows → Room (FR-A01: the network writes Room, screens observe Room). */
@Singleton
class ChangeApplier @Inject constructor(private val db: AppDatabase, private val session: Session) {

    /** Entities kept as small JSON documents in `reference_kv`, keyed `<entity>:<id>`. */
    private val kvEntities = setOf("village", "village_waypoint", "capability", "emergency_category", "symptom", "risk_rule",
        "config", "channel_number", "message_template", "facility_capability", "patient_cohort", "patient_condition",
        "patient_medication", "consent", "care_plan", "teleconsult_session", "attachment", "volunteer_profile", "vehicle")

    suspend fun apply(changes: List<JsonObject>) = db.withTransaction {
        for (c in changes) {
            val entity = c.str("entity") ?: continue
            val id = c.str("id") ?: continue
            if (c.str("op") == "delete") delete(entity, id) else c.obj("data")?.let { upsert(entity, id, it) }
        }
    }

    private suspend fun delete(entity: String, id: String) {
        when (entity) {
            "household" -> db.households().tombstone(id)
            "patient" -> db.households().tombstonePatient(id)
            "follow_up_task" -> db.tasks().delete(id)
            "case" -> db.cases().delete(id)
            "transport_leg" -> db.cases().deleteLeg(id)
            else -> db.reference().delete("$entity:$id")
        }
    }

    private suspend fun upsert(entity: String, id: String, d: JsonObject) {
        val now = System.currentTimeMillis()
        when (entity) {
            "household" -> {
                val local = db.households().get(id)
                if (local?.dirty == true) return  // local edit not yet acked — server copy returns after push
                db.households().upsert(HouseholdEntity(id = id, villageId = d.str("villageId") ?: "", ashaId = d.str("ashaId"),
                    houseNumber = d.str("houseNumber"), headMemberId = d.str("headMemberId"),
                    lat = d.obj("location")?.dbl("lat"), lng = d.obj("location")?.dbl("lng"),
                    locationSource = d.str("locationSource"), registeredPhoneMasked = d.str("registeredPhoneMasked"),
                    recordedAt = Time.parse(d.str("recordedAt")) ?: now, serverVersion = d.long("version")))
            }
            "patient" -> {
                val local = db.households().patient(id)
                if (local?.dirty == true) return
                val name = d.str("name") ?: local?.name ?: ""
                db.households().upsertPatient(PatientEntity(id = id, householdId = d.str("householdId") ?: "",
                    shortCode = d.str("shortCode"), name = name, nameNormalised = Names.normalise(name), sex = d.str("sex") ?: "O",
                    dateOfBirth = d.str("dateOfBirth"), dobIsEstimated = d.bool("dobIsEstimated") ?: false,
                    relationshipToHead = d.str("relationshipToHead"), phone = d.str("phone") ?: d.str("phoneMasked"),
                    bloodGroup = d.str("bloodGroup"), highRisk = d.bool("highRisk") ?: false,
                    cohortsJson = (d.arr("cohorts") ?: JsonArray(emptyList())).toString(),
                    consentsJson = (d.arr("consents") ?: JsonArray(emptyList())).toString(),
                    recordedAt = Time.parse(d.str("recordedAt")) ?: now, serverVersion = d.long("version")))
            }
            "health_record_entry" -> db.entries().upsert(HealthEntryEntity(id = id, patientId = d.str("patientId") ?: "",
                kind = d.str("kind") ?: "note", highRisk = d.bool("highRisk") ?: false, highRiskSource = "server",
                riskFlagsJson = (d.arr("riskFlags") ?: JsonArray(emptyList())).toString(),
                vitalsJson = d.obj("vitals")?.toString(), symptomsJson = d.arr("symptoms")?.toString(), notes = d.str("notes"),
                authorName = d.str("authorName"), recordedAt = Time.parse(d.str("recordedAt")) ?: now))
            "follow_up_task" -> {
                val local = db.tasks().get(id)
                if (local?.dirty == true) return
                db.tasks().upsert(TaskEntity(id = id, patientId = d.str("patientId") ?: "", patientName = d.str("patientName"),
                    ashaId = d.str("ashaId") ?: "", taskType = d.str("taskType") ?: "", title = d.str("title"),
                    priority = d.str("priority") ?: "normal", dueDate = d.str("dueDate") ?: Time.today(),
                    status = d.str("status") ?: "open", doneAt = Time.parse(d.str("doneAt")), version = d.long("version")))
            }
            "case" -> upsertCase(id, d)
            "case_event" -> db.cases().upsertEvent(CaseEventEntity(id = id, caseId = d.str("caseId") ?: "",
                action = d.str("action") ?: "", toStatus = d.str("toStatus"), occurredAt = Time.parse(d.str("occurredAt")) ?: now))
            "transport_leg" -> db.cases().upsertLeg(leg(d))
            "volunteer_offer" -> {
                val status = d.str("status") ?: "pending"
                val prev = db.rides().offer(id)
                db.rides().upsert(RideOfferEntity(id = id, legId = d.str("legId") ?: "", caseId = prev?.caseId ?: "",
                    caseShortCode = d.str("caseShortCode") ?: "", category = d.str("category"),
                    villageName = d.str("pickupLabel"), landmark = prev?.landmark,
                    distanceKm = d.int("distanceM")?.let { it / 1000 }, destinationLabel = d.str("destinationLabel"),
                    verified = prev?.verified ?: false, expiresAt = Time.parse(d.str("expiresAt")) ?: now,
                    serverSkewMs = session.skewMs(), status = status))
            }
            "facility" -> {
                val caps = d.arr("capabilities")?.mapNotNull { it as? JsonObject }
                    ?.filter { it.bool("available") == true }?.mapNotNull { it.str("code") } ?: emptyList()
                db.reference().upsertFacility(FacilityEntity(id = id, name = d.str("name") ?: "", level = d.str("level") ?: "",
                    districtCode = d.str("districtCode") ?: "", lat = d.obj("location")?.dbl("lat") ?: 0.0,
                    lng = d.obj("location")?.dbl("lng") ?: 0.0, status = d.str("status") ?: "open",
                    bedsAvailable = d.int("bedsAvailable") ?: 0,
                    capabilityUpdatedAt = Time.parse(d.str("capabilityUpdatedAt")) ?: now, availableCaps = caps.joinToString(",")))
            }
            "incentive" -> db.rides().upsertIncentive(IncentiveEntity(id = id, kind = d.str("kind") ?: "",
                credits = d.int("credits") ?: 0, caseShortCode = d.str("caseShortCode"), villageName = d.str("villageName"),
                state = d.str("state") ?: "verified", createdAt = Time.parse(d.str("createdAt")) ?: now))
            "user_self" -> session.saveMe(d)
            "volunteer_profile" -> {  // the Ride switch mirrors the server's availability
                db.reference().put(ReferenceKv("$entity:$id", d.toString(), now))
                d.bool("available")?.let { db.reference().put(ReferenceKv("ride:available", it.toString(), now)) }
            }
            else -> if (entity in kvEntities) db.reference().put(ReferenceKv("$entity:$id", d.toString(), now))
        }
    }

    fun leg(d: JsonObject): LegEntity {
        val cust = d.obj("custodian")
        val next = d.obj("nextCustodian")
        return LegEntity(id = d.str("id")!!, caseId = d.str("caseId") ?: "", legOrder = d.int("legOrder") ?: 1,
            fromLabel = d.str("fromLabel"), toLabel = d.str("toLabel"),
            fromLat = d.obj("fromPoint")?.dbl("lat"), fromLng = d.obj("fromPoint")?.dbl("lng"),
            toLat = d.obj("toPoint")?.dbl("lat"), toLng = d.obj("toPoint")?.dbl("lng"), status = d.str("status") ?: "open",
            custodianUserId = cust?.str("userId") ?: d.str("custodianUserId"), custodianName = cust?.str("firstName"),
            custodianPhone = cust?.str("phone") ?: cust?.str("phoneMasked"),
            nextLegId = d.str("nextLegId"), nextCustodianUserId = next?.str("userId"), nextCustodianDeviceId = next?.str("deviceId"),
            nextCustodianKey = next?.str("publicKeyEd25519"), handoverState = d.str("handoverState"), etaSeconds = d.int("etaSeconds"))
    }

    /**
     * Server wins on status; local SOS bookkeeping (idempotency key, channel used) is preserved.
     * Accepts both the flat sync projection and CaseDetailOut (`{case, events, legs, …}`) from GET /cases/{id}.
     */
    suspend fun upsertCase(id: String, raw: JsonObject) {
        val d = raw.obj("case")?.let { c -> JsonObject(c + listOfNotNull(raw["legs"]?.let { "legs" to it }, raw["events"]?.let { "events" to it })) } ?: raw
        val prev = db.cases().get(id)
        val now = System.currentTimeMillis()
        val fac = d.obj("currentFacility")
        val progress = d.obj("legProgress")
        db.cases().upsert(CaseEntity(id = id, shortCode = d.str("shortCode") ?: prev?.shortCode, type = d.str("type") ?: prev?.type ?: "sos",
            channel = d.str("channel") ?: prev?.channel ?: "app", patientId = d.str("patientId") ?: prev?.patientId,
            householdId = d.str("householdId") ?: prev?.householdId, category = d.str("emergencyCategory") ?: prev?.category,
            status = d.str("status") ?: prev?.status ?: "created",
            statusChangedAt = Time.parse(d.str("statusChangedAt")) ?: now, statusIsOptimistic = false,
            currentFacilityName = fac?.str("name") ?: d.str("currentFacilityName"), currentFacilityPhone = fac?.str("phone"),
            legCurrent = progress?.int("current"), legTotal = progress?.int("total"), etaMinutes = d.int("etaMin"),
            pickupLat = d.obj("pickupPoint")?.dbl("lat") ?: prev?.pickupLat, pickupLng = d.obj("pickupPoint")?.dbl("lng") ?: prev?.pickupLng,
            locationSource = d.str("locationSource") ?: prev?.locationSource ?: "village",
            idempotencyKey = prev?.idempotencyKey ?: id, idemTag = prev?.idemTag ?: "", sentVia = prev?.sentVia ?: "api",
            serverConfirmed = true, recordedAt = Time.parse(d.str("recordedAt")) ?: prev?.recordedAt ?: now,
            json = d.toString()))
        d.arr("legs")?.forEach { l -> (l as? JsonObject)?.let { db.cases().upsertLeg(leg(it)) } }
        d.arr("events")?.forEach { e ->
            (e as? JsonObject)?.let {
                db.cases().upsertEvent(CaseEventEntity(id = it.str("id")!!, caseId = id, action = it.str("action") ?: "",
                    toStatus = it.str("toStatus"), occurredAt = Time.parse(it.str("occurredAt")) ?: now))
            }
        }
    }

    /**
     * The server merged our SOS into an existing case (same idempotency tag / 30-min dedupe, API-Guide §6.4.1):
     * move the local row to the server id and remember the alias so open screens follow it.
     */
    suspend fun rekeyCase(localId: String, serverId: String, shortCode: String?, status: String?) {
        if (localId == serverId) return
        db.withTransaction {
            val local = db.cases().get(localId) ?: return@withTransaction
            db.cases().delete(localId)
            val existing = db.cases().get(serverId)
            db.cases().upsert((existing ?: local.copy(id = serverId, idempotencyKey = serverId)).copy(
                shortCode = shortCode ?: existing?.shortCode ?: local.shortCode, status = status ?: existing?.status ?: local.status,
                sentVia = local.sentVia, serverConfirmed = true, statusIsOptimistic = false))
            db.reference().put(ReferenceKv("case_alias:$localId", serverId, System.currentTimeMillis()))
        }
    }

    fun parse(line: String): JsonObject = AppJson.parseToJsonElement(line).jsonObject
}
