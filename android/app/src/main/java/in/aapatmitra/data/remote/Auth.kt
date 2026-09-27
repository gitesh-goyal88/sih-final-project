package `in`.aapatmitra.data.remote

import `in`.aapatmitra.BuildConfig
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.jsonOf
import `in`.aapatmitra.core.obj
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.session.Session
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.jsonObject
import okhttp3.Authenticator
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.Route
import java.util.Locale
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/** Adds the bearer token, device id, app version and language to every call (API-Guide §2). */
@Singleton
class AuthInterceptor @Inject constructor(private val session: Session) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val b = chain.request().newBuilder()
            .header("X-Device-Id", session.deviceId)
            .header("X-App-Version", "${BuildConfig.VERSION_NAME}+${BuildConfig.VERSION_CODE}")  // "name+build" (API-Guide §2)
            .header("Accept-Language", Locale.getDefault().language)
        session.accessToken?.let { b.header("Authorization", "Bearer $it") }
        return chain.proceed(b.build())
    }
}

/**
 * On 401, rotate the refresh token once (reuse detection revokes the family server-side — SEC-AUTH-05).
 * Synchronized so parallel 401s trigger one refresh.
 */
@Singleton
class TokenAuthenticator @Inject constructor(private val session: Session) : Authenticator {
    private val plain = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).build()

    @Synchronized
    override fun authenticate(route: Route?, response: Response): Request? {
        if (response.request.url.encodedPath.contains("/auth/")) return null
        if (responseCount(response) >= 2) return null
        val sent = response.request.header("Authorization")?.removePrefix("Bearer ")
        if (session.accessToken != null && session.accessToken != sent) {
            return response.request.newBuilder().header("Authorization", "Bearer ${session.accessToken}").build()
        }
        val refresh = runBlocking { session.refreshToken() } ?: return null
        val body = jsonOf("refreshToken" to refresh, "deviceId" to session.deviceId).toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url(BuildConfig.API_BASE + "auth/refresh").post(body)
            .header("X-Device-Id", session.deviceId).build()
        return try {
            plain.newCall(req).execute().use { r ->
                if (!r.isSuccessful) {
                    if (r.code == 401) runBlocking { session.saveTokens(null, null) }
                    return null
                }
                val out = AppJson.parseToJsonElement(r.body!!.string()).jsonObject
                val access = out.str("accessToken") ?: return null
                runBlocking {
                    session.saveTokens(access, out.str("refreshToken"))
                    out.obj("user")?.let { session.saveMe(it) }
                }
                response.request.newBuilder().header("Authorization", "Bearer $access").build()
            }
        } catch (_: Exception) { null }
    }

    private fun responseCount(r: Response): Int {
        var n = 1
        var p = r.priorResponse
        while (p != null) { n++; p = p.priorResponse }
        return n
    }
}

/** Problem+json `code` from an error response (API-Guide §2.7). */
fun retrofit2.Response<*>.problemCode(): String? = try {
    errorBody()?.string()?.let { AppJson.parseToJsonElement(it).jsonObject.str("code") }
} catch (_: Exception) { null }
