package `in`.aapatmitra.data.local

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import net.zetetic.database.sqlcipher.SupportOpenHelperFactory
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

@Database(
    version = 1,
    exportSchema = true,
    entities = [
        HouseholdEntity::class, PatientEntity::class, PatientFts::class, HealthEntryEntity::class, TaskEntity::class,
        CaseEntity::class, CaseEventEntity::class, LegEntity::class, RideOfferEntity::class, FacilityEntity::class,
        IncentiveEntity::class, ReferenceKv::class, OutboxItem::class, SyncStateEntity::class, SyncConflictEntity::class,
    ],
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun households(): HouseholdDao
    abstract fun entries(): EntryDao
    abstract fun tasks(): TaskDao
    abstract fun cases(): CaseDao
    abstract fun rides(): RideDao
    abstract fun reference(): ReferenceDao
    abstract fun outbox(): OutboxDao
    abstract fun syncState(): SyncStateDao

    companion object {
        const val NAME = "aapatmitra.db"

        /** SQLCipher 4 (AES-256); random 256-bit passphrase wrapped by the Keystore key `am_db_kek` (database.md B5). */
        fun open(ctx: Context): AppDatabase {
            System.loadLibrary("sqlcipher")
            val passphrase = KeyManager.databasePassphrase(ctx)
            return Room.databaseBuilder(ctx, AppDatabase::class.java, NAME)
                .openHelperFactory(SupportOpenHelperFactory(passphrase))
                .setJournalMode(JournalMode.WRITE_AHEAD_LOGGING)
                // NO fallbackToDestructiveMigration — it would drop the outbox (database.md §14.2)
                .build()
        }
    }
}

/**
 * Keystore-held AES-GCM key wraps the SQLCipher passphrase and the refresh token (TRD §14.3, SEC-MOB-01).
 * The SOS path must work without the app PIN (SEC-MOB-02), so this key is NOT user-authentication-bound;
 * clinical screens are gated by the app PIN instead (see feature/me/AppLock.kt).
 */
object KeyManager {
    private const val KEK = "am_db_kek"
    private const val PREFS = "am_keys"

    private fun kek(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getEntry(KEK, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(KeyGenParameterSpec.Builder(KEK, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256).build())
        return gen.generateKey()
    }

    fun wrap(plain: ByteArray): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, kek())
        return Base64.encodeToString(c.iv + c.doFinal(plain), Base64.NO_WRAP)
    }

    fun unwrap(blob: String): ByteArray {
        val raw = Base64.decode(blob, Base64.NO_WRAP)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, kek(), GCMParameterSpec(128, raw, 0, 12))
        return c.doFinal(raw, 12, raw.size - 12)
    }

    fun databasePassphrase(ctx: Context): ByteArray {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        prefs.getString("db_passphrase_wrapped", null)?.let { return unwrap(it) }
        val pass = ByteArray(32).also { SecureRandom().nextBytes(it) }
        prefs.edit().putString("db_passphrase_wrapped", wrap(pass)).apply()
        return pass
    }

    /** Remote wipe / 10 wrong PINs / logout-with-remove (database.md §14.8). */
    fun wipe(ctx: Context) {
        ctx.deleteDatabase(AppDatabase.NAME)
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply()
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        listOf(KEK, "am_device_sign").forEach { if (ks.containsAlias(it)) ks.deleteEntry(it) }
    }
}

/** After a wipe the singleton DB is closed: relaunch into a fresh process (login screen, SOS still works). */
object AppRestart {
    fun restart(ctx: Context) {
        val launch = ctx.packageManager.getLaunchIntentForPackage(ctx.packageName)
            ?.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)
        if (launch != null) ctx.startActivity(launch)
        Runtime.getRuntime().exit(0)
    }
}
