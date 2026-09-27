package `in`.aapatmitra.data.local

import androidx.room.Entity
import androidx.room.Fts4
import androidx.room.Index
import androidx.room.PrimaryKey

/*
 * Room mirrors the server entities the device needs, scoped by role (TRD §4.3, database.md §14).
 * Room is the single source of truth for every screen (B1). Names/phones are plaintext INSIDE the
 * SQLCipher-encrypted file (B6) — needed for offline search and display; they leave only over TLS.
 * `dirty` = local change not yet acked; `serverVersion` null = never synced.
 */

@Entity(tableName = "households", indices = [Index("villageId"), Index("houseNumber")])
data class HouseholdEntity(
    @PrimaryKey val id: String, val villageId: String, val ashaId: String?, val houseNumber: String?,
    val headMemberId: String?, val lat: Double?, val lng: Double?, val locationSource: String?,
    val registeredPhoneMasked: String?, val recordedAt: Long, val dirty: Boolean = false, val serverVersion: Long? = null,
    val deleted: Boolean = false,
)

@Entity(tableName = "patients", indices = [Index("householdId"), Index(value = ["shortCode"])])
data class PatientEntity(
    @PrimaryKey val id: String, val householdId: String, val shortCode: String?, val name: String,
    val nameNormalised: String, val sex: String, val dateOfBirth: String?, val dobIsEstimated: Boolean,
    val relationshipToHead: String?, val phone: String?, val bloodGroup: String?, val highRisk: Boolean,
    val cohortsJson: String = "[]", val consentsJson: String = "[]", val recordedAt: Long,
    val dirty: Boolean = false, val serverVersion: Long? = null, val deleted: Boolean = false,
)

/** ASHA offline search "by name or house number" — the server never stores a searchable name (schema R8). */
@Fts4(contentEntity = PatientEntity::class)
@Entity(tableName = "patients_fts")
data class PatientFts(val nameNormalised: String)

@Entity(tableName = "health_entries", indices = [Index("patientId", "recordedAt")])
data class HealthEntryEntity(
    @PrimaryKey val id: String, val patientId: String, val kind: String, val highRisk: Boolean,
    val highRiskSource: String, val riskFlagsJson: String = "[]", val vitalsJson: String?, val symptomsJson: String?,
    val notes: String?, val authorName: String?, val recordedAt: Long, val pending: Boolean = false,
)

@Entity(tableName = "follow_up_tasks", indices = [Index("ashaId", "status", "dueDate"), Index("patientId")])
data class TaskEntity(
    @PrimaryKey val id: String, val patientId: String, val patientName: String?, val ashaId: String,
    val taskType: String, val title: String?, val priority: String, val dueDate: String, val status: String,
    val doneAt: Long?, val version: Long?, val dirty: Boolean = false,
)

@Entity(tableName = "cases", indices = [Index("householdId"), Index("status"), Index(value = ["idempotencyKey"], unique = true)])
data class CaseEntity(
    @PrimaryKey val id: String, val shortCode: String?, val type: String, val channel: String,
    val patientId: String?, val householdId: String?, val category: String?, val status: String,
    val statusChangedAt: Long, val statusIsOptimistic: Boolean, val currentFacilityName: String?,
    val currentFacilityPhone: String?, val legCurrent: Int?, val legTotal: Int?, val etaMinutes: Int?,
    val pickupLat: Double?, val pickupLng: Double?, val locationSource: String,
    val idempotencyKey: String, val idemTag: String,
    val sentVia: String?,          // api | sms | ivr | helpline
    val serverConfirmed: Boolean,  // server ack received ("AM OK" / 202)
    val recordedAt: Long, val json: String = "{}",
)

@Entity(tableName = "case_events", indices = [Index("caseId", "occurredAt")])
data class CaseEventEntity(
    @PrimaryKey val id: String, val caseId: String, val action: String, val toStatus: String?, val occurredAt: Long,
)

@Entity(tableName = "transport_legs", indices = [Index("caseId")])
data class LegEntity(
    @PrimaryKey val id: String, val caseId: String, val legOrder: Int, val fromLabel: String?, val toLabel: String?,
    val fromLat: Double?, val fromLng: Double?, val toLat: Double?, val toLng: Double?, val status: String,
    val custodianUserId: String?, val custodianName: String?, val custodianPhone: String?,
    val nextLegId: String?, val nextCustodianUserId: String?, val nextCustodianDeviceId: String?, val nextCustodianKey: String?,
    val handoverState: String?, val etaSeconds: Int?,
)

@Entity(tableName = "volunteer_offers", indices = [Index("status", "expiresAt")])
data class RideOfferEntity(
    @PrimaryKey val id: String, val legId: String, val caseId: String, val caseShortCode: String, val category: String?,
    val villageName: String?, val landmark: String?, val distanceKm: Int?, val destinationLabel: String?,
    val verified: Boolean, val expiresAt: Long, val serverSkewMs: Long, val status: String,
)

@Entity(tableName = "facilities", indices = [Index("districtCode")])
data class FacilityEntity(
    @PrimaryKey val id: String, val name: String, val level: String, val districtCode: String, val lat: Double,
    val lng: Double, val status: String, val bedsAvailable: Int, val capabilityUpdatedAt: Long,
    val availableCaps: String,     // comma list of AVAILABLE capability codes
)

@Entity(tableName = "incentives")
data class IncentiveEntity(
    @PrimaryKey val id: String, val kind: String, val credits: Int, val caseShortCode: String?, val villageName: String?,
    val state: String, val createdAt: Long,
)

/** Catalogs, config, channel numbers, own user profile — small JSON documents keyed by name. */
@Entity(tableName = "reference_kv")
data class ReferenceKv(@PrimaryKey val key: String, val json: String, val updatedAt: Long)

/** TRD FR-A02: every user write inserts an outbox row in the same Room transaction as the domain change. */
@Entity(tableName = "outbox", indices = [Index("state", "priority", "createdAt")])
data class OutboxItem(
    @PrimaryKey val opId: String,             // UUIDv7, also the Idempotency-Key
    val entity: String,
    val entityId: String,
    val op: String,                           // create | update | command
    val commandName: String?,
    val payloadJson: String,
    val priority: Int,                        // 0 EMERGENCY · 1 CASE_EVENT · 2 CLINICAL · 3 ROUTINE
    val createdAt: Long,
    val hlc: String,
    val attempts: Int = 0,
    val lastError: String? = null,
    val lastErrorCode: String? = null,
    val state: String = "pending",            // pending | in_flight | acked | failed | rejected
    val sentViaSms: Boolean = false,          // SOS already sent by SMS — still synced later with the same key
    val dependsOnOpId: String? = null,
    val ackedAt: Long? = null,
)

@Entity(tableName = "sync_state")
data class SyncStateEntity(
    @PrimaryKey val key: String = "main", val cursor: String?, val lastPullAt: Long?, val lastPushAt: Long?,
    val clockSkewMs: Long = 0, val snapshotRequired: Boolean = true,
)

@Entity(tableName = "sync_conflicts")
data class SyncConflictEntity(
    @PrimaryKey val opId: String, val entity: String, val entityId: String, val fieldsJson: String, val code: String,
    val createdAt: Long, val resolved: Boolean = false,
)
