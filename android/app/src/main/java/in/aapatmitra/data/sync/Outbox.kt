package `in`.aapatmitra.data.sync

import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.data.local.OutboxDao
import `in`.aapatmitra.data.local.OutboxItem
import `in`.aapatmitra.data.session.Session
import kotlinx.serialization.json.JsonObject
import javax.inject.Inject
import javax.inject.Singleton

/** Priority classes (API-Guide §8.2): P0 SOS always drains first. */
object Priority { const val EMERGENCY = 0; const val CASE_EVENT = 1; const val CLINICAL = 2; const val ROUTINE = 3 }

/**
 * FR-A02: callers invoke [add] inside the same `db.withTransaction { }` as their domain write, so the
 * outbox row and the Room change commit or roll back together.
 */
@Singleton
class Outbox @Inject constructor(private val dao: OutboxDao, private val session: Session) {
    suspend fun add(
        entity: String, entityId: String, op: String, body: JsonObject, priority: Int,
        name: String? = null, opId: String = Ids.uuid7(), dependsOn: String? = null,
    ): String {
        dao.insert(OutboxItem(opId = opId, entity = entity, entityId = entityId, op = op, commandName = name,
            payloadJson = body.toString(), priority = priority, createdAt = System.currentTimeMillis(),
            hlc = session.nextHlc(), dependsOnOpId = dependsOn))
        return opId
    }
}
