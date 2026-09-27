package `in`.aapatmitra.di

import android.content.Context
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import `in`.aapatmitra.BuildConfig
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.remote.Api
import `in`.aapatmitra.data.remote.AuthInterceptor
import `in`.aapatmitra.data.remote.TokenAuthenticator
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object AppModule {
    @Provides @Singleton fun db(@ApplicationContext ctx: Context): AppDatabase = AppDatabase.open(ctx)
    @Provides fun households(db: AppDatabase) = db.households()
    @Provides fun entries(db: AppDatabase) = db.entries()
    @Provides fun tasks(db: AppDatabase) = db.tasks()
    @Provides fun cases(db: AppDatabase) = db.cases()
    @Provides fun rides(db: AppDatabase) = db.rides()
    @Provides fun reference(db: AppDatabase) = db.reference()
    @Provides fun outbox(db: AppDatabase) = db.outbox()
    @Provides fun syncState(db: AppDatabase) = db.syncState()
    @Provides fun json(): Json = AppJson

    @Provides @Singleton
    fun okhttp(auth: AuthInterceptor, authenticator: TokenAuthenticator): OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .addInterceptor(auth)
        .authenticator(authenticator)
        .build()

    @Provides @Singleton
    fun api(client: OkHttpClient): Api = Retrofit.Builder()
        .baseUrl(BuildConfig.API_BASE)
        .client(client)
        .addConverterFactory(AppJson.asConverterFactory("application/json".toMediaType()))
        .build().create(Api::class.java)
}
