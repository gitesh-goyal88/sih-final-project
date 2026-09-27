package `in`.aapatmitra.data.sync

import android.content.Context
import androidx.room.withTransaction
import dagger.hilt.android.qualifiers.ApplicationContext
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.arr
import `in`.aapatmitra.core.bool
import `in`.aapatmitra.core.jsonOf
import `in`.aapatmitra.core.obj
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.local.KeyManager
import `in`.aapatmitra.data.local.OutboxItem
import `in`.aapatmitra.data.local.SyncConflictEntity
import `in`.aapatmitra.data.local.SyncStateEntity
import `in`.aapatmitra.data.remote.Api
import `in`.aapatmitra.data.session.Session
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.ByteArrayOutputStream
import java.util.zip.GZIPOutputStream
import javax.inject.Inject
import javax.inject.Singleton

sealed interface SyncResult {
    data object Ok : SyncResult
    data object Offline : SyncResult
    data object SignedOut : SyncResult
    data class Retry(val reason: String) : SyncResult
}

/**
 * One `POST /sync` round = push outbox + pull changes (API-Guide §8). First run and `410 CURSOR_EXPIRED`
 * bootstrap from `GET /sync/snapshot` (gzipped NDJSON, database.md §9.3).
 */
@Singleton
class SyncEngine @Inject constructor(
    @ApplicationContext private val ctx: Context,
    private val api: Api,
    private val db: AppDatabase,
    private val session: Session,
    private val applier: ChangeApplier,
) {
    private val lock = Mutex()

    suspend fun run(): SyncResult = lock.withLock {
        val first = runLocked()
        if (first is SyncResult.Retry && first.reason == "cursor_expired") runLocked() else first
    }

    private suspend fun runLocked(): SyncResult {
        val me = session.me() ?: return SyncResult.SignedOut
        return try {
            val state = db.syncState().get() ?: SyncStateEntity(cursor = null, lastPullAt = null, lastPushAt = null)
            var cursor = state.cursor
            if (cursor == null || state.snapshotRequired) cursor = snapshot(scopes(me)) ?: return SyncResult.Retry("snapshot")
            var rounds = 0
            while (rounds++ < 20) {
                val ops = db.outbox().drainable()
                val body = jsonOf("cursor" to cursor, "ops" to ops.map { toOp(it) }, "scopes" to scopes(me))
                val resp = api.sync(Ids.uuid7(), "gzip", gzip(body.toString()).toRequestBody("application/json".toMediaType()))
                if (resp.code() == 410) {
                    db.syncState().put(state.copy(snapshotRequired = true, cursor = null))
                    return SyncResult.Retry("cursor_expired")
                }
                if (resp.code() == 401) return SyncResult.SignedOut
                val out = resp.body() ?: return SyncResult.Retry("http ${resp.code()}")
                handlePush(ops, out)
                applier.apply(out.arr("changes")?.map { it.jsonObject } ?: emptyList())
                cursor = out.str("cursor") ?: cursor
                val serverNow = Time.parse(out.str("serverTime"))
                if (serverNow != null) session.setSkew(serverNow - System.currentTimeMillis())
                db.syncState().put(SyncStateEntity(cursor = cursor, lastPullAt = System.currentTimeMillis(),
                    lastPushAt = if (ops.isNotEmpty()) System.currentTimeMillis() else state.lastPushAt,
                    clockSkewMs = session.skewMs(), snapshotRequired = false))
                out.obj("wipe")?.let { wipe(it) ; return SyncResult.SignedOut }
                val more = out.bool("hasMore") == true
                if (!more && db.outbox().drainable().isEmpty()) break
            }
            db.outbox().purgeAcked(System.currentTimeMillis() - 7L * 24 * 3600 * 1000)
            SyncResult.Ok
        } catch (e: java.io.IOException) {
            SyncResult.Offline
        }
    }

    /** Scopes the device may hold (API-Guide §8.4). */
    private suspend fun scopes(me: `in`.aapatmitra.data.session.Me): List<String> {
        val s = mutableListOf("global", "user:${me.id}")
        if (me.role == "asha") {
            s += me.villageIds.map { "village:$it" }
            me.districtCode?.let { s += "district:$it" }
        }
        if (me.role == "patient") me.householdId?.let { s += "household:$it" }
        if (me.role in setOf("asha", "volunteer")) {
            var block = session.blockCode()
            if (block == null) {
                val vid = me.homeVillageId ?: me.villageIds.firstOrNull()
                if (vid != null) block = api.village(vid).body()?.str("blockCode")?.also { session.setBlockCode(it) }
            }
            block?.let { s += "block:$it" }
        }
        return s
    }

    private suspend fun snapshot(scopes: List<String>): String? {
        val resp = api.snapshot(scopes.joinToString(","))
        if (!resp.isSuccessful) return null
        var cursor: String? = null
        val batch = mutableListOf<JsonObject>()
        // OkHttp decompresses Content-Encoding: gzip transparently
        resp.body()!!.byteStream().bufferedReader().useLines { lines ->
            for (line in lines) {
                if (line.isBlank()) continue
                val o = applier.parse(line)
                when (o.str("type")) {
                    "meta" -> cursor = o.str("cursor")
                    "row" -> {
                        batch += jsonOf("entity" to o.str("entity"), "id" to o.str("id"), "op" to "upsert", "data" to o.obj("data"))
                        if (batch.size >= 200) { applier.apply(batch.toList()); batch.clear() }
                    }
                }
            }
        }
        if (batch.isNotEmpty()) applier.apply(batch)
        if (cursor != null) db.syncState().put(SyncStateEntity(cursor = cursor, lastPullAt = System.currentTimeMillis(),
            lastPushAt = null, snapshotRequired = false))
        return cursor
    }

    private fun toOp(o: OutboxItem): JsonObject {
        val body = AppJson.parseToJsonElement(o.payloadJson).jsonObject
        val key = when (o.op) { "create" -> "data"; "update" -> "fields"; else -> "payload" }
        return jsonOf("opId" to o.opId, "priority" to o.priority, "entity" to o.entity, "op" to o.op, "name" to o.commandName,
            "id" to o.entityId, key to body, "hlc" to o.hlc)
    }

    private suspend fun handlePush(ops: List<OutboxItem>, out: JsonObject) {
        val applied = out.arr("applied")?.map { it.toString().trim('"') } ?: emptyList()
        db.withTransaction {
            if (applied.isNotEmpty()) db.outbox().ack(applied, System.currentTimeMillis())
            val results = out.obj("results")
            for (op in ops.filter { it.opId in applied }) {
                val r = results?.obj(op.opId)
                if (op.entity == "case" && op.commandName in setOf("CreateSOS", "CreateReferral") && r != null) {
                    val serverId = r.str("caseId") ?: op.entityId
                    if (serverId != op.entityId) applier.rekeyCase(op.entityId, serverId, r.str("shortCode"), r.str("status"))
                    else db.cases().get(op.entityId)?.let { c ->
                        db.cases().upsert(c.copy(shortCode = r.str("shortCode") ?: c.shortCode, status = r.str("status") ?: c.status,
                            serverConfirmed = true, statusIsOptimistic = false))
                    }
                }
                clearDirty(op)
            }
            out.obj("assigned")?.obj("patientShortCodes")?.forEach { (pid, code) ->
                db.households().setShortCode(pid, code.toString().trim('"'))
            }
            for (rj in out.arr("rejected") ?: emptyList()) {
                val r = rj.jsonObject
                val id = r.str("opId") ?: continue
                val retry = r.bool("retry") == true
                db.outbox().fail(id, if (retry) "failed" else "rejected", r.str("detail"), r.str("code"))
            }
            for (cj in out.arr("conflicts") ?: emptyList()) {
                val c = cj.jsonObject
                db.syncState().conflict(SyncConflictEntity(opId = c.str("opId") ?: continue, entity = c.str("entity") ?: "",
                    entityId = c.str("id") ?: "", fieldsJson = c.toString(), code = "server_won", createdAt = System.currentTimeMillis()))
            }
        }
    }

    private suspend fun clearDirty(op: OutboxItem) {
        when (op.entity) {
            "household" -> db.households().get(op.entityId)?.let { db.households().upsert(it.copy(dirty = false)) }
            "patient" -> db.households().patient(op.entityId)?.let { db.households().upsertPatient(it.copy(dirty = false)) }
            "follow_up_task" -> db.tasks().get(op.entityId)?.let { db.tasks().upsert(it.copy(dirty = false)) }
            "health_record_entry" -> Unit
        }
    }

    /** `wipe` instruction (API-Guide §8.3): clinical store only; the SOS path keeps working signed-out. */
    private suspend fun wipe(w: JsonObject) {
        session.clear()
        kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) { db.close(); KeyManager.wipe(ctx) }
        `in`.aapatmitra.data.local.AppRestart.restart(ctx)
    }

    private fun gzip(s: String): ByteArray {
        val bos = ByteArrayOutputStream()
        GZIPOutputStream(bos).use { it.write(s.toByteArray()) }
        return bos.toByteArray()
    }
}
