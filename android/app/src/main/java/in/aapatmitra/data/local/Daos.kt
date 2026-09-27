package `in`.aapatmitra.data.local

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Upsert
import kotlinx.coroutines.flow.Flow

@Dao
interface HouseholdDao {
    @Upsert suspend fun upsert(h: HouseholdEntity)
    @Upsert suspend fun upsertPatient(p: PatientEntity)
    @Query("SELECT * FROM households WHERE deleted = 0 ORDER BY houseNumber") fun all(): Flow<List<HouseholdEntity>>
    @Query("SELECT * FROM households WHERE id = :id") suspend fun get(id: String): HouseholdEntity?
    @Query("SELECT * FROM patients WHERE householdId = :hh AND deleted = 0 ORDER BY recordedAt") fun members(hh: String): Flow<List<PatientEntity>>
    @Query("SELECT * FROM patients WHERE deleted = 0 ORDER BY name") fun allPatients(): Flow<List<PatientEntity>>
    @Query("SELECT * FROM patients WHERE id = :id") suspend fun patient(id: String): PatientEntity?
    @Query("SELECT * FROM patients WHERE id = :id") fun patientFlow(id: String): Flow<PatientEntity?>
    @Query("SELECT * FROM patients WHERE dirty = 0 AND id = :id") suspend fun cleanPatient(id: String): PatientEntity?
    @Query("""SELECT p.* FROM patients p JOIN patients_fts f ON p.rowid = f.rowid
              WHERE patients_fts MATCH :q AND p.deleted = 0 LIMIT 50""")
    fun search(q: String): Flow<List<PatientEntity>>
    @Query("UPDATE patients SET shortCode = :code WHERE id = :id") suspend fun setShortCode(id: String, code: String)
    @Query("UPDATE households SET deleted = 1 WHERE id = :id") suspend fun tombstone(id: String)
    @Query("UPDATE patients SET deleted = 1 WHERE id = :id") suspend fun tombstonePatient(id: String)
}

@Dao
interface EntryDao {
    @Upsert suspend fun upsert(e: HealthEntryEntity)
    @Query("SELECT * FROM health_entries WHERE patientId = :p ORDER BY recordedAt DESC LIMIT 1") suspend fun latest(p: String): HealthEntryEntity?
    @Query("SELECT * FROM health_entries WHERE patientId = :p ORDER BY recordedAt DESC") fun forPatient(p: String): Flow<List<HealthEntryEntity>>
}

@Dao
interface TaskDao {
    @Upsert suspend fun upsert(t: TaskEntity)
    @Query("SELECT * FROM follow_up_tasks WHERE id = :id") suspend fun get(id: String): TaskEntity?
    /** ASHA "Today" (database.md §14.6): urgent first, then due, then done today. */
    @Query("""SELECT * FROM follow_up_tasks WHERE (status = 'open' AND dueDate <= :today)
              OR (status = 'done' AND doneAt >= :startOfDay)
              ORDER BY CASE WHEN status='done' THEN 2 WHEN priority='urgent' THEN 0 ELSE 1 END, dueDate""")
    fun today(today: String, startOfDay: Long): Flow<List<TaskEntity>>
    @Query("SELECT * FROM follow_up_tasks WHERE patientId = :p AND status = 'open' ORDER BY dueDate") fun openFor(p: String): Flow<List<TaskEntity>>
    @Query("SELECT * FROM follow_up_tasks WHERE status = 'open' ORDER BY dueDate LIMIT 1") fun nextOpen(): Flow<TaskEntity?>
    @Query("DELETE FROM follow_up_tasks WHERE id = :id") suspend fun delete(id: String)
}

@Dao
interface CaseDao {
    @Upsert suspend fun upsert(c: CaseEntity)
    @Upsert suspend fun upsertEvent(e: CaseEventEntity)
    @Upsert suspend fun upsertLeg(l: LegEntity)
    @Query("SELECT * FROM cases WHERE id = :id") suspend fun get(id: String): CaseEntity?
    @Query("SELECT * FROM cases WHERE id = :id") fun flow(id: String): Flow<CaseEntity?>
    @Query("SELECT * FROM cases WHERE status NOT IN ('closed','follow_up','cancelled') ORDER BY recordedAt DESC") fun open(): Flow<List<CaseEntity>>
    @Query("SELECT * FROM case_events WHERE caseId = :id ORDER BY occurredAt") fun events(id: String): Flow<List<CaseEventEntity>>
    @Query("SELECT * FROM transport_legs WHERE caseId = :id ORDER BY legOrder") fun legs(id: String): Flow<List<LegEntity>>
    @Query("SELECT * FROM transport_legs WHERE custodianUserId = :me AND status IN ('accepted','picked_up')") fun myActiveLegs(me: String): Flow<List<LegEntity>>
    @Query("SELECT * FROM transport_legs WHERE id = :id") suspend fun leg(id: String): LegEntity?
    @Query("DELETE FROM cases WHERE id = :id") suspend fun delete(id: String)
    @Query("SELECT * FROM cases WHERE idempotencyKey = :key") suspend fun byKey(key: String): CaseEntity?
    @Query("SELECT * FROM cases WHERE patientId = :p ORDER BY recordedAt DESC") fun forPatient(p: String): Flow<List<CaseEntity>>
    @Query("DELETE FROM transport_legs WHERE id = :id") suspend fun deleteLeg(id: String)
}

