package `in`.aapatmitra.data.sync

import android.content.Context
import androidx.hilt.work.HiltWorker
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.core.app.NotificationCompat
import `in`.aapatmitra.AapatMitraApp
import `in`.aapatmitra.R
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import dagger.assisted.Assisted
import dagger.assisted.AssistedInject
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

@HiltWorker
class SyncWorker @AssistedInject constructor(
    @Assisted ctx: Context,
    @Assisted params: WorkerParameters,
    private val engine: SyncEngine,
) : CoroutineWorker(ctx, params) {
    /** Expedited work runs as a short foreground service below API 31. */
    override suspend fun getForegroundInfo(): ForegroundInfo {
        val n = NotificationCompat.Builder(applicationContext, AapatMitraApp.CH_SYNC)
            .setSmallIcon(R.drawable.ic_launcher).setContentTitle(applicationContext.getString(R.string.sync_sending))
            .setPriority(NotificationCompat.PRIORITY_MIN).build()
        return ForegroundInfo(4201, n)
    }

    override suspend fun doWork(): Result = when (engine.run()) {
        SyncResult.Ok, SyncResult.SignedOut -> Result.success()
        SyncResult.Offline, is SyncResult.Retry -> Result.retry()
    }
}

/** TRD §6.5 retry policy: P0 expedited with short backoff; everything else exponential, network-constrained. */
@Singleton
class SyncScheduler @Inject constructor(@ApplicationContext private val ctx: Context) {
    private val net = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    fun schedulePeriodic() {
        WorkManager.getInstance(ctx).enqueueUniquePeriodicWork("sync-periodic", ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES).setConstraints(net).build())
    }

    /** Emergency: expedited, retried every 10 s (WorkManager's minimum linear backoff). */
    fun syncEmergency() {
        WorkManager.getInstance(ctx).enqueueUniqueWork("sync-p0", ExistingWorkPolicy.REPLACE,
            OneTimeWorkRequestBuilder<SyncWorker>().setConstraints(net)
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .setBackoffCriteria(BackoffPolicy.LINEAR, 10, TimeUnit.SECONDS).build())
    }

    fun syncSoon() {
        WorkManager.getInstance(ctx).enqueueUniqueWork("sync-now", ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<SyncWorker>().setConstraints(net)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
    }
}
