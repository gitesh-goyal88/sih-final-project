package `in`.aapatmitra.feature.me

import android.app.Activity
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import `in`.aapatmitra.R
import `in`.aapatmitra.data.sos.ChannelNumbers
import `in`.aapatmitra.domain.Tone
import `in`.aapatmitra.nav.LocalVm
import `in`.aapatmitra.nav.findActivity
import `in`.aapatmitra.nav.TopBar
import `in`.aapatmitra.ui.components.AppCard
import `in`.aapatmitra.ui.components.ChipRow
import `in`.aapatmitra.ui.components.ConfirmDialog
import `in`.aapatmitra.ui.components.PrimaryButton
import `in`.aapatmitra.ui.components.SecondaryButton
import `in`.aapatmitra.ui.components.SectionTitle
import `in`.aapatmitra.ui.components.SelectChip
import `in`.aapatmitra.ui.components.StatusChip
import `in`.aapatmitra.ui.theme.SosRed
import `in`.aapatmitra.ui.theme.TextMuted
import kotlinx.coroutines.launch

/** "Me" tab (UI-UX §6.5): who I am, language, text size, privacy, pending items, log out. */
@Composable
fun MeScreen(modifier: Modifier = Modifier, extra: @Composable () -> Unit = {}) {
    val vm = LocalVm.current
    val me by vm.me.collectAsState()
    val lang by vm.language.collectAsState()
    val scale by vm.textScale.collectAsState()
    val pending by vm.pending.collectAsState()
    val hasPin by vm.session.hasPinFlow.collectAsState(initial = false)
    val scope = rememberCoroutineScope()
    var confirmLogout by remember { mutableStateOf(false) }
    var settingPin by remember { mutableStateOf(false) }
    val village by produceState<String?>(null, me) { value = vm.households.villageName(me?.homeVillageId ?: me?.villageIds?.firstOrNull()) }
    Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        AppCard {
            Text(me?.name ?: "—", style = MaterialTheme.typography.headlineSmall)
            Text(me?.phoneMasked ?: "", style = MaterialTheme.typography.bodyLarge, color = TextMuted)
            Text(stringResource(roleRes(me?.role)) + (village?.let { " · $it" } ?: ""), style = MaterialTheme.typography.bodyLarge)
        }
        SectionTitle(stringResource(R.string.language))
        ChipRow {
            SelectChip("हिन्दी", lang != "en") { vm.launch { vm.session.setLanguage("hi") } }
            SelectChip("English", lang == "en") { vm.launch { vm.session.setLanguage("en") } }
        }
        SectionTitle(stringResource(R.string.text_size))
        ChipRow {
            listOf(100 to "A", 130 to "A+", 160 to "A++").forEach { (v, l) -> SelectChip(l, scale == v) { vm.launch { vm.session.setTextScale(v) } } }
        }
        extra()
        if (me?.role == "asha") {
            SectionTitle(stringResource(R.string.app_pin))
            Text(stringResource(R.string.app_pin_help), color = TextMuted, style = MaterialTheme.typography.bodyLarge)
            SecondaryButton(stringResource(if (hasPin) R.string.change_pin else R.string.set_pin)) { settingPin = true }
        }
        SectionTitle(stringResource(R.string.privacy))
        Text(stringResource(if (me?.role == "patient") R.string.privacy_patient else R.string.privacy_staff), style = MaterialTheme.typography.bodyLarge)
        SectionTitle(stringResource(R.string.sync))
        if (pending == 0) StatusChip(stringResource(R.string.all_sent), Tone.DONE)
        else StatusChip(pluralStringResource(R.plurals.banner_pending, pending, pending), Tone.OFFLINE)
        SecondaryButton(stringResource(R.string.sync_now)) { vm.syncNow() }
        SectionTitle(stringResource(R.string.about))
        Text(stringResource(R.string.about_body), style = MaterialTheme.typography.bodyLarge, color = TextMuted)
        SecondaryButton(stringResource(R.string.logout)) { confirmLogout = true }  // red is reserved for emergency (UI-UX §3.1)
    }
    if (confirmLogout) ConfirmDialog(stringResource(R.string.logout),
        if (pending > 0) stringResource(R.string.logout_pending_warning, pending) else stringResource(R.string.logout_body),
        stringResource(R.string.logout), onDismiss = { confirmLogout = false }) {
        confirmLogout = false
        scope.launch { vm.auth.logout() }
    }
    if (settingPin) PinDialog(onDismiss = { settingPin = false }) { pin -> scope.launch { vm.session.setPin(pin) }; settingPin = false }
}

fun roleRes(role: String?) = when (role) {
    "patient" -> R.string.role_patient
    "asha" -> R.string.role_asha
    "volunteer" -> R.string.role_volunteer
    else -> R.string.role_staff
}

@Composable
private fun PinDialog(onDismiss: () -> Unit, onSet: (String) -> Unit) {
    var pin by remember { mutableStateOf("") }
    androidx.compose.material3.AlertDialog(onDismissRequest = onDismiss, title = { Text(stringResource(R.string.set_pin)) },
        text = {
            OutlinedTextField(pin, { pin = it.filter(Char::isDigit).take(6) }, singleLine = true, visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword), label = { Text(stringResource(R.string.pin_digits)) })
        },
        confirmButton = { TextButton(onClick = { onSet(pin) }, enabled = pin.length >= 4) { Text(stringResource(R.string.save)) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text(stringResource(R.string.back)) } })
}

