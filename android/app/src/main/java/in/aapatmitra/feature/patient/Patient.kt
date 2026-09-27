package `in`.aapatmitra.feature.patient

import android.app.Activity
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import `in`.aapatmitra.R
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.local.CaseEntity
import `in`.aapatmitra.data.local.PatientEntity
import `in`.aapatmitra.data.sos.ChannelNumbers
import `in`.aapatmitra.domain.Tone
import `in`.aapatmitra.domain.Vocab
import `in`.aapatmitra.feature.me.MeScreen
import `in`.aapatmitra.feature.me.OutboxScreen
import `in`.aapatmitra.feature.onboarding.PermissionsIntro
import `in`.aapatmitra.feature.sos.CaseTracker
import `in`.aapatmitra.feature.sos.SosFlow
import `in`.aapatmitra.nav.LocalVm
import `in`.aapatmitra.nav.findActivity
import `in`.aapatmitra.nav.RoleShell
import `in`.aapatmitra.nav.Tab
import `in`.aapatmitra.nav.rememberNav
import `in`.aapatmitra.ui.components.AppCard
import `in`.aapatmitra.ui.components.EmptyState
import `in`.aapatmitra.ui.components.LaunchedPoll
import `in`.aapatmitra.ui.components.SectionTitle
import `in`.aapatmitra.ui.components.SosButton
import `in`.aapatmitra.ui.components.StatusChip
import `in`.aapatmitra.ui.components.Tile
import `in`.aapatmitra.ui.theme.SosRed
import `in`.aapatmitra.ui.theme.TextMuted
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject

/** Patient / Family — tabs Home · My Family · Me (UI-UX §6.2). */
@Composable
fun PatientGraph() {
    val vm = LocalVm.current
    val permDone by vm.session.permOnboardedFlow.collectAsState(initial = true)
    if (!permDone) { PermissionsIntro { vm.launch { vm.session.setPermOnboarded() } }; return }
    val nav = rememberNav()
    val tabs = listOf(Tab("home", "🏠", R.string.tab_home), Tab("family", "👪", R.string.tab_family), Tab("me", "👤", R.string.tab_me))
    RoleShell(nav, tabs) { m ->
        NavHost(nav, "home", modifier = m) {
            composable("home") { PatientHome(onSos = { nav.navigate("sos") }, onCase = { nav.navigate("case/$it") }, onCard = { nav.navigate("card/$it") }) }
            composable("sos") { PatientSos(onSent = { nav.navigate("case/$it") { popUpTo("home") } }, onCancel = { nav.popBackStack() }) }
            composable("case/{id}") { CaseTracker(it.arguments!!.getString("id")!!, patientView = true) { nav.popBackStack() } }
            composable("family") { Family { nav.navigate("card/$it") } }
            composable("card/{id}") { HealthCard(it.arguments!!.getString("id")!!) }
            composable("me") { MeScreen() }
            composable("outbox") { OutboxScreen { nav.popBackStack() } }
        }
    }
}

@Composable
private fun PatientHome(onSos: () -> Unit, onCase: (String) -> Unit, onCard: (String) -> Unit) {
    val vm = LocalVm.current
    val me by vm.me.collectAsState()
    val open by vm.cases.openCases().collectAsState(initial = emptyList())
    val activity = LocalContext.current.findActivity()
    val numbers by produceState<ChannelNumbers?>(null) { value = vm.sos.channelNumbers() }
    val nextVisit by produceState<String?>(null) {
        value = vm.db.reference().byPrefix("care_plan:").mapNotNull { AppJson.parseToJsonElement(it.json).jsonObject.str("nextVisitOn") }
            .filter { it >= Time.today() }.minOrNull()
    }
    LaunchedPoll(me?.id, 30_000) { vm.cases.refreshMine() }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(16.dp)) {
        val active = open.firstOrNull()
        if (active != null) LiveCaseCard(active) { onCase(active.id) }   // replaces the SOS button (UI-UX §6.2)
        else SosButton(onConfirmed = onSos)
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Tile("📞", stringResource(R.string.call_helpline, numbers?.helpline ?: "108"), Modifier.weight(1f)) {
                numbers?.let { n -> activity?.let { vm.sos.call(it, n.helpline) } }
            }
            Tile("🪪", stringResource(R.string.health_card), Modifier.weight(1f)) { me?.patientId?.let(onCard) }
        }
        nextVisit?.let { AppCard { Text("⏳ " + stringResource(R.string.next_visit, it), style = MaterialTheme.typography.titleMedium) } }
    }
}

