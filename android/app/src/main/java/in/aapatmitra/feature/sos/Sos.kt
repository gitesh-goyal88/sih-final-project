package `in`.aapatmitra.feature.sos

import android.app.Activity
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import `in`.aapatmitra.R
import `in`.aapatmitra.data.local.PatientEntity
import `in`.aapatmitra.data.sos.ChannelNumbers
import `in`.aapatmitra.data.sos.SosOutcome
import `in`.aapatmitra.domain.Tone
import `in`.aapatmitra.domain.Vocab
import `in`.aapatmitra.nav.LocalVm
import `in`.aapatmitra.nav.findActivity
import `in`.aapatmitra.ui.components.AppCard
import `in`.aapatmitra.ui.components.CaseStepper
import `in`.aapatmitra.ui.components.Choice
import `in`.aapatmitra.ui.components.ConfirmDialog
import `in`.aapatmitra.ui.components.LaunchedPoll
import `in`.aapatmitra.ui.components.PersonCallCard
import `in`.aapatmitra.ui.components.PictureChoiceGrid
import `in`.aapatmitra.ui.components.PrimaryButton
import `in`.aapatmitra.ui.components.SecondaryButton
import `in`.aapatmitra.ui.components.SosButton
import `in`.aapatmitra.ui.components.StatusChip
import `in`.aapatmitra.ui.components.UpdatedAgo
import `in`.aapatmitra.ui.theme.OfflineGrey
import `in`.aapatmitra.ui.theme.SosRed
import `in`.aapatmitra.ui.theme.SuccessGreen
import `in`.aapatmitra.ui.theme.TextMuted
import kotlinx.coroutines.launch

@Composable
fun categoryChoices(): List<Choice> = listOf(
    Choice("pregnancy", "🤰", stringResource(R.string.cat_pregnancy)), Choice("newborn", "👶", stringResource(R.string.cat_newborn)),
    Choice("injury", "🩹", stringResource(R.string.cat_injury)), Choice("breathing", "🫁", stringResource(R.string.cat_breathing)),
    Choice("unconscious", "😵", stringResource(R.string.cat_unconscious)), Choice("other", "➕", stringResource(R.string.cat_other)),
)

/**
 * UI-UX §6.2 SOS flow: hold (done by the caller) → Who needs help? → What happened? → sent.
 * Both choices have "Skip – send now"; help never waits for a form.
 */
@Composable
fun SosFlow(members: List<PatientEntity>, preselected: String?, householdId: String?, onSent: (String) -> Unit, onCancel: () -> Unit) {
    val vm = LocalVm.current
    val activity = LocalContext.current.findActivity()
    var who by rememberSaveable { mutableStateOf(preselected) }
    // null until the family list has loaded from Room; then ask only when there is a real choice
    var whoPicked by rememberSaveable { mutableStateOf(false) }
    val askWho = !whoPicked && members.size > 1
    var requested by rememberSaveable { mutableStateOf(false) }
    val busy by vm.sosBusy.collectAsState()
    val outcome by vm.sosOutcome.collectAsState()
    LaunchedEffect(outcome, requested) { if (requested) outcome?.let { onSent(it.caseId) } }

    fun send(category: String) {
        if (requested) return
        requested = true
        vm.sosOutcome.value = null
        val p = members.firstOrNull { it.id == who }
        vm.raiseSos(activity, category, p?.id, householdId ?: p?.householdId, p?.shortCode)
    }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        if (busy) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator()
                Spacer(Modifier.padding(8.dp))
                Text(stringResource(R.string.sos_sending), style = MaterialTheme.typography.titleMedium)
            }
            return@Column
        }
        if (askWho) {
            Text(stringResource(R.string.sos_who), style = MaterialTheme.typography.headlineSmall)
            PictureChoiceGrid(members.map { Choice(it.id, if (it.sex == "F") "👩" else "👨", it.name) }, selected = who) {
                who = it; whoPicked = true
            }
            SecondaryButton(stringResource(R.string.skip_send_now)) { send("other") }
        } else {
            Text(stringResource(R.string.sos_what), style = MaterialTheme.typography.headlineSmall)
            PictureChoiceGrid(categoryChoices()) { send(it) }
            SecondaryButton(stringResource(R.string.skip_send_now)) { send("other") }
        }
        TextButton(onClick = onCancel) { Text(stringResource(R.string.back)) }
    }
}

