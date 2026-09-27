package `in`.aapatmitra.data.repo

import androidx.room.withTransaction
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.jsonOf
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.local.HealthEntryEntity
import `in`.aapatmitra.data.local.TaskEntity
import `in`.aapatmitra.data.session.Session
import `in`.aapatmitra.data.sync.Outbox
import `in`.aapatmitra.data.sync.Priority
import `in`.aapatmitra.data.sync.SyncScheduler
import `in`.aapatmitra.domain.RiskEvaluator
import kotlinx.serialization.json.jsonObject
import java.time.LocalDate
import javax.inject.Inject
import javax.inject.Singleton

data class Symptom(val code: String, val cohorts: List<String>, val labelEn: String, val labelHi: String)

/** Screenings, follow-up tasks (ASHA) — offline-first, risk evaluated on the device (TRD §12A). */
@Singleton
class CareRepo @Inject constructor(
    private val db: AppDatabase, private val outbox: Outbox, private val session: Session, private val scheduler: SyncScheduler,
) {
    fun today() = db.tasks().today(Time.today(), LocalDate.now().atStartOfDay(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli())
    fun openTasksFor(patientId: String) = db.tasks().openFor(patientId)
    fun entries(patientId: String) = db.entries().forPatient(patientId)
    fun nextOpenTask() = db.tasks().nextOpen()

    suspend fun rules(): List<RiskEvaluator.Rule> = db.reference().byPrefix("risk_rule:")
        .map { RiskEvaluator.parseRule(AppJson.parseToJsonElement(it.json).jsonObject) }

    suspend fun symptoms(): List<Symptom> = db.reference().byPrefix("symptom:").map {
        val j = AppJson.parseToJsonElement(it.json).jsonObject
        Symptom(j.str("code")!!, (j["cohorts"] as? kotlinx.serialization.json.JsonArray)?.map { c -> c.toString().trim('"') } ?: emptyList(),
            j.str("labelEn") ?: "", j.str("labelHi") ?: "")
    }

    /** Saves the entry + outbox op; returns the rules that fired on the device (provisional). */
    suspend fun saveScreening(patientId: String, cohorts: Set<String>, vitals: Map<String, Double>,
                              symptoms: Map<String, Boolean>, notes: String?): List<RiskEvaluator.Rule> {
        val fired = RiskEvaluator.fired(rules(), cohorts, vitals, symptoms)
        val id = Ids.uuid7()
        val now = System.currentTimeMillis()
        val vitalsOut = vitals.mapValues { (k, v) -> if (k in INT_VITALS) v.toInt() else v }
        val data = jsonOf("id" to id, "patientId" to patientId, "kind" to "screening", "vitals" to vitalsOut,
            "symptoms" to symptoms.map { (c, p) -> mapOf("code" to c, "present" to p) },
            "deviceRiskFlags" to fired.map { it.code }, "riskRuleSetVersion" to (fired.maxOfOrNull { it.version } ?: 1),
            "notes" to notes?.ifBlank { null }, "recordedAt" to Time.iso(now))
        db.withTransaction {
            db.entries().upsert(HealthEntryEntity(id = id, patientId = patientId, kind = "screening", highRisk = fired.isNotEmpty(),
                highRiskSource = "device", riskFlagsJson = jsonOf("codes" to fired.map { it.code }).toString(),
                vitalsJson = jsonOf(*vitalsOut.map { it.key to it.value }.toTypedArray()).toString(),
                symptomsJson = null, notes = notes, authorName = session.me()?.name, recordedAt = now, pending = true))
            outbox.add("health_record_entry", id, "create", data, Priority.CLINICAL)
        }
        scheduler.syncSoon()
        return fired
    }

    suspend fun addFollowUp(patientId: String, patientName: String?, days: Int, type: String = "high_risk_recheck") {
        val id = Ids.uuid7()
        val due = LocalDate.now().plusDays(days.toLong()).toString()
        db.withTransaction {
            db.tasks().upsert(TaskEntity(id = id, patientId = patientId, patientName = patientName, ashaId = session.me()?.id ?: "",
                taskType = type, title = null, priority = "normal", dueDate = due, status = "open", doneAt = null, version = null, dirty = true))
            outbox.add("follow_up_task", id, "create", jsonOf("id" to id, "patientId" to patientId, "taskType" to type,
                "dueDate" to due, "priority" to "normal"), Priority.ROUTINE)
        }
        scheduler.syncSoon()
    }

    /** Done is monotonic server-side (TRD §6.4). */
    suspend fun markDone(task: TaskEntity) {
        db.withTransaction {
            db.tasks().upsert(task.copy(status = "done", doneAt = System.currentTimeMillis(), dirty = true))
            outbox.add("follow_up_task", task.id, "update", jsonOf("status" to "done"), Priority.ROUTINE)
        }
        scheduler.syncSoon()
    }

    companion object {
        val INT_VITALS = setOf("bpSystolic", "bpDiastolic", "pulseBpm", "respRate", "spo2Pct", "rbsMgDl", "fetalHrBpm", "gestationWeeks")
    }
}