@Composable
fun LiveCaseCard(c: CaseEntity, onOpen: () -> Unit) {
    AppCard(onClick = onOpen) {
        Text("🔴 " + stringResource(R.string.help_on_way), style = MaterialTheme.typography.headlineSmall, color = SosRed)
        Text(stringResource(Vocab.statusRes(c.status, true)), style = MaterialTheme.typography.titleMedium)
        c.etaMinutes?.let { Text(stringResource(R.string.eta_minutes, it), style = MaterialTheme.typography.bodyLarge) }
        if (c.sentVia == "sms" && !c.serverConfirmed) StatusChip(stringResource(R.string.sent_by_sms), Tone.OFFLINE)
        Text(stringResource(R.string.tap_to_open), color = TextMuted, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun PatientSos(onSent: (String) -> Unit, onCancel: () -> Unit) {
    val vm = LocalVm.current
    val me by vm.me.collectAsState()
    val hh = me?.householdId
    val members by (if (hh != null) vm.households.members(hh) else vm.households.patients()).collectAsState(initial = emptyList())
    SosFlow(members, preselected = me?.patientId, householdId = hh, onSent = onSent, onCancel = onCancel)
}

@Composable
private fun Family(onOpen: (String) -> Unit) {
    val vm = LocalVm.current
    val me by vm.me.collectAsState()
    val members by (me?.householdId?.let { vm.households.members(it) } ?: vm.households.patients()).collectAsState(initial = emptyList())
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.tab_family), style = MaterialTheme.typography.headlineSmall)
        if (members.isEmpty()) EmptyState("👪", stringResource(R.string.family_empty))
        members.forEach { p -> MemberRow(p) { onOpen(p.id) } }
    }
}

@Composable
fun MemberRow(p: PatientEntity, onClick: () -> Unit) {
    AppCard(onClick = onClick) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(if (p.sex == "F") "👩" else "👨", style = MaterialTheme.typography.headlineSmall)
            Column(Modifier.padding(start = 12.dp).weight(1f)) {
                Text(p.name, style = MaterialTheme.typography.titleMedium)
                Text(listOfNotNull(Time.ageYears(p.dateOfBirth)?.let { stringResource(R.string.age_years, it) }, p.shortCode).joinToString(" · "),
                    color = TextMuted, style = MaterialTheme.typography.bodySmall)
            }
            if (p.highRisk) StatusChip(stringResource(R.string.high_risk), Tone.PROBLEM)
            if (p.dirty) StatusChip(stringResource(R.string.saved_will_send), Tone.OFFLINE)
        }
        CohortChips(p)
    }
}

@Composable
fun CohortChips(p: PatientEntity) {
    val cohorts = runCatching { AppJson.parseToJsonElement(p.cohortsJson).jsonArray }.getOrDefault(JsonArray(emptyList()))
        .mapNotNull { it as? JsonObject }.filter { it.str("endedOn") == null }.mapNotNull { it.str("cohort") }
    if (cohorts.isEmpty()) return
    Row(Modifier.padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        cohorts.forEach { StatusChip(stringResource(Vocab.cohortRes(it)), Tone.INFO) }
    }
}

/** Health card (UI-UX §6.2 My Family): read-only for families. */
@Composable
fun HealthCard(patientId: String) {
    val vm = LocalVm.current
    val p by vm.households.patientFlow(patientId).collectAsState(initial = null)
    val extras by produceState(Pair(emptyList<String>(), emptyList<String>()), patientId) {
        suspend fun of(prefix: String, f: (JsonObject) -> String?) = vm.db.reference().byPrefix(prefix)
            .map { AppJson.parseToJsonElement(it.json).jsonObject }.filter { it.str("patientId") == patientId }.mapNotNull(f)
        value = of("patient_condition:") { it.str("conditionCode") } to of("patient_medication:") {
            listOfNotNull(it.str("medicineName"), it.str("doseText")).joinToString(" ") }
    }
    val patient = p ?: return
    LaunchedEffect(patientId) { }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(patient.name, style = MaterialTheme.typography.headlineSmall)
        patient.shortCode?.let { Text(stringResource(R.string.health_id, it), color = TextMuted, style = MaterialTheme.typography.bodyLarge) }
        CohortChips(patient)
        AppCard {
            Row(Modifier.fillMaxWidth()) {
                Info(stringResource(R.string.age), Time.ageYears(patient.dateOfBirth)?.toString() ?: "—", Modifier.weight(1f))
                Info(stringResource(R.string.blood_group), patient.bloodGroup ?: "—", Modifier.weight(1f))
            }
        }
        SectionTitle(stringResource(R.string.conditions))
        Text(extras.first.joinToString(", ").ifBlank { "—" }, style = MaterialTheme.typography.bodyLarge)
        SectionTitle(stringResource(R.string.medicines))
        Text(extras.second.joinToString("\n").ifBlank { "—" }, style = MaterialTheme.typography.bodyLarge)
    }
}

@Composable
private fun Info(label: String, value: String, modifier: Modifier) {
    Column(modifier) {
        Text(label, color = TextMuted, style = MaterialTheme.typography.bodySmall)
        Text(value, style = MaterialTheme.typography.titleMedium)
    }
}
