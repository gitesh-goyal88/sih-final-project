package `in`.aapatmitra

import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import dagger.hilt.android.AndroidEntryPoint
import `in`.aapatmitra.nav.AppRoot
import `in`.aapatmitra.ui.theme.AapatTheme

@AndroidEntryPoint
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Clinical data must not appear in screenshots / recents (SEC-MOB-07). Debug builds allow capture for testing.
        if (!BuildConfig.DEBUG) window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
        setContent { AapatTheme { AppRoot() } }
    }
}
