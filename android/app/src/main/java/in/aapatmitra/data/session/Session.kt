package `in`.aapatmitra.data.session

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dagger.hilt.android.qualifiers.ApplicationContext
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Hlc
import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.local.KeyManager
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import java.security.MessageDigest
import javax.inject.Inject
import javax.inject.Singleton

private val Context.store by preferencesDataStore("am_session")

/** Signed-in user as the app needs it (UserOut, API-Guide §3). */
data class Me(val json: JsonObject) {
    val id get() = json.str("id")!!
    val role get() = json.str("role")!!
    val status get() = json.str("status") ?: "active"
    val name get() = json.str("name")
    val phoneMasked get() = json.str("phoneMasked")
    val districtCode get() = json.str("districtCode")
    val householdId get() = json.str("householdId")
    val patientId get() = json.str("patientId")
    val homeVillageId get() = json.str("homeVillageId")
    val villageIds: List<String> get() = (json["villageIds"] as? kotlinx.serialization.json.JsonArray)?.map { it.toString().trim('"') } ?: emptyList()
}

/**
 * Session state (TRD §14.3): the access token lives in memory only; the refresh token is wrapped by the
 * Keystore KEK before it touches disk (SEC-MOB-01). Device id and HLC state survive restarts.
 */
@Singleton
class Session @Inject constructor(@ApplicationContext private val ctx: Context) {
    private object K {
        val deviceId = stringPreferencesKey("device_id")
        val refresh = stringPreferencesKey("refresh_wrapped")
        val me = stringPreferencesKey("me_json")
        val language = stringPreferencesKey("language")
        val textScale = intPreferencesKey("text_scale")
        val hlcMs = longPreferencesKey("hlc_ms")
        val hlcCounter = intPreferencesKey("hlc_counter")
        val pinHash = stringPreferencesKey("pin_hash")
        val pinFails = intPreferencesKey("pin_fails")
        val onboarded = booleanPreferencesKey("perm_onboarded")
        val blockCode = stringPreferencesKey("block_code")
        val skewMs = longPreferencesKey("skew_ms")
    }

    @Volatile var accessToken: String? = null

    val deviceId: String by lazy {
        runBlocking {
            ctx.store.data.first()[K.deviceId] ?: Ids.uuid7().also { id -> ctx.store.edit { it[K.deviceId] = id } }
        }
    }

    val hlc: Hlc by lazy {
        val prefs = runBlocking { ctx.store.data.first() }
        Hlc(deviceId, prefs[K.hlcMs] ?: 0, prefs[K.hlcCounter] ?: 0)
    }

    suspend fun nextHlc(): String {
        val stamp = hlc.next()
        val (ms, c) = hlc.state()
        ctx.store.edit { it[K.hlcMs] = ms; it[K.hlcCounter] = c }
        return stamp
    }

    val meFlow: Flow<Me?> = ctx.store.data.map { p -> p[K.me]?.let { Me(AppJson.parseToJsonElement(it).jsonObject) } }
    val languageFlow: Flow<String?> = ctx.store.data.map { it[K.language] }
    val textScaleFlow: Flow<Int> = ctx.store.data.map { it[K.textScale] ?: 100 }
    val permOnboardedFlow: Flow<Boolean> = ctx.store.data.map { it[K.onboarded] ?: false }

    suspend fun me(): Me? = meFlow.first()
    suspend fun language(): String? = languageFlow.first()
    suspend fun blockCode(): String? = ctx.store.data.first()[K.blockCode]
    suspend fun skewMs(): Long = ctx.store.data.first()[K.skewMs] ?: 0

    suspend fun setLanguage(lang: String) = ctx.store.edit { it[K.language] = lang }
    suspend fun setTextScale(v: Int) = ctx.store.edit { it[K.textScale] = v }
    suspend fun setPermOnboarded() = ctx.store.edit { it[K.onboarded] = true }
    suspend fun setBlockCode(code: String) = ctx.store.edit { it[K.blockCode] = code }
    suspend fun setSkew(ms: Long) = ctx.store.edit { it[K.skewMs] = ms }
    suspend fun saveMe(user: JsonObject) = ctx.store.edit { it[K.me] = user.toString() }

    suspend fun saveTokens(access: String?, refresh: String?) {
        accessToken = access
        if (refresh != null) ctx.store.edit { it[K.refresh] = KeyManager.wrap(refresh.toByteArray()) }
    }

    suspend fun refreshToken(): String? = ctx.store.data.first()[K.refresh]?.let {
        try { String(KeyManager.unwrap(it)) } catch (_: Exception) { null }
    }

    // ---- app PIN (clinical data gate, SEC-MOB-02: never on the SOS path) ----
    private fun pinDigest(pin: String) = MessageDigest.getInstance("SHA-256")
        .digest("am-pin:$deviceId:$pin".toByteArray()).joinToString("") { "%02x".format(it) }

    val hasPinFlow: Flow<Boolean> = ctx.store.data.map { it[K.pinHash] != null }
    suspend fun setPin(pin: String) = ctx.store.edit { it[K.pinHash] = pinDigest(pin); it[K.pinFails] = 0 }

    /** Returns remaining attempts, or -1 on success. 10 wrong PINs wipe local clinical data (database.md §14.8). */
    suspend fun checkPin(pin: String): Int {
        val p = ctx.store.data.first()
        if (p[K.pinHash] == pinDigest(pin)) { ctx.store.edit { it[K.pinFails] = 0 }; return -1 }
        val fails = (p[K.pinFails] ?: 0) + 1
        ctx.store.edit { it[K.pinFails] = fails }
        return (10 - fails).coerceAtLeast(0)
    }

    suspend fun clear() {
        accessToken = null
        ctx.store.edit {
            it.remove(K.refresh); it.remove(K.me); it.remove(K.pinHash); it.remove(K.pinFails); it.remove(K.blockCode)
        }
    }
}
