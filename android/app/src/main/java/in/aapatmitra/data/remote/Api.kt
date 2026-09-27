package `in`.aapatmitra.data.remote

import kotlinx.serialization.json.JsonObject
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query
import retrofit2.http.Streaming

/** The endpoints the Android app calls (API-Guide §3, §6, §8). Bodies are JSON trees — unknown fields ignored. */
interface Api {
    @POST("auth/otp/request") suspend fun otpRequest(@Body body: JsonObject): Response<JsonObject>
    @POST("auth/otp/verify") suspend fun otpVerify(@Body body: JsonObject): Response<JsonObject>
    @POST("auth/logout") suspend fun logout(@Body body: JsonObject): Response<Unit>
    @GET("me") suspend fun me(): Response<JsonObject>

    @GET("villages/{id}") suspend fun village(@Path("id") id: String): Response<JsonObject>
    @GET("reference/districts/{code}/channel-numbers") suspend fun channelNumbers(@Path("code") code: String): Response<JsonObject>

    @POST("sos") suspend fun sos(@Header("Idempotency-Key") key: String, @Body body: JsonObject): Response<JsonObject>
    @GET("cases/{id}") suspend fun case(@Path("id") id: String): Response<JsonObject>
    @GET("me/cases") suspend fun myCases(): Response<JsonObject>
    @POST("cases/{id}/commands") suspend fun caseCommand(@Path("id") id: String, @Header("Idempotency-Key") key: String, @Body body: JsonObject): Response<JsonObject>
    @GET("facilities/match") suspend fun match(@Query("patientId") patientId: String, @Query("needs") needs: String): Response<JsonObject>
    @POST("cases") suspend fun createReferral(@Header("Idempotency-Key") key: String, @Body body: JsonObject): Response<JsonObject>

    @POST("volunteers/me/availability") suspend fun availability(@Header("Idempotency-Key") key: String, @Body body: JsonObject): Response<JsonObject>
    @POST("volunteers/me/location") suspend fun location(@Header("Idempotency-Key") key: String, @Body body: JsonObject): Response<Unit>
    @GET("volunteers/me") suspend fun volunteerMe(): Response<JsonObject>
    @GET("volunteers/me/offers") suspend fun offers(): Response<JsonObject>
    @GET("volunteers/me/legs") suspend fun activeLegs(@Query("active") active: Boolean = true): Response<JsonObject>
    @POST("cases/{c}/offers/{o}/opened") suspend fun offerOpened(@Path("c") caseId: String, @Path("o") offerId: String, @Header("Idempotency-Key") key: String, @Body body: JsonObject = JsonObject(emptyMap())): Response<Unit>
    @POST("cases/{c}/legs/{l}/accept") suspend fun accept(@Path("c") caseId: String, @Path("l") legId: String, @Header("Idempotency-Key") key: String, @Body body: JsonObject): Response<JsonObject>
    @POST("cases/{c}/legs/{l}/decline") suspend fun decline(@Path("c") caseId: String, @Path("l") legId: String, @Header("Idempotency-Key") key: String, @Body body: JsonObject): Response<Unit>
    @POST("cases/{c}/legs/{l}/handover") suspend fun handover(@Path("c") caseId: String, @Path("l") legId: String, @Header("Idempotency-Key") key: String, @Body body: JsonObject): Response<JsonObject>
    @GET("cases/{c}/legs/{l}/handover-qr-payload") suspend fun handoverQrPayload(@Path("c") caseId: String, @Path("l") legId: String): Response<JsonObject>
    @GET("incentives/me") suspend fun incentives(): Response<JsonObject>
    @GET("leaderboards") suspend fun leaderboard(@Query("scope") scope: String = "village"): Response<JsonObject>

    @POST("sync") suspend fun sync(@Header("Idempotency-Key") key: String, @Header("Content-Encoding") encoding: String, @Body body: RequestBody): Response<JsonObject>
    @Streaming @GET("sync/snapshot") suspend fun snapshot(@Query("scopes") scopes: String, @Query("resumeAfter") resumeAfter: String? = null): Response<ResponseBody>
}