@Dao
interface RideDao {
    @Upsert suspend fun upsert(o: RideOfferEntity)
    @Query("SELECT * FROM volunteer_offers WHERE status = 'pending' AND expiresAt > :now ORDER BY expiresAt") fun pending(now: Long): Flow<List<RideOfferEntity>>
    @Query("UPDATE volunteer_offers SET status = :status WHERE id = :id") suspend fun setStatus(id: String, status: String)
    @Query("SELECT * FROM volunteer_offers WHERE id = :id") suspend fun offer(id: String): RideOfferEntity?
    @Upsert suspend fun upsertIncentive(i: IncentiveEntity)
    @Query("SELECT * FROM incentives ORDER BY createdAt DESC") fun incentives(): Flow<List<IncentiveEntity>>
    @Query("DELETE FROM incentives") suspend fun clearIncentives()
}

@Dao
interface ReferenceDao {
    @Upsert suspend fun put(kv: ReferenceKv)
    @Query("SELECT * FROM reference_kv WHERE `key` = :k") suspend fun get(k: String): ReferenceKv?
    @Query("SELECT * FROM reference_kv WHERE `key` = :k") fun flow(k: String): Flow<ReferenceKv?>
    @Query("SELECT * FROM reference_kv WHERE `key` LIKE :prefix || '%'") suspend fun byPrefix(prefix: String): List<ReferenceKv>
    @Query("SELECT * FROM reference_kv WHERE `key` LIKE :prefix || '%'") fun byPrefixFlow(prefix: String): Flow<List<ReferenceKv>>
    @Query("DELETE FROM reference_kv WHERE `key` = :k") suspend fun delete(k: String)
    @Query("SELECT * FROM facilities WHERE id = :id") suspend fun facility(id: String): FacilityEntity?
    @Upsert suspend fun upsertFacility(f: FacilityEntity)
    @Query("SELECT * FROM facilities WHERE status = 'open' ORDER BY name") suspend fun openFacilities(): List<FacilityEntity>
    @Query("SELECT * FROM facilities ORDER BY name") fun facilities(): Flow<List<FacilityEntity>>
}

@Dao
interface OutboxDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE) suspend fun insert(item: OutboxItem)
    /** Drain order (database.md §14.5): P0 SOS always first; dependencies must be acked first. */
    @Query("""SELECT * FROM outbox WHERE state IN ('pending','failed')
              AND (dependsOnOpId IS NULL OR dependsOnOpId IN (SELECT opId FROM outbox WHERE state = 'acked'))
              ORDER BY priority, createdAt LIMIT 200""")
    suspend fun drainable(): List<OutboxItem>
    @Query("SELECT * FROM outbox WHERE opId = :id") suspend fun get(id: String): OutboxItem?
    @Query("SELECT count(*) FROM outbox WHERE state IN ('pending','in_flight','failed')") fun pendingCount(): Flow<Int>
    @Query("SELECT * FROM outbox WHERE state IN ('pending','in_flight','failed','rejected') ORDER BY priority, createdAt") fun pendingList(): Flow<List<OutboxItem>>
    @Query("UPDATE outbox SET state = 'acked', ackedAt = :now WHERE opId IN (:ids)") suspend fun ack(ids: List<String>, now: Long)
    @Query("UPDATE outbox SET state = :state, attempts = attempts + 1, lastError = :err, lastErrorCode = :code WHERE opId = :id")
    suspend fun fail(id: String, state: String, err: String?, code: String?)
    @Query("UPDATE outbox SET sentViaSms = 1 WHERE opId = :id") suspend fun markSms(id: String)
    @Query("UPDATE outbox SET state = 'pending' WHERE opId = :id") suspend fun retry(id: String)
    @Query("DELETE FROM outbox WHERE state = 'acked' AND ackedAt < :before") suspend fun purgeAcked(before: Long)
    @Query("SELECT count(*) FROM outbox WHERE state IN ('pending','failed') AND priority = 0") suspend fun pendingEmergency(): Int
}

@Dao
interface SyncStateDao {
    @Upsert suspend fun put(s: SyncStateEntity)
    @Query("SELECT * FROM sync_state WHERE `key` = 'main'") suspend fun get(): SyncStateEntity?
    @Query("SELECT * FROM sync_state WHERE `key` = 'main'") fun flow(): Flow<SyncStateEntity?>
    @Upsert suspend fun conflict(c: SyncConflictEntity)
}
