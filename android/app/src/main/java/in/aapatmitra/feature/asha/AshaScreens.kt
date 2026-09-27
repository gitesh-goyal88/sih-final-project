package `in`.aapatmitra.feature.asha

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import `in`.aapatmitra.R
import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Time
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.repo.FacilityOption
import `in`.aapatmitra.data.repo.MatchResult
import `in`.aapatmitra.data.repo.MemberDraft
import `in`.aapatmitra.data.repo.Symptom
import `in`.aapatmitra.data.repo.Village
import `in`.aapatmitra.domain.RiskEvaluator
import `in`.aapatmitra.domain.Tone
import `in`.aapatmitra.domain.Vocab
import `in`.aapatmitra.feature.patient.CohortChips
import `in`.aapatmitra.nav.LocalVm
import `in`.aapatmitra.ui.components.AppCard
import `in`.aapatmitra.ui.components.ChipRow
import `in`.aapatmitra.ui.components.Feedback
import `in`.aapatmitra.ui.components.NumberPadField
import `in`.aapatmitra.ui.components.PrimaryButton
import `in`.aapatmitra.ui.components.SecondaryButton
import `in`.aapatmitra.ui.components.SectionTitle
import `in`.aapatmitra.ui.components.SelectChip
import `in`.aapatmitra.ui.components.StatusChip
import `in`.aapatmitra.ui.components.UpdatedAgo
import `in`.aapatmitra.ui.theme.MitraBlueTint
import `in`.aapatmitra.ui.theme.SosRed
import `in`.aapatmitra.ui.theme.SuccessGreen
import `in`.aapatmitra.ui.theme.TextMuted
import kotlinx.coroutines.launch
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject

// ---------------- Add household wizard (one step per screen) ----------------

@Composable
fun AddHousehold(onDone: () -> Unit) {
    val vm = LocalVm.current
    val scope = rememberCoroutineScope()
    val villages by produceState(emptyList<Village>()) { value = vm.households.villages() }
    val me by vm.me.collectAsState()
    var step by rememberSaveable { mutableIntStateOf(0) }
    var village by rememberSaveable { mutableStateOf<String?>(null) }
    var house by rememberSaveable { mutableStateOf("") }
    val members = remember { mutableStateListOf(MemberDraft(relationship = "self")) }
    var consent by rememberSaveable { mutableStateOf(false) }
    val mine = villages.filter { me?.villageIds?.contains(it.id) != false }
    val steps = 5
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.add_household) + " · " + stringResource(R.string.step_x_of_y, step + 1, steps), style = MaterialTheme.typography.titleMedium)
        LinearProgressIndicator(progress = { (step + 1f) / steps }, modifier = Modifier.fillMaxWidth())
        when (step) {
            0 -> {
                Text(stringResource(R.string.village), style = MaterialTheme.typography.headlineSmall)
                mine.forEach { v -> SelectChip(v.name, village == v.id) { village = v.id } }
                OutlinedTextField(house, { house = it.take(20) }, label = { Text(stringResource(R.string.house_number)) }, singleLine = true,
                    modifier = Modifier.fillMaxWidth())
                PrimaryButton(stringResource(R.string.next), enabled = village != null) { step++ }
            }
            1 -> {
                Text(stringResource(R.string.head_of_family), style = MaterialTheme.typography.headlineSmall)
                MemberForm(members[0], showRelation = false) { members[0] = it }
                PrimaryButton(stringResource(R.string.next), enabled = members[0].name.isNotBlank()) { step++ }
            }
            2 -> {
                Text(stringResource(R.string.members), style = MaterialTheme.typography.headlineSmall)
                members.drop(1).forEachIndexed { i, m -> AppCard { MemberForm(m, showRelation = true) { members[i + 1] = it } } }
                SecondaryButton("➕ " + stringResource(R.string.add_member)) { members.add(MemberDraft(relationship = "child")) }
                PrimaryButton(stringResource(R.string.next)) { step++ }
            }
            3 -> {
                Text(stringResource(R.string.special_status), style = MaterialTheme.typography.headlineSmall)
                members.forEachIndexed { i, m ->
                    if (m.name.isBlank()) return@forEachIndexed
                    AppCard {
                        Text(m.name, style = MaterialTheme.typography.titleMedium)
                        Row(Modifier.padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            listOf("pregnant", "newborn").filter { it != "pregnant" || m.sex == "F" }.forEach { c ->
                                SelectChip(stringResource(Vocab.cohortRes(c)), c in m.cohorts) { members[i] = m.copy(cohorts = m.cohorts.toggle(c)) }
                            }
                        }
                        Row(Modifier.padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            listOf("chronic", "elderly").forEach { c ->
                                SelectChip(stringResource(Vocab.cohortRes(c)), c in m.cohorts) { members[i] = m.copy(cohorts = m.cohorts.toggle(c)) }
                            }
                        }
                    }
                }
                PrimaryButton(stringResource(R.string.next)) { step++ }
            }
            else -> {
                Text(stringResource(R.string.consent_title), style = MaterialTheme.typography.headlineSmall)
                AppCard(color = MitraBlueTint) { Text(stringResource(R.string.consent_notice), style = MaterialTheme.typography.bodyLarge) }
                ChipRow {
                    SelectChip(stringResource(R.string.consent_yes), consent) { consent = true }
                    SelectChip(stringResource(R.string.consent_no), !consent) { consent = false }
                }
                if (!consent) Text(stringResource(R.string.consent_no_help), color = TextMuted, style = MaterialTheme.typography.bodyLarge)
                PrimaryButton(stringResource(R.string.save)) {
                    scope.launch { vm.households.create(village!!, house, members.toList(), consent); onDone() }
                }
            }
        }
        if (step > 0) TextButton(onClick = { step-- }) { Text(stringResource(R.string.back)) }
    }
}

