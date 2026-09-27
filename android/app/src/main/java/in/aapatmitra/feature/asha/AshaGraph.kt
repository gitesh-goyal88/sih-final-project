package `in`.aapatmitra.feature.asha

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import `in`.aapatmitra.R
import `in`.aapatmitra.domain.Vocab
import `in`.aapatmitra.feature.me.MeScreen
import `in`.aapatmitra.feature.me.OutboxScreen
import `in`.aapatmitra.feature.me.PinGate
import `in`.aapatmitra.feature.onboarding.PermissionsIntro
import `in`.aapatmitra.feature.patient.MemberRow
import `in`.aapatmitra.feature.sos.CaseTracker
import `in`.aapatmitra.feature.sos.SosFlow
import `in`.aapatmitra.nav.LocalVm
import `in`.aapatmitra.nav.RoleShell
import `in`.aapatmitra.nav.Tab
import `in`.aapatmitra.nav.rememberNav
import `in`.aapatmitra.ui.components.EmptyState
import `in`.aapatmitra.ui.components.SectionTitle
import `in`.aapatmitra.ui.components.SosButton
import `in`.aapatmitra.ui.components.TaskRow
import `in`.aapatmitra.ui.theme.SosRed
import `in`.aapatmitra.ui.theme.SuccessGreen
import `in`.aapatmitra.ui.theme.WarningAmber

/** ASHA — tabs Today · Households · SOS · Me (UI-UX §6.3). Clinical tabs sit behind the app PIN; SOS never does. */
@Composable
fun AshaGraph() {
    val vm = LocalVm.current
    val permDone by vm.session.permOnboardedFlow.collectAsState(initial = true)
    if (!permDone) { PermissionsIntro { vm.launch { vm.session.setPermOnboarded() } }; return }
    val nav = rememberNav()
    var unlocked by rememberSaveable { mutableStateOf(false) }
    val tabs = listOf(Tab("today", "📅", R.string.tab_today), Tab("households", "🏠", R.string.tab_households),
        Tab("sos", "🆘", R.string.tab_sos), Tab("me", "👤", R.string.tab_me))
    RoleShell(nav, tabs) { m ->
        NavHost(nav, "today", modifier = m) {
            composable("today") { PinGate(unlocked, { unlocked = true }) { Today(onPatient = { nav.navigate("patient/$it") }, onCase = { nav.navigate("case/$it") }) } }
            composable("households") { PinGate(unlocked, { unlocked = true }) { Households(onPatient = { nav.navigate("patient/$it") }, onAdd = { nav.navigate("add") }) } }
            composable("add") { AddHousehold(onDone = { nav.popBackStack() }) }
            composable("patient/{id}") { b ->
                val id = b.arguments!!.getString("id")!!
                PinGate(unlocked, { unlocked = true }) {
                    PatientRecord(id, onScreen = { nav.navigate("screening/$id") }, onRefer = { nav.navigate("referral/$id") },
                        onCase = { nav.navigate("case/$it") })
                }
            }
            composable("screening/{id}") { b ->
                val id = b.arguments!!.getString("id")!!
                Screening(id, onRefer = { nav.navigate("referral/$id") { popUpTo("patient/$id") } }, onDone = { nav.popBackStack() })
            }
            composable("referral/{id}") { b ->
                Referral(b.arguments!!.getString("id")!!, onSent = { nav.navigate("case/$it") { popUpTo("today") } })
            }
            composable("sos") { AshaSosPick { pid -> nav.navigate("sosSend/$pid") } }
            composable("sosSend/{id}") { b -> AshaSosSend(b.arguments!!.getString("id")!!, onSent = { nav.navigate("case/$it") { popUpTo("today") } }) { nav.popBackStack() } }
            composable("case/{id}") { CaseTracker(it.arguments!!.getString("id")!!, patientView = false) { nav.popBackStack() } }
            composable("me") { MeScreen() }
            composable("outbox") { OutboxScreen { nav.popBackStack() } }
        }
    }
}

