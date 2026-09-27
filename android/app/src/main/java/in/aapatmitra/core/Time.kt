package `in`.aapatmitra.core

import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.Period

object Time {
    /** Accepts ISO-8601 (`2026-09-26T03:21:17Z`) and the snapshot's `str(datetime)` form (`2026-09-26 03:21:17.5+00:00`). */
    fun parse(iso: String?): Long? = try {
        iso?.let { OffsetDateTime.parse(it.trim().replace(' ', 'T')).toInstant().toEpochMilli() }
    } catch (_: Exception) {
        try { iso?.let { Instant.parse(it).toEpochMilli() } } catch (_: Exception) { null }
    }
    fun iso(ms: Long): String = Instant.ofEpochMilli(ms).toString()
    fun ageYears(dob: String?): Int? = try { dob?.let { Period.between(LocalDate.parse(it), LocalDate.now()).years } } catch (_: Exception) { null }
    fun minutesAgo(ms: Long?, skew: Long = 0): Long? = ms?.let { ((System.currentTimeMillis() + skew - it) / 60_000).coerceAtLeast(0) }
    fun today(): String = LocalDate.now().toString()
}
