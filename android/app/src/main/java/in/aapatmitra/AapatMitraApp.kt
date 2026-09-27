package `in`.aapatmitra

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import androidx.hilt.work.HiltWorkerFactory
import androidx.work.Configuration
import dagger.hilt.android.HiltAndroidApp
import `in`.aapatmitra.data.sync.SyncScheduler
import javax.inject.Inject

@HiltAndroidApp
class AapatMitraApp : Application(), Configuration.Provider {
    @Inject lateinit var workerFactory: HiltWorkerFactory
    @Inject lateinit var scheduler: SyncScheduler

    override val workManagerConfiguration: Configuration
        get() = Configuration.Builder().setWorkerFactory(workerFactory).build()

    override fun onCreate() {
        super.onCreate()
        if (Build.VERSION.SDK_INT >= 26) {
            val nm = getSystemService(NotificationManager::class.java)
            // Ride requests ring loudly (UI-UX §6.4); sync runs quietly
            nm.createNotificationChannel(NotificationChannel(CH_EMERGENCY, getString(R.string.channel_emergency), NotificationManager.IMPORTANCE_HIGH))
            nm.createNotificationChannel(NotificationChannel(CH_SYNC, getString(R.string.channel_sync), NotificationManager.IMPORTANCE_MIN))
        }
        scheduler.schedulePeriodic()
    }

    companion object {
        const val CH_EMERGENCY = "emergency"
        const val CH_SYNC = "sync"
    }
}
