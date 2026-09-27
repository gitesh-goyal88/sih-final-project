package `in`.aapatmitra.core

/**
 * Hybrid logical clock `physicalMs:counter:deviceId` for field-level last-writer-wins (TRD §6.4, ADR-05).
 * Monotonic across restarts: the last stamp is persisted in DataStore (database.md §14.7 `hlc_state`).
 */
class Hlc(private val deviceId: String, private var lastMs: Long = 0, private var counter: Int = 0) {
    @Synchronized
    fun next(): String {
        val now = System.currentTimeMillis()
        if (now > lastMs) {
            lastMs = now
            counter = 0
        } else {
            counter += 1
        }
        return "%d:%04d:%s".format(lastMs, counter, deviceId.take(8))
    }

    fun state(): Pair<Long, Int> = lastMs to counter
}