/** Case tracker (patient wording or ASHA wording) — live card with names + Call buttons (UI-UX §6.2). */
@Composable
fun CaseTracker(localId: String, patientView: Boolean, onBack: () -> Unit) {
    val vm = LocalVm.current
    val caseId by vm.cases.resolved(localId).collectAsState(initial = localId)
    val activity = LocalContext.current.findActivity()
    val scope = rememberCoroutineScope()
    val case by vm.cases.case(caseId).collectAsState(initial = null)
    val legs by vm.cases.legs(caseId).collectAsState(initial = emptyList())
    val online by vm.online.collectAsState()
    var lastOk by remember { mutableStateOf<Long?>(null) }
    var askCancel by remember { mutableStateOf(false) }
    val numbers by produceState<ChannelNumbers?>(null) { value = vm.sos.channelNumbers() }
    LaunchedPoll(caseId, 5_000) { if (vm.cases.refresh(caseId)) lastOk = System.currentTimeMillis() }
    val c = case ?: run { CircularProgressIndicator(Modifier.padding(24.dp)); return }
    val current = legs.firstOrNull { it.status in setOf("accepted", "picked_up") } ?: legs.firstOrNull()
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        AppCard {
            val closed = c.status in setOf("closed", "follow_up", "cancelled", "arrived_seen")
            val titleColor = when { c.status == "cancelled" -> OfflineGrey; closed -> SuccessGreen; else -> SosRed }
            // Patients read "Help is on the way"; ASHA sees the exact lifecycle step (UI-UX §14)
            Text(if (closed || !patientView) stringResource(Vocab.statusRes(c.status, patientView)) else stringResource(R.string.help_on_way),
                style = MaterialTheme.typography.headlineSmall, color = titleColor, fontWeight = FontWeight.Bold)
            c.shortCode?.let { Text(stringResource(R.string.case_code, it), color = TextMuted, style = MaterialTheme.typography.bodyLarge) }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                when {
                    c.sentVia == "sms" && !c.serverConfirmed -> StatusChip(stringResource(R.string.sent_by_sms), Tone.OFFLINE)
                    !c.serverConfirmed -> StatusChip(stringResource(R.string.saved_will_send), Tone.OFFLINE)
                    else -> StatusChip(stringResource(Vocab.statusRes(c.status, patientView)), Vocab.tone(c.status))
                }
                c.category?.let { StatusChip(stringResource(Vocab.categoryRes(it)), Tone.INFO) }
            }
            Spacer(Modifier.height(12.dp))
            if (c.status != "cancelled") CaseStepper(c.status, patientView, c.currentFacilityName, current?.custodianName, c.etaMinutes)
            Spacer(Modifier.height(8.dp))
            if (!online) Text(stringResource(R.string.offline_last_update), color = TextMuted) else UpdatedAgo(lastOk)
            if (!patientView && c.legTotal != null && c.legCurrent != null)
                Text(stringResource(R.string.leg_x_of_y, c.legCurrent, c.legTotal), style = MaterialTheme.typography.bodyLarge)
        }
        current?.custodianName?.let { PersonCallCard(stringResource(R.string.driver), it, current.custodianPhone) { p -> activity?.let { a -> vm.sos.call(a, p) } } }
        c.currentFacilityName?.let { PersonCallCard(stringResource(R.string.hospital), it, c.currentFacilityPhone) { p -> activity?.let { a -> vm.sos.call(a, p) } } }
        if (!c.serverConfirmed && c.sentVia in setOf("ivr", null) && !online) {
            // Final fallback (FR-A03): one-tap helpline
            numbers?.let { n -> PrimaryButton(stringResource(R.string.call_helpline, n.helpline), color = SosRed) { activity?.let { vm.sos.call(it, n.helpline) } } }
        }
        if (c.status in setOf("created", "matched", "accepted", "transport_assigned")) {
            TextButton(onClick = { askCancel = true }) { Text(stringResource(R.string.cancel_request), color = TextMuted) }
        }
        TextButton(onClick = onBack) { Text(stringResource(R.string.back)) }
    }
    if (askCancel) ConfirmDialog(stringResource(R.string.cancel_request), stringResource(R.string.cancel_confirm_body),
        stringResource(R.string.cancel_yes), danger = true, onDismiss = { askCancel = false }) {
        askCancel = false
        scope.launch { vm.cases.cancel(caseId, "false_alarm") }
    }
}

/** Emergency from the login screen: no account → structured SMS, else IVR, else helpline (FR-A03). */
@Composable
fun EmergencyWithoutLogin(onBack: () -> Unit) {
    val vm = LocalVm.current
    val activity = LocalContext.current.findActivity()
    val scope = rememberCoroutineScope()
    var result by remember { mutableStateOf<SosOutcome?>(null) }
    var holding by rememberSaveable { mutableStateOf(true) }
    val numbers by produceState<ChannelNumbers?>(null) { value = vm.sos.channelNumbers() }
    Text(stringResource(R.string.emergency_no_login), style = MaterialTheme.typography.headlineSmall)
    when {
        result != null -> {
            Text(stringResource(when (result) {
                is SosOutcome.SentBySms, is SosOutcome.ComposerOpened -> R.string.sent_by_sms_long
                else -> R.string.calling_ivr
            }), style = MaterialTheme.typography.titleMedium)
            numbers?.let { n -> PrimaryButton(stringResource(R.string.call_helpline, n.helpline), color = SosRed) { activity?.let { vm.sos.call(it, n.helpline) } } }
        }
        holding -> SosButton { holding = false }
        else -> PictureChoiceGrid(categoryChoices()) { cat ->
            scope.launch { result = vm.sos.raise(activity, cat, null, null, null) }
        }
    }
    TextButton(onClick = onBack) { Text(stringResource(R.string.back)) }
}
