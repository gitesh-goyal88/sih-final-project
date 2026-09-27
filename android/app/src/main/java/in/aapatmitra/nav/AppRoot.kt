package `in`.aapatmitra.nav

import android.content.Context
import android.content.ContextWrapper
import android.content.res.Resources
import android.app.Activity
import android.content.res.Configuration
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Language
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.NavHostController
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import `in`.aapatmitra.R
import `in`.aapatmitra.feature.asha.AshaGraph
import `in`.aapatmitra.feature.me.PendingApproval
import `in`.aapatmitra.feature.onboarding.OnboardingFlow
import `in`.aapatmitra.feature.patient.PatientGraph
import `in`.aapatmitra.feature.volunteer.VolunteerGraph
import `in`.aapatmitra.feature.me.WebOnlyRole
import `in`.aapatmitra.ui.components.ConnectivityBanner
import `in`.aapatmitra.ui.theme.MitraBlue
import `in`.aapatmitra.ui.theme.OfflineGrey
import `in`.aapatmitra.ui.theme.SuccessGreen
import `in`.aapatmitra.ui.theme.Surface
import java.util.Locale

val LocalVm = staticCompositionLocalOf<AppVm> { error("AppVm not provided") }

/** Apply the in-app language (UI-UX §4.2) and text size A / A+ / A++ (UI-UX §6.5) without recreating the activity. */
@Composable
private fun Localized(lang: String?, scale: Int, content: @Composable () -> Unit) {
    val base = LocalContext.current
    val ctx: Context = remember(lang, base) {
        if (lang == null) base else {
            val cfg = Configuration(base.resources.configuration).apply { setLocale(Locale(lang)) }
            val res = base.createConfigurationContext(cfg).resources
            // Wrap (not replace) the Activity so result launchers and startActivity keep working
            object : ContextWrapper(base) { override fun getResources(): Resources = res }
        }
    }
    val density = LocalDensity.current
    CompositionLocalProvider(
        LocalContext provides ctx,
        LocalConfiguration provides ctx.resources.configuration,
        LocalDensity provides Density(density.density, density.fontScale * scale / 100f),
    ) { content() }
}

@Composable
fun AppRoot() {
    val vm: AppVm = hiltViewModel()
    val me by vm.me.collectAsState()
    val loaded by vm.meLoaded.collectAsState()
    val lang by vm.language.collectAsState()
    val scale by vm.textScale.collectAsState()
    CompositionLocalProvider(LocalVm provides vm) {
        Localized(lang, scale) {
            Box(Modifier.fillMaxSize().background(Surface)) {
                when {
                    !loaded -> CircularProgressIndicator(Modifier.align(Alignment.Center))
                    me == null -> OnboardingFlow()
                    me!!.status == "pending" && me!!.role != "patient" -> PendingApproval()
                    me!!.role == "patient" -> PatientGraph()
                    me!!.role == "asha" -> AshaGraph()
                    me!!.role == "volunteer" -> VolunteerGraph()
                    else -> WebOnlyRole()
                }
            }
        }
    }
    LaunchedEffect(me?.id) {
        if (me != null) vm.syncNow()
        vm.locator.warmUp()  // refresh last-known location off the SOS path, so SOS never waits for a fix
    }
}

/** Top bar: logo + name left; language + network dot right (UI-UX §5). */
@Composable
fun TopBar(title: String? = null, onLanguage: () -> Unit) {
    val vm = LocalVm.current
    val online by vm.online.collectAsState()
    val lang by vm.language.collectAsState()
    Row(Modifier.fillMaxWidth().background(MitraBlue).statusBarsPadding().padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Text("🤝", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.width(8.dp))
        Text(title ?: stringResource(R.string.app_name), color = Color.White, style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
        Row(Modifier.clip(CircleShape).clickable(onClick = onLanguage).heightIn(min = 48.dp).padding(horizontal = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Filled.Language, stringResource(R.string.language), tint = Color.White)
            Spacer(Modifier.width(4.dp))
            Text(if (lang == "en") "English" else "हिन्दी", color = Color.White)
        }
        Spacer(Modifier.width(8.dp))
        Box(Modifier.size(14.dp).clip(CircleShape).background(if (online) SuccessGreen else OfflineGrey))
    }
}

data class Tab(val route: String, val emoji: String, val label: Int)

@Composable
fun RoleShell(nav: NavHostController, tabs: List<Tab>, content: @Composable (Modifier) -> Unit) {
    val vm = LocalVm.current
    val online by vm.online.collectAsState()
    val pending by vm.pending.collectAsState()
    val lang by vm.language.collectAsState()
    val entry by nav.currentBackStackEntryAsState()
    Scaffold(
        topBar = {
            Column {
                TopBar { vm.launch { vm.session.setLanguage(if (lang == "en") "hi" else "en") } }
                ConnectivityBanner(online, pending) { nav.navigate("outbox") }
            }
        },
        bottomBar = {
            NavigationBar(containerColor = Color.White) {
                tabs.forEach { t ->
                    NavigationBarItem(selected = entry?.destination?.route == t.route,
                        onClick = { nav.navigate(t.route) { popUpTo(tabs.first().route); launchSingleTop = true } },
                        icon = { Text(t.emoji, style = MaterialTheme.typography.titleMedium) },
                        label = { Text(stringResource(t.label)) },
                        colors = NavigationBarItemDefaults.colors(indicatorColor = Color(0xFFEAF1FB)))
                }
            }
        },
        containerColor = Surface,
    ) { pad -> content(Modifier.padding(pad)) }
}

@Composable fun rememberNav() = rememberNavController()

/** The hosting Activity behind any ContextWrapper (the locale wrapper above). */
tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}