/** Today: Urgent → Due today → Done (UI-UX §6.3). */
@Composable
private fun Today(onPatient: (String) -> Unit, onCase: (String) -> Unit) {
    val vm = LocalVm.current
    val tasksOrNull by vm.care.today().collectAsState(initial = null)
    val casesOrNull by vm.cases.openCases().collectAsState(initial = null)
    var showDone by remember { mutableStateOf(false) }
    if (tasksOrNull == null || casesOrNull == null) { Text(stringResource(R.string.loading), Modifier.padding(16.dp)); return }
    val tasks = tasksOrNull!!
    val cases = casesOrNull!!
    val urgent = tasks.filter { it.status == "open" && it.priority == "urgent" }
    val due = tasks.filter { it.status == "open" && it.priority != "urgent" }
    val done = tasks.filter { it.status == "done" }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(stringResource(R.string.today_n_tasks, urgent.size + due.size + cases.size), style = MaterialTheme.typography.headlineSmall)
        if (cases.isNotEmpty() || urgent.isNotEmpty()) SectionTitle("🔴 " + stringResource(R.string.urgent, cases.size + urgent.size), SosRed)
        cases.forEach { c ->
            TaskRow("🚑", stringResource(if (c.type == "sos") R.string.sos_case else R.string.referral_case) + (c.shortCode?.let { " · $it" } ?: ""),
                stringResource(Vocab.statusRes(c.status, false)), stringResource(R.string.open), urgent = true) { onCase(c.id) }
        }
        urgent.forEach { t -> TaskRow(Vocab.taskIcon(t.taskType), taskTitle(t.taskType, t.title) + " · " + (t.patientName ?: ""), t.dueDate,
            stringResource(R.string.open), urgent = true) { onPatient(t.patientId) } }
        if (due.isNotEmpty()) SectionTitle("⏳ " + stringResource(R.string.due_today), WarningAmber)
        due.forEach { t -> TaskRow(Vocab.taskIcon(t.taskType), taskTitle(t.taskType, t.title) + " · " + (t.patientName ?: ""), t.dueDate,
            stringResource(R.string.open)) { onPatient(t.patientId) } }
        if (urgent.isEmpty() && due.isEmpty() && cases.isEmpty()) EmptyState("✅", stringResource(R.string.no_tasks_today))
        if (done.isNotEmpty()) {
            SectionTitle("✔ " + stringResource(R.string.done_n, done.size) + if (showDone) " ▲" else " ▼", SuccessGreen)
            androidx.compose.material3.TextButton(onClick = { showDone = !showDone }) { Text(stringResource(if (showDone) R.string.hide else R.string.show)) }
            if (showDone) done.forEach { t -> TaskRow("✔", taskTitle(t.taskType, t.title) + " · " + (t.patientName ?: ""), t.dueDate, "") { onPatient(t.patientId) } }
        }
    }
}

@Composable
fun taskTitle(type: String, title: String?): String = title ?: stringResource(when (type) {
    "anc_visit" -> R.string.tt_anc; "pnc_visit" -> R.string.tt_pnc; "newborn_check" -> R.string.tt_newborn
    "bp_check" -> R.string.tt_bp; "sugar_check" -> R.string.tt_sugar; "medicine_adherence" -> R.string.tt_medicine
    "referral_followup" -> R.string.tt_referral; "high_risk_recheck" -> R.string.tt_recheck
    "immunisation" -> R.string.tt_immunisation; "teleconsult_followup" -> R.string.tt_teleconsult
    else -> R.string.tt_other
})

/** Households: search by name or house number, plus a village filter (UI-UX §6.3). */
@Composable
private fun Households(onPatient: (String) -> Unit, onAdd: () -> Unit) {
    val vm = LocalVm.current
    var q by rememberSaveable { mutableStateOf("") }
    val all by vm.households.patients().collectAsState(initial = emptyList())
    val hits by vm.households.search(q.ifBlank { "zz_none" }).collectAsState(initial = emptyList())
    val houses by vm.households.households().collectAsState(initial = emptyList())
    val byHouse = houses.associateBy { it.id }
    val shown = when {
        q.isBlank() -> all
        q.all { it.isDigit() } -> all.filter { byHouse[it.householdId]?.houseNumber == q }
        else -> hits
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        `in`.aapatmitra.ui.components.PrimaryButton("➕ " + stringResource(R.string.add_household), onClick = onAdd)
        OutlinedTextField(q, { q = it }, label = { Text(stringResource(R.string.search_name_house)) }, singleLine = true, modifier = Modifier.fillMaxWidth())
        if (shown.isEmpty()) EmptyState("🏠", stringResource(R.string.no_households))
        shown.groupBy { it.householdId }.forEach { (hh, members) ->
            SectionTitle(stringResource(R.string.house_no, byHouse[hh]?.houseNumber ?: "—"))
            members.forEach { p -> MemberRow(p) { onPatient(p.id) } }
        }
    }
}

/** ASHA SOS: pick the patient from her households first (UI-UX §6.3). */
@Composable
private fun AshaSosPick(onPick: (String) -> Unit) {
    val vm = LocalVm.current
    var q by rememberSaveable { mutableStateOf("") }
    val all by vm.households.patients().collectAsState(initial = emptyList())
    val hits by vm.households.search(q.ifBlank { "zz_none" }).collectAsState(initial = emptyList())
    val shown = if (q.isBlank()) all.take(30) else hits
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(stringResource(R.string.sos_pick_patient), style = MaterialTheme.typography.headlineSmall, color = SosRed)
        OutlinedTextField(q, { q = it }, label = { Text(stringResource(R.string.search_name_house)) }, singleLine = true, modifier = Modifier.fillMaxWidth())
        shown.forEach { p -> MemberRow(p) { onPick(p.id) } }
        if (shown.isEmpty()) EmptyState("🔎", stringResource(R.string.no_match))
    }
}

@Composable
private fun AshaSosSend(patientId: String, onSent: (String) -> Unit, onCancel: () -> Unit) {
    val vm = LocalVm.current
    val p by vm.households.patientFlow(patientId).collectAsState(initial = null)
    var held by rememberSaveable { mutableStateOf(false) }
    val patient = p ?: return
    if (!held) {
        Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp),
            horizontalAlignment = androidx.compose.ui.Alignment.CenterHorizontally) {
            Text(patient.name, style = MaterialTheme.typography.headlineSmall)
            SosButton { held = true }
        }
    } else SosFlow(listOf(patient), patient.id, patient.householdId, onSent, onCancel)
}
