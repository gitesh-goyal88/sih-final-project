package `in`.aapatmitra.feature.onboarding

import android.Manifest
import androidx.activity.compose.BackHandler
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import `in`.aapatmitra.R
import `in`.aapatmitra.data.repo.AuthResult
import `in`.aapatmitra.feature.sos.EmergencyWithoutLogin
import `in`.aapatmitra.nav.LocalVm
import `in`.aapatmitra.nav.TopBar
import `in`.aapatmitra.ui.components.PrimaryButton
import `in`.aapatmitra.ui.components.SecondaryButton
import `in`.aapatmitra.ui.components.Tile
import `in`.aapatmitra.ui.theme.SosRed
import `in`.aapatmitra.ui.theme.TextMuted
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private enum class Step { LANGUAGE, ROLE, LOGIN, OTP, CHOOSE_ROLE, EMERGENCY }

/** UI-UX §6.1: Splash → Language → Role → Phone number + OTP → Home. */
@Composable
fun OnboardingFlow() {
    val vm = LocalVm.current
    val lang by vm.language.collectAsState()
    var step by rememberSaveable { mutableStateOf(if (lang == null) Step.LANGUAGE else Step.ROLE) }
    var role by rememberSaveable { mutableStateOf("patient") }
    var challenge by rememberSaveable { mutableStateOf<String?>(null) }
    var roles by remember { mutableStateOf<List<String>>(emptyList()) }
    var otpValue by rememberSaveable { mutableStateOf("") }

    BackHandler(enabled = step != Step.LANGUAGE && step != Step.ROLE) {
        step = when (step) { Step.OTP -> Step.LOGIN; Step.CHOOSE_ROLE -> Step.LOGIN; else -> Step.ROLE }
    }
    Column(Modifier.fillMaxSize()) {
        TopBar { vm.launch { vm.session.setLanguage(if (lang == "en") "hi" else "en") } }
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            when (step) {
                Step.LANGUAGE -> LanguagePicker { vm.launch { vm.session.setLanguage(it) }; step = Step.ROLE }
                Step.ROLE -> {
                    Text(stringResource(R.string.who_are_you), style = MaterialTheme.typography.headlineSmall)
                    Tile("👪", stringResource(R.string.role_patient), Modifier.fillMaxWidth()) { role = "patient"; step = Step.LOGIN }
                    Tile("👩‍⚕️", stringResource(R.string.role_asha), Modifier.fillMaxWidth()) { role = "asha"; step = Step.LOGIN }
                    Tile("🚗", stringResource(R.string.role_volunteer), Modifier.fillMaxWidth()) { role = "volunteer"; step = Step.LOGIN }
                    Spacer(Modifier.height(8.dp))
                    // The one rule: an emergency never waits for a login
                    PrimaryButton(stringResource(R.string.emergency_no_login), color = SosRed) { step = Step.EMERGENCY }
                }
                Step.LOGIN -> Login(role, onBack = { step = Step.ROLE }) { challenge = it; otpValue = ""; step = Step.OTP }
                Step.OTP -> Otp(challenge!!, role, otpValue, { otpValue = it }, onBack = { step = Step.LOGIN }) { r -> roles = r; step = Step.CHOOSE_ROLE }
                Step.CHOOSE_ROLE -> {
                    Text(stringResource(R.string.choose_role), style = MaterialTheme.typography.headlineSmall)
                    val scope = rememberCoroutineScope()
                    roles.forEach { r ->
                        SecondaryButton(r) { scope.launch { vm.auth.verify(challenge!!, otpValue, r) } }
                    }
                }
                Step.EMERGENCY -> EmergencyWithoutLogin { step = Step.ROLE }
            }
        }
    }
}

@Composable
fun LanguagePicker(onPick: (String) -> Unit) {
    Text(stringResource(R.string.choose_language), style = MaterialTheme.typography.headlineSmall)
    // Tiles show each language in its own script (UI-UX §4.2); MVP ships Hindi + English string packs
    Tile("अ", "हिन्दी", Modifier.fillMaxWidth()) { onPick("hi") }
    Tile("A", "English", Modifier.fillMaxWidth()) { onPick("en") }
}