private fun Set<String>.toggle(v: String) = if (v in this) this - v else this + v

@Composable
private fun MemberForm(m: MemberDraft, showRelation: Boolean, onChange: (MemberDraft) -> Unit) {
    OutlinedTextField(m.name, { onChange(m.copy(name = it.take(80))) }, label = { Text(stringResource(R.string.name)) }, singleLine = true,
        modifier = Modifier.fillMaxWidth())
    OutlinedTextField(m.ageYears?.toString() ?: "", { v -> onChange(m.copy(ageYears = v.filter(Char::isDigit).take(3).toIntOrNull())) },
        label = { Text(stringResource(R.string.age_years_label)) }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
    ChipRow {
        SelectChip(stringResource(R.string.female), m.sex == "F") { onChange(m.copy(sex = "F")) }
        SelectChip(stringResource(R.string.male), m.sex == "M") { onChange(m.copy(sex = "M", cohorts = m.cohorts - "pregnant")) }
        SelectChip(stringResource(R.string.other_sex), m.sex == "O") { onChange(m.copy(sex = "O")) }
    }
    OutlinedTextField(m.phone, { onChange(m.copy(phone = it.filter(Char::isDigit).take(10))) }, label = { Text(stringResource(R.string.phone_optional)) },
        singleLine = true, modifier = Modifier.fillMaxWidth(), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone))
    if (showRelation) ChipRow {
        listOf("spouse" to R.string.rel_spouse, "child" to R.string.rel_child, "parent" to R.string.rel_parent, "other" to R.string.rel_other)
            .forEach { (k, r) -> SelectChip(stringResource(r), m.relationship == k) { onChange(m.copy(relationship = k)) } }
    }
}

// ---------------- Patient record (ASHA view) ----------------

