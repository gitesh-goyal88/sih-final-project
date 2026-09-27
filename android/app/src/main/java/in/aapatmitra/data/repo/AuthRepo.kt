package `in`.aapatmitra.data.repo

import android.os.Build
import `in`.aapatmitra.BuildConfig
import `in`.aapatmitra.core.arr
import `in`.aapatmitra.core.int
import `in`.aapatmitra.core.jsonOf
import `in`.aapatmitra.core.obj
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.local.KeyManager
import `in`.aapatmitra.data.remote.Api
import `in`.aapatmitra.data.remote.problemCode
import `in`.aapatmitra.data.session.DeviceKeys
import `in`.aapatmitra.data.session.Session
import `in`.aapatmitra.data.sync.SyncScheduler
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import javax.inject.Inject
import javax.inject.Singleton

sealed interface AuthResult {
    data object Ok : AuthResult
    data class ChooseRole(val roles: List<String>) : AuthResult
    data class Error(val code: String) : AuthResult
}

data class Challenge(val id: String, val resendAfterS: Int)

/** OTP login (API-Guide §3.1). The device's signing key is registered at verify (SECURITY §8.1). */
@Singleton
class AuthRepo @Inject constructor(
    private val api: Api, private val session: Session, private val scheduler: SyncScheduler, private val db: AppDatabase,
    @dagger.hilt.android.qualifiers.ApplicationContext private val ctx: android.content.Context,
) {
    suspend fun requestOtp(phone: String?, staffId: String?): Result<Challenge> = runCatching {
        val body = jsonOf("phone" to phone?.let { normalise(it) }, "staffId" to staffId, "purpose" to "login")
        val r = api.otpRequest(body)
        val out = r.body() ?: error(r.problemCode() ?: "HTTP_${r.code()}")
        Challenge(out.str("challengeId")!!, out.int("resendAfterS") ?: 30)
    }

    suspend fun verify(challengeId: String, otp: String, role: String?): AuthResult = try {
        val device = jsonOf("id" to session.deviceId, "platform" to "android", "appVersion" to "${BuildConfig.VERSION_NAME}+${BuildConfig.VERSION_CODE}",
            "osVersion" to "Android ${Build.VERSION.RELEASE}", "model" to Build.MODEL,
            "publicKeyEd25519" to DeviceKeys.publicSpkiB64())
        val r = api.otpVerify(jsonOf("challengeId" to challengeId, "otp" to otp, "device" to device, "role" to role))
        val out = r.body()
        when {
            out == null -> AuthResult.Error(r.problemCode() ?: "HTTP_${r.code()}")
            out.arr("chooseRole") != null && out.str("accessToken") == null ->
                AuthResult.ChooseRole(out.arr("chooseRole")!!.map { it.toString().trim('"') })
            else -> {
                session.saveTokens(out.str("accessToken"), out.str("refreshToken"))
                out.obj("user")?.let { session.saveMe(it) }
                scheduler.syncEmergency()
                AuthResult.Ok
            }
        }
    } catch (e: Exception) {
        AuthResult.Error("NETWORK")
    }

    suspend fun logout() {
        runCatching { api.logout(jsonOf("allDevices" to false)) }
        session.clear()
        withContext(Dispatchers.IO) { db.clearAllTables() }  // blocking Room call — never on the main thread
    }

    /** "Remove this phone's data" — wipes the encrypted DB and its key (database.md §14.8). */
    suspend fun wipe() {
        session.clear()
        withContext(Dispatchers.IO) { db.close(); KeyManager.wipe(ctx) }
        `in`.aapatmitra.data.local.AppRestart.restart(ctx)
    }

    companion object {
        fun normalise(raw: String): String {
            val d = raw.filter { it.isDigit() }
            return when {
                d.length == 10 -> "+91$d"
                d.length == 12 && d.startsWith("91") -> "+$d"
                else -> "+$d"
            }
        }
    }
}