@Composable
private fun Login(role: String, onBack: () -> Unit, onSent: (String) -> Unit) {
    val vm = LocalVm.current
    val scope = rememberCoroutineScope()
    var input by rememberSaveable { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val staff = role == "asha"
    Text(stringResource(if (staff) R.string.enter_staff_id else R.string.enter_mobile), style = MaterialTheme.typography.headlineSmall)
    OutlinedTextField(value = input, onValueChange = { input = if (staff) it.uppercase().take(20) else it.filter(Char::isDigit).take(10) },
        prefix = if (staff) null else ({ Text("+91 ") }), singleLine = true, modifier = Modifier.fillMaxWidth(),
        textStyle = MaterialTheme.typography.titleMedium,
        keyboardOptions = KeyboardOptions(keyboardType = if (staff) KeyboardType.Text else KeyboardType.Phone))
    error?.let { Text(it, color = SosRed, style = MaterialTheme.typography.bodyLarge) }
    val errText = stringResource(R.string.otp_send_failed)
    PrimaryButton(stringResource(R.string.send_otp), enabled = !busy && (if (staff) input.length >= 4 else input.length == 10)) {
        busy = true
        scope.launch {
            vm.auth.requestOtp(if (staff) null else input, if (staff) input else null)
                .onSuccess { onSent(it.id) }.onFailure { error = "$errText (${it.message})" }
            busy = false
        }
    }
    if (role == "patient") Text(stringResource(R.string.no_phone_ask_asha), color = TextMuted, style = MaterialTheme.typography.bodyLarge)
    TextButton(onClick = onBack) { Text(stringResource(R.string.back)) }
}

@Composable
private fun Otp(challenge: String, role: String, otp: String, setOtp: (String) -> Unit, onBack: () -> Unit, onChooseRole: (List<String>) -> Unit) {
    val vm = LocalVm.current
    val scope = rememberCoroutineScope()
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var resendIn by remember { mutableIntStateOf(30) }
    LaunchedEffect(challenge) { while (resendIn > 0) { delay(1000); resendIn-- } }
    Text(stringResource(R.string.enter_otp), style = MaterialTheme.typography.headlineSmall)
    OutlinedTextField(value = otp, onValueChange = { setOtp(it.filter(Char::isDigit).take(6)) }, singleLine = true,
        modifier = Modifier.fillMaxWidth(), textStyle = MaterialTheme.typography.displaySmall.copy(textAlign = TextAlign.Center),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword))
    error?.let { Text(it, color = SosRed, style = MaterialTheme.typography.bodyLarge) }
    val wrong = stringResource(R.string.otp_wrong)
    PrimaryButton(stringResource(R.string.verify), enabled = otp.length == 6 && !busy) {
        busy = true
        scope.launch {
            when (val r = vm.auth.verify(challenge, otp, role.takeIf { it != "asha" })) {
                AuthResult.Ok -> Unit
                is AuthResult.ChooseRole -> onChooseRole(r.roles)
                is AuthResult.Error -> error = "$wrong (${r.code})"
            }
            busy = false
        }
    }
    Text(if (resendIn > 0) stringResource(R.string.resend_in, resendIn) else stringResource(R.string.resend_now),
        color = TextMuted, style = MaterialTheme.typography.bodyLarge)
    if (resendIn == 0) TextButton(onClick = onBack) { Text(stringResource(R.string.resend_now)) }
}

/**
 * FR-A04: SEND_SMS and CALL_PHONE are requested during onboarding with an explanation, one at a time —
 * not at SOS time. Location and notifications are requested the same way.
 */
@Composable
fun PermissionsIntro(onDone: () -> Unit) {
    val perms = buildList {
        add(Manifest.permission.ACCESS_FINE_LOCATION to R.string.perm_location)
        add(Manifest.permission.SEND_SMS to R.string.perm_sms)
        add(Manifest.permission.CALL_PHONE to R.string.perm_call)
        if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS to R.string.perm_notify)
    }
    var i by rememberSaveable { mutableIntStateOf(0) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { i++ }
    if (i >= perms.size) { LaunchedEffect(Unit) { onDone() }; return }
    val (perm, why) = perms[i]
    Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text(stringResource(R.string.perm_title, i + 1, perms.size), style = MaterialTheme.typography.headlineSmall)
        Text(stringResource(why), style = MaterialTheme.typography.bodyLarge)
        PrimaryButton(stringResource(R.string.allow)) { launcher.launch(perm) }
        TextButton(onClick = { i++ }) { Text(stringResource(R.string.not_now)) }
    }
}