@Composable
fun PatientRecord(id: String, onScreen: () -> Unit, onRefer: () -> Unit, onCase: (String) -> Unit) {
    val vm = LocalVm.current
    val scope = rememberCoroutineScope()
    val p by vm.households.patientFlow(id).collectAsState(initial = null)
    val entries by vm.care.entries(id).collectAsState(initial = emptyList())
    val tasks by vm.care.openTasksFor(id).collectAsState(initial = emptyList())
    val cases by vm.cases.casesFor(id).collectAsState(initial = emptyList())
    val patient = p ?: return
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(patient.name, style = MaterialTheme.typography.headlineSmall)
        Text(listOfNotNull(Time.ageYears(patient.dateOfBirth)?.let { stringResource(R.string.age_years, it) }, patient.shortCode,
            patient.phone).joinToString(" · "), color = TextMuted, style = MaterialTheme.typography.bodyLarge)
        CohortChips(patient)
        if (patient.highRisk) StatusChip(stringResource(R.string.high_risk), Tone.PROBLEM)
        PrimaryButton(stringResource(R.string.start_screening), onClick = onScreen)
        SecondaryButton(stringResource(R.string.send_to_hospital), onClick = onRefer)
        if (tasks.isNotEmpty()) SectionTitle(stringResource(R.string.follow_ups))
        tasks.forEach { t ->
            AppCard {
                Text(taskTitle(t.taskType, t.title) + " · " + t.dueDate, style = MaterialTheme.typography.titleMedium)
                TextButton(onClick = { scope.launch { vm.care.markDone(t) } }) { Text("✔ " + stringResource(R.string.mark_done)) }
            }
        }
        SectionTitle(stringResource(R.string.health_summary))
        val latest = entries.firstOrNull { it.vitalsJson != null }
        AppCard { Text(latest?.vitalsJson?.let { vitalsLine(it) } ?: "—", style = MaterialTheme.typography.bodyLarge) }
        SectionTitle(stringResource(R.string.visits))
        if (entries.isEmpty()) Text("—")
        entries.take(20).forEach { e ->
            AppCard {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(java.text.DateFormat.getDateInstance().format(java.util.Date(e.recordedAt)), style = MaterialTheme.typography.titleMedium)
                    if (e.highRisk) StatusChip(stringResource(R.string.high_risk), Tone.PROBLEM)
                    if (e.pending) StatusChip(stringResource(R.string.saved_will_send), Tone.OFFLINE)
                }
                Text(e.kind + (e.authorName?.let { " · $it" } ?: ""), color = TextMuted, style = MaterialTheme.typography.bodySmall)
                e.vitalsJson?.let { Text(vitalsLine(it), style = MaterialTheme.typography.bodyLarge) }
                e.notes?.let { Text(it, style = MaterialTheme.typography.bodyLarge) }
            }
        }
        SectionTitle(stringResource(R.string.referrals))
        if (cases.isEmpty()) Text("—")
        cases.forEach { c ->
            AppCard(onClick = { onCase(c.id) }) {
                Text((c.shortCode ?: "…") + " · " + stringResource(Vocab.statusRes(c.status, false)), style = MaterialTheme.typography.titleMedium)
                c.currentFacilityName?.let { Text(it, color = TextMuted) }
            }
        }
    }
}

private fun vitalsLine(json: String): String = runCatching {
    val o = AppJson.parseToJsonElement(json).jsonObject
    buildList {
        val s = o.str("bpSystolic"); val d = o.str("bpDiastolic")
        if (s != null && d != null) add("BP $s/$d")
        o.str("pulseBpm")?.let { add("Pulse $it") }
        o.str("spo2Pct")?.let { add("SpO₂ $it%") }
        o.str("tempC")?.let { add("$it °C") }
        o.str("weightKg")?.let { add("$it kg") }
        o.str("hbGDl")?.let { add("Hb $it") }
        o.str("rbsMgDl")?.let { add("Sugar $it") }
        o.str("gestationWeeks")?.let { add("$it wk") }
    }.joinToString(" · ")
}.getOrDefault("")

// ---------------- Screening wizard (one question per screen) ----------------

private data class VitalStep(val title: Int, val fields: List<Pair<String, Int>>, val decimal: Boolean = false, val cohort: String? = null)

private val VITAL_STEPS = listOf(
    VitalStep(R.string.v_bp, listOf("bpSystolic" to R.string.v_systolic, "bpDiastolic" to R.string.v_diastolic)),
    VitalStep(R.string.v_pulse_spo2, listOf("pulseBpm" to R.string.v_pulse, "spo2Pct" to R.string.v_spo2)),
    VitalStep(R.string.v_temp, listOf("tempC" to R.string.v_temp_c), decimal = true),
    VitalStep(R.string.v_pregnancy, listOf("gestationWeeks" to R.string.v_weeks, "hbGDl" to R.string.v_hb), decimal = true, cohort = "pregnant"),
    VitalStep(R.string.v_weight, listOf("weightKg" to R.string.v_weight_kg), decimal = true, cohort = "newborn"),
    VitalStep(R.string.v_sugar, listOf("rbsMgDl" to R.string.v_rbs), cohort = "chronic"),
)