/**
 * App PIN gate for clinical screens (SEC-MOB-02). The SOS tab is never behind it.
 * 10 wrong PINs wipe the local store (database.md §14.8).
 */
@Composable
fun PinGate(unlocked: Boolean, onUnlock: () -> Unit, content: @Composable () -> Unit) {
    val vm = LocalVm.current
    val hasPin by vm.session.hasPinFlow.collectAsState(initial = false)
    if (!hasPin || unlocked) { content(); return }
    val scope = rememberCoroutineScope()
    var pin by remember { mutableStateOf("") }
    var left by remember { mutableStateOf<Int?>(null) }
    Column(Modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text(stringResource(R.string.enter_pin), style = MaterialTheme.typography.headlineSmall)
        OutlinedTextField(pin, { pin = it.filter(Char::isDigit).take(6) }, singleLine = true, modifier = Modifier.fillMaxWidth(),
            visualTransformation = PasswordVisualTransformation(), textStyle = MaterialTheme.typography.displaySmall.copy(textAlign = TextAlign.Center),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword))
        left?.let { Text(stringResource(R.string.pin_wrong, it), color = SosRed) }
        PrimaryButton(stringResource(R.string.unlock), enabled = pin.length >= 4) {
            scope.launch {
                val r = vm.session.checkPin(pin)
                pin = ""
                when {
                    r < 0 -> onUnlock()
                    r == 0 -> vm.auth.wipe()
                    else -> left = r
                }
            }
        }
    }
}

/** Staff not yet verified: "Waiting for approval" with a Call button (UI-UX §6.1). SOS still works. */
@Composable
fun PendingApproval() {
    val vm = LocalVm.current
    val activity = LocalContext.current.findActivity()
    val numbers by produceState<ChannelNumbers?>(null) { value = vm.sos.channelNumbers() }
    val scope = rememberCoroutineScope()
    Column(Modifier.fillMaxSize()) {
        TopBar { }
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text("⏳", style = MaterialTheme.typography.displaySmall)
            Text(stringResource(R.string.waiting_approval), style = MaterialTheme.typography.headlineSmall)
            Text(stringResource(R.string.waiting_approval_body), style = MaterialTheme.typography.bodyLarge)
            numbers?.let { n -> PrimaryButton(stringResource(R.string.call_helpline, n.helpline)) { activity?.let { vm.sos.call(it, n.helpline) } } }
            SecondaryButton(stringResource(R.string.check_again)) { scope.launch { vm.syncNow() } }
            TextButton(onClick = { scope.launch { vm.auth.logout() } }) { Text(stringResource(R.string.logout)) }
        }
    }
}

/** Doctor / facility / admin accounts use the Web Console (TRD §2). */
@Composable
fun WebOnlyRole() {
    val vm = LocalVm.current
    val scope = rememberCoroutineScope()
    Column(Modifier.fillMaxSize()) {
        TopBar { }
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text(stringResource(R.string.use_web_console), style = MaterialTheme.typography.headlineSmall)
            PrimaryButton(stringResource(R.string.logout)) { scope.launch { vm.auth.logout() } }
        }
    }
}

/** Items waiting to send (UI-UX §9) with Retry; SOS items first. */
@Composable
fun OutboxScreen(modifier: Modifier = Modifier, onBack: () -> Unit) {
    val vm = LocalVm.current
    val items by vm.pendingList.collectAsState()
    val activity = LocalContext.current.findActivity()
    val numbers by produceState<ChannelNumbers?>(null) { value = vm.sos.channelNumbers() }
    Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.waiting_items), style = MaterialTheme.typography.headlineSmall)
        if (items.isEmpty()) Text(stringResource(R.string.all_sent), style = MaterialTheme.typography.bodyLarge)
        items.forEach { o ->
            AppCard {
                Text(stringResource(outboxLabel(o.entity, o.commandName)), style = MaterialTheme.typography.titleMedium)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    when {
                        o.sentViaSms -> StatusChip(stringResource(R.string.sent_by_sms), Tone.OFFLINE)
                        o.state == "rejected" -> StatusChip(stringResource(R.string.could_not_send), Tone.PROBLEM)
                        o.attempts >= 3 -> StatusChip(stringResource(R.string.could_not_send), Tone.WAITING)
                        else -> StatusChip(stringResource(R.string.saved_will_send), Tone.OFFLINE)
                    }
                }
                o.lastErrorCode?.let { Text(it, color = TextMuted, style = MaterialTheme.typography.bodySmall) }
                if (o.state != "in_flight") TextButton(onClick = { vm.launch { vm.db.outbox().retry(o.opId); vm.syncNow() } }) { Text(stringResource(R.string.try_again)) }
                if (o.priority == 0 && o.attempts >= 3) numbers?.let { n ->
                    PrimaryButton(stringResource(R.string.call_helpline, n.helpline), color = SosRed) { activity?.let { vm.sos.call(it, n.helpline) } }
                }
            }
        }
        TextButton(onClick = onBack) { Text(stringResource(R.string.back)) }
    }
}

private fun outboxLabel(entity: String, name: String?) = when {
    name == "CreateSOS" -> R.string.ob_sos
    name == "CreateReferral" -> R.string.ob_referral
    entity == "household" -> R.string.ob_household
    entity == "health_record_entry" -> R.string.ob_screening
    entity == "follow_up_task" -> R.string.ob_task
    entity == "transport_leg" -> R.string.ob_leg
    else -> R.string.ob_other
}
