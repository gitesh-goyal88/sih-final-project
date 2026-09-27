package `in`.aapatmitra.core

import java.security.SecureRandom
import java.util.UUID

/** UUIDv7 (RFC 9562) generated on the phone and never rewritten (TRD FR-D01, database.md B3). */
object Ids {
    private val rnd = SecureRandom()
    private const val CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    fun uuid7(): String {
        val bytes = ByteArray(16)
        rnd.nextBytes(bytes)
        val ms = System.currentTimeMillis()
        for (i in 0 until 6) bytes[i] = ((ms shr (8 * (5 - i))) and 0xff).toByte()
        bytes[6] = ((bytes[6].toInt() and 0x0f) or 0x70).toByte()
        bytes[8] = ((bytes[8].toInt() and 0x3f) or 0x80).toByte()
        val hex = bytes.joinToString("") { "%02x".format(it) }
        return "${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}"
    }

    /** First 8 Crockford base32 chars of the idempotency-key UUID bytes — the SMS `#tag` (API-Guide §2.3). */
    fun idemTag(key: String): String {
        val u = UUID.fromString(key)
        val bytes = java.nio.ByteBuffer.allocate(16).putLong(u.mostSignificantBits).putLong(u.leastSignificantBits).array()
        val sb = StringBuilder()
        var buffer = 0
        var bits = 0
        for (b in bytes) {
            buffer = (buffer shl 8) or (b.toInt() and 0xff)
            bits += 8
            while (bits >= 5) {
                sb.append(CROCKFORD[(buffer shr (bits - 5)) and 31])
                bits -= 5
                if (sb.length == 8) return sb.toString()
            }
        }
        return sb.toString()
    }
}