@Composable
fun Screening(patientId: String, onRefer: () -> Unit, onDone: () -> Unit) {
    val vm = LocalVm.current
    val scope = rememberCoroutineScope()
    val p by vm.households.patientFlow(patientId).collectAsState(initial = null)
    val rules by produceState(emptyList<RiskEvaluator.Rule>()) { value = vm.care.rules() }
    val symptomsAll by produceState(emptyList<Symptom>()) { value = vm.care.symptoms() }
    val lang by vm.language.collectAsState()
    val patient = p ?: return
    val cohorts = remember(patient.cohortsJson) {
        runCatching { AppJson.parseToJsonElement(patient.cohortsJson).jsonArray.map { it.jsonObject }
            .filter { it.str("endedOn") == null }.mapNotNull { it.str("cohort") }.toSet() }.getOrDefault(emptySet())
    }
    val steps = VITAL_STEPS.filter { it.cohort == null || it.cohort in cohorts }
    val symptoms = symptomsAll.filter { s -> s.cohorts.isEmpty() || s.cohorts.any { it in cohorts || it == "any" } }.take(6)
    val values = remember { mutableStateMapOf<String, String>() }
    val answers = remember { mutableStateMapOf<String, Boolean>() }
    var step by rememberSaveable { mutableIntStateOf(0) }
    var fired by remember { mutableStateOf<List<RiskEvaluator.Rule>?>(null) }
    val total = steps.size + 2
    fun vitals() = values.mapNotNull { (k, v) -> v.toDoubleOrNull()?.let { k to it } }.toMap()

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text(stringResource(R.string.screening_of, patient.name) + " · " + stringResource(R.string.step_x_of_y, step + 1, total),
            style = MaterialTheme.typography.titleMedium)
        LinearProgressIndicator(progress = { (step + 1f) / total }, modifier = Modifier.fillMaxWidth())
        when {
            step < steps.size -> {
                val s = steps[step]
                Text(stringResource(s.title), style = MaterialTheme.typography.headlineSmall)
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    s.fields.forEach { (k, label) ->
                        NumberPadField(stringResource(label), values[k] ?: "", { values[k] = it }, Modifier.weight(1f), decimal = s.decimal)
                    }
                }
                // Instant plain feedback from the same rules the server uses (no invented thresholds)
                val entered = s.fields.any { values[it.first]?.toDoubleOrNull() != null }
                if (entered) {
                    val cols = s.fields.mapNotNull { RiskEvaluator.VITAL_COLUMNS[it.first] }.toSet()
                    val bad = RiskEvaluator.fired(rules, cohorts, vitals(), emptyMap()).any { r -> cols.any { r.expression.toString().contains("\"$it\"") } }
                    Feedback(!bad, stringResource(R.string.fb_normal), stringResource(R.string.fb_high))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    SecondaryButton(stringResource(R.string.skip), Modifier.weight(1f)) { s.fields.forEach { values.remove(it.first) }; step++ }
                    PrimaryButton(stringResource(R.string.next), Modifier.weight(1f)) { step++ }
                }
            }
            step == steps.size -> {
                Text(stringResource(R.string.symptoms_q), style = MaterialTheme.typography.headlineSmall)
                symptoms.forEach { s ->
                    AppCard {
                        Text(if (lang == "en") s.labelEn else s.labelHi, style = MaterialTheme.typography.titleMedium)
                        ChipRow {
                            SelectChip(stringResource(R.string.yes), answers[s.code] == true) { answers[s.code] = true }
                            SelectChip(stringResource(R.string.no), answers[s.code] == false) { answers[s.code] = false }
                        }
                    }
                }
                PrimaryButton(stringResource(R.string.see_result), enabled = vitals().isNotEmpty() || answers.isNotEmpty()) {
                    scope.launch {
                        fired = vm.care.saveScreening(patientId, cohorts, vitals(), answers.toMap(), null)
                        step++
                    }
                }
            }
            else -> {
                val f = fired.orEmpty()
                Text(stringResource(R.string.result), style = MaterialTheme.typography.headlineSmall)
                if (f.isEmpty()) Text("✔ " + stringResource(R.string.result_ok), color = SuccessGreen, style = MaterialTheme.typography.titleMedium)
                else {
                    Text("⚠ " + stringResource(R.string.result_risk), color = SosRed, style = MaterialTheme.typography.titleMedium)
                    f.forEach { r -> Text("• " + ((if (lang == "en") r.descriptionEn else r.descriptionHi) ?: r.code), style = MaterialTheme.typography.bodyLarge) }
                }
                Text(stringResource(R.string.care_needed), style = MaterialTheme.typography.headlineSmall)
                PrimaryButton(stringResource(R.string.yes_send_hospital), color = if (f.isNotEmpty()) SosRed else `in`.aapatmitra.ui.theme.MitraBlue, onClick = onRefer)
                Text(stringResource(R.string.no_follow_up), style = MaterialTheme.typography.titleMedium)
                ChipRow {
                    listOf(7 to R.string.fu_1w, 14 to R.string.fu_2w, 30 to R.string.fu_1m).forEach { (d, r) ->
                        SelectChip(stringResource(r), false) {
                            // A fired rule already makes the server create its own re-check task; this is the ASHA's next visit
                            val type = when { "pregnant" in cohorts -> "anc_visit"; "newborn" in cohorts -> "newborn_check"
                                "chronic" in cohorts -> "bp_check"; else -> "high_risk_recheck" }
                            scope.launch { vm.care.addFollowUp(patientId, patient.name, d, type); onDone() }
                        }
                    }
                }
                Text(stringResource(R.string.saved_will_send), color = TextMuted)
            }
        }
    }
}

// ---------------- Referral — facility matching ----------------

@Composable
fun Referral(patientId: String, onSent: (String) -> Unit) {
    val vm = LocalVm.current
    val scope = rememberCoroutineScope()
    val p by vm.households.patientFlow(patientId).collectAsState(initial = null)
    val lang by vm.language.collectAsState()
    val online by vm.online.collectAsState()
    val caps by produceState(emptyList<Triple<String, String, String>>()) {
        value = vm.db.reference().byPrefix("capability:").map { AppJson.parseToJsonElement(it.json).jsonObject }
            .map { Triple(it.str("code")!!, it.str("labelEn") ?: "", it.str("labelHi") ?: "") }
    }
    val defaults by produceState(emptyMap<String, List<String>>()) {
        value = vm.db.reference().byPrefix("emergency_category:").map { AppJson.parseToJsonElement(it.json).jsonObject }
            .associate { it.str("code")!! to (it["defaultCapabilities"]?.jsonArray?.map { c -> c.toString().trim('"') } ?: emptyList()) }
    }
    val needs = remember { mutableStateListOf<String>() }
    var result by remember { mutableStateOf<MatchResult?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val patient = p ?: return
    if (needs.isEmpty() && defaults.isNotEmpty()) {
        val c = patient.cohortsJson
        val cat = when { c.contains("pregnant") -> "pregnancy"; c.contains("newborn") -> "newborn"; else -> null }
        (defaults[cat] ?: listOf("emergency_opd")).forEach { if (it !in needs) needs.add(it) }
    }
    fun label(code: String) = caps.firstOrNull { it.first == code }?.let { if (lang == "en") it.second else it.third } ?: code
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.send_to_hospital) + " · " + patient.name, style = MaterialTheme.typography.headlineSmall)
        Text(stringResource(R.string.need), style = MaterialTheme.typography.titleMedium)
        caps.chunked(2).forEach { row -> ChipRow { row.forEach { (code, _, _) -> SelectChip(label(code), code in needs) { if (code in needs) needs.remove(code) else needs.add(code) } } } }
        if (!online) Text(stringResource(R.string.referral_offline), color = TextMuted, style = MaterialTheme.typography.bodyLarge)
        PrimaryButton(stringResource(R.string.find_hospital), enabled = needs.isNotEmpty() && !busy && online) {
            busy = true; error = null
            scope.launch {
                vm.cases.match(patientId, needs.toList()).onSuccess { result = it }.onFailure { error = it.message }
                busy = false
            }
        }
        error?.let { Text(it, color = SosRed) }
        result?.let { r ->
            if (r.noCapable) Text(stringResource(R.string.no_capable), color = SosRed)
            r.options.forEach { f -> FacilityCard(f, ::label) { scope.launch { onSent(vm.cases.createReferral(patientId, needs.toList(), f.id, null)) } } }
            UpdatedAgo(r.computedAt)
        }
        if (!online) SecondaryButton(stringResource(R.string.send_anyway_offline), enabled = needs.isNotEmpty()) {
            scope.launch { onSent(vm.cases.createReferral(patientId, needs.toList(), null, null)) }
        }
    }
}

/** Facility card: name → capability ✔ chips → distance/ETA → Send request (UI-UX §11). */
@Composable
private fun FacilityCard(f: FacilityOption, label: (String) -> String, onSend: () -> Unit) {
    AppCard {
        Row { Text(f.name, style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f)); Text("%.0f km".format(f.distanceKm)) }
        Text(f.matched.joinToString("  ") { "✔ " + label(it) }, color = SuccessGreen, style = MaterialTheme.typography.bodyLarge)
        if (f.missing.isNotEmpty()) Text(f.missing.joinToString("  ") { "✖ " + label(it) }, color = TextMuted, style = MaterialTheme.typography.bodyLarge)
        Text(stringResource(R.string.eta_minutes, f.etaMin) + " · " + stringResource(R.string.beds_n, f.beds), style = MaterialTheme.typography.bodySmall)
        if (f.stale) StatusChip(stringResource(R.string.info_may_be_old), Tone.WAITING)
        PrimaryButton(stringResource(R.string.send_request), onClick = onSend)
    }
}
