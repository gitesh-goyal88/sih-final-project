package `in`.aapatmitra.feature.volunteer

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.media.RingtoneManager
import android.net.Uri
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import `in`.aapatmitra.R
import `in`.aapatmitra.core.obj
import `in`.aapatmitra.core.str
import `in`.aapatmitra.data.local.LegEntity
import `in`.aapatmitra.data.local.RideOfferEntity
import `in`.aapatmitra.domain.Tone
import `in`.aapatmitra.domain.Vocab
import `in`.aapatmitra.feature.me.MeScreen
import `in`.aapatmitra.feature.me.OutboxScreen
import `in`.aapatmitra.feature.onboarding.PermissionsIntro
import `in`.aapatmitra.nav.LocalVm
import `in`.aapatmitra.nav.findActivity
import `in`.aapatmitra.nav.RoleShell
import `in`.aapatmitra.nav.Tab
import `in`.aapatmitra.nav.rememberNav
import `in`.aapatmitra.ui.components.AppCard
import `in`.aapatmitra.ui.components.EmptyState
import `in`.aapatmitra.ui.components.LaunchedPoll
import `in`.aapatmitra.ui.components.PersonCallCard
import `in`.aapatmitra.ui.components.PrimaryButton
import `in`.aapatmitra.ui.components.SecondaryButton
import `in`.aapatmitra.ui.components.SectionTitle
import `in`.aapatmitra.ui.components.StatusChip
import androidx.activity.compose.rememberLauncherForActivityResult
import `in`.aapatmitra.ui.theme.Dim
import `in`.aapatmitra.ui.theme.SosRed
import `in`.aapatmitra.ui.theme.SuccessGreen
import `in`.aapatmitra.ui.theme.TextMuted
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Volunteer / driver — tabs Ride · My Points · Me (UI-UX §6.4). */
@Composable
fun VolunteerGraph() {
    val vm = LocalVm.current
    val permDone by vm.session.permOnboardedFlow.collectAsState(initial = true)
    if (!permDone) { PermissionsIntro { vm.launch { vm.session.setPermOnboarded() } }; return }
    val nav = rememberNav()
    val tabs = listOf(Tab("ride", "🚗", R.string.tab_ride), Tab("points", "⭐", R.string.tab_points), Tab("me", "👤", R.string.tab_me))
    RoleShell(nav, tabs) { m ->
        NavHost(nav, "ride", modifier = m) {
            composable("ride") { Ride(onLeg = { c, l -> nav.navigate("leg/$c/$l") }) }
            composable("leg/{c}/{l}") { b -> LegSteps(b.arguments!!.getString("c")!!, b.arguments!!.getString("l")!!) { nav.popBackStack() } }
            composable("points") { Points() }
            composable("me") { MeScreen() }
            composable("outbox") { OutboxScreen { nav.popBackStack() } }
        }
    }
}

@Composable
private fun Ride(onLeg: (String, String) -> Unit) {
    val vm = LocalVm.current
    val me by vm.me.collectAsState()
    val available by vm.rides.available.collectAsState(initial = false)
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    val offers by vm.rides.offers(now).collectAsState(initial = emptyList())
    val legs by vm.rides.myActiveLegs(me?.id ?: "").collectAsState(initial = emptyList())
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    LaunchedEffect(Unit) { while (true) { delay(1000); now = System.currentTimeMillis() } }
    // No FCM in this build: poll offers every 10 s while ON, and keep the coarse location fresh (TRD §9)
    LaunchedPoll(available, 10_000) {
        if (available || legs.isNotEmpty()) { vm.rides.pollOffers(); vm.rides.pingLocation(legs.firstOrNull()?.id) }
    }
    val incoming = offers.firstOrNull()
    if (incoming != null && legs.isEmpty()) { IncomingRequest(incoming, now) { accepted -> if (accepted != null) onLeg(incoming.caseId, accepted) }; return }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        AppCard {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(stringResource(R.string.i_am_available), style = MaterialTheme.typography.headlineSmall, modifier = Modifier.weight(1f))
                Switch(checked = available, onCheckedChange = { on -> scope.launch { error = vm.rides.setAvailable(on) } },
                    modifier = Modifier.size(width = 80.dp, height = Dim.touch),
                    colors = SwitchDefaults.colors(checkedTrackColor = SuccessGreen))
            }
            Text(stringResource(if (available) R.string.on else R.string.off), fontWeight = FontWeight.Bold)
        }
        error?.let { Text(if (it == "VOLUNTEER_NOT_VERIFIED") stringResource(R.string.not_verified) else it, color = SosRed) }
        if (legs.isNotEmpty()) {
            SectionTitle(stringResource(R.string.current_ride))
            legs.forEach { l -> LegSummary(l) { onLeg(l.caseId, l.id) } }
        } else EmptyState("🚗", stringResource(if (available) R.string.no_ride_now else R.string.switch_on_help))
    }
}

/** Full-screen, loud request with a 45 s countdown (UI-UX §6.4); coarse area only before accept (SEC-PRV-03). */
@Composable
private fun IncomingRequest(o: RideOfferEntity, now: Long, onResult: (String?) -> Unit) {
    val vm = LocalVm.current
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val left = ((o.expiresAt - now) / 1000).coerceAtLeast(0)
    DisposableEffect(o.id) {
        val tone = runCatching { RingtoneManager.getRingtone(ctx, RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)) }.getOrNull()
        tone?.play()
        scope.launch { vm.rides.opened(o) }
        onDispose { tone?.stop() }
    }
    Column(Modifier.fillMaxSize().background(Color(0xFFFDECEC)).padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text("🔴 " + stringResource(R.string.emergency_ride), style = MaterialTheme.typography.headlineSmall, color = SosRed, fontWeight = FontWeight.Bold)
        Text(stringResource(Vocab.categoryRes(o.category)), style = MaterialTheme.typography.titleMedium)
        Text(stringResource(R.string.pickup_area, listOfNotNull(o.villageName, o.landmark).joinToString(", "), o.distanceKm ?: 0),
            style = MaterialTheme.typography.bodyLarge)
        Text(stringResource(R.string.take_to, o.destinationLabel ?: "—"), style = MaterialTheme.typography.bodyLarge)
        if (!o.verified) StatusChip(stringResource(R.string.unverified_request), Tone.WAITING)
        error?.let { Text(if (it == "LEG_ALREADY_TAKEN" || it == "CASE_STATE_CONFLICT") stringResource(R.string.already_taken) else it, color = SosRed) }
        PrimaryButton("✔ " + stringResource(R.string.accept_ride), enabled = !busy && left > 0, color = SuccessGreen) {
            busy = true
            scope.launch {
                val err = vm.rides.accept(o)
                busy = false
                if (err == null) onResult(o.legId) else error = err
            }
        }
        SecondaryButton("✖ " + stringResource(R.string.cant_go)) { scope.launch { vm.rides.decline(o); onResult(null) } }
        Text(stringResource(R.string.auto_skip_in, "%d:%02d".format(left / 60, left % 60)), color = TextMuted, style = MaterialTheme.typography.titleMedium)
    }
    LaunchedEffect(left) { if (left == 0L) { vm.db.rides().setStatus(o.id, "expired"); onResult(null) } }
}

@Composable
private fun LegSummary(l: LegEntity, onOpen: () -> Unit) {
    AppCard(onClick = onOpen) {
        Text("${l.fromLabel ?: "—"} → ${l.toLabel ?: "—"}", style = MaterialTheme.typography.titleMedium)
        StatusChip(stringResource(if (l.status == "picked_up") R.string.leg_on_the_way else R.string.leg_go_pickup), Tone.WAITING)
    }
}

/**
 * Leg step list — their leg only (UI-UX §6.4): Go to pickup → Picked up → Reached hand-over point → Handed over.
 * Custody passes by the receiver's signed QR (SEC-CUS-01) or a 6-digit code for an ambulance receiver.
 */
@Composable
private fun LegSteps(caseId: String, legId: String, onBack: () -> Unit) {
    val vm = LocalVm.current
    val activity = LocalContext.current.findActivity()
    val scope = rememberCoroutineScope()
    val legs by vm.rides.leg(caseId).collectAsState(initial = emptyList())
    val view by vm.rides.contactsFlow(legId).collectAsState(initial = null)
    var reached by rememberSaveable { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var code by rememberSaveable { mutableStateOf("") }
    var qr by remember { mutableStateOf<Bitmap?>(null) }
    LaunchedPoll(legId, 10_000) { vm.rides.refreshActiveLegs(); vm.rides.pingLocation(legId) }
    val scan = rememberLauncherForActivityResult(ScanContract()) { res ->
        res.contents?.let { contents -> scope.launch { error = vm.rides.handover(caseId, legId, contents, null) } }
    }
    val leg = legs.firstOrNull { it.id == legId } ?: run { Text(stringResource(R.string.loading), Modifier.padding(16.dp)); return }
    val next = legs.firstOrNull { it.legOrder == leg.legOrder + 1 }
    val isLast = leg.nextLegId == null && next == null && leg.toLabel != null && (leg.toLat == null || view?.obj("leg")?.str("toKind") == "facility")
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("${leg.fromLabel ?: "—"} → ${leg.toLabel ?: "—"}", style = MaterialTheme.typography.headlineSmall)
        val target = if (leg.status == "accepted") leg.fromLat to leg.fromLng else leg.toLat to leg.toLng
        if (target.first != null) SecondaryButton("🗺 " + stringResource(R.string.open_in_maps)) {
            activity?.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("geo:${target.first},${target.second}?q=${target.first},${target.second}")))
        }
        view?.obj("contacts")?.let { c ->
            c.obj("family")?.let { f -> PersonCallCard(stringResource(R.string.family), f.str("firstName"), f.str("phone")) { p -> activity?.let { vm.sos.call(it, p) } } }
            c.obj("asha")?.let { a -> PersonCallCard(stringResource(R.string.asha), a.str("name"), a.str("phone")) { p -> activity?.let { vm.sos.call(it, p) } } }
        }
        Step(1, stringResource(R.string.step_go_pickup), done = leg.status != "accepted")
        Step(2, stringResource(R.string.step_picked_up), done = leg.status in setOf("picked_up", "handed_over"))
        Step(3, stringResource(R.string.step_reached), done = reached || leg.status == "handed_over")
        Step(4, stringResource(R.string.step_handed_over), done = leg.status == "handed_over")
        error?.let { Text(handoverError(it), color = SosRed, style = MaterialTheme.typography.bodyLarge) }
        when {
            leg.status == "accepted" && leg.legOrder == 1 -> PrimaryButton(stringResource(R.string.i_have_picked_up), color = SuccessGreen) {
                scope.launch { error = vm.rides.confirmPickup(caseId, legId, vm.cases) }
            }
            leg.status == "accepted" -> {
                // I receive the patient from the previous custodian: show my signed code
                Text(stringResource(R.string.show_code_to_driver), style = MaterialTheme.typography.titleMedium)
                PrimaryButton(stringResource(R.string.show_my_code)) {
                    scope.launch { qr = vm.rides.buildHandoverQr(caseId, legId)?.let(::qrBitmap); if (qr == null) error = "NO_QR" }
                }
                qr?.let { Image(it.asImageBitmap(), stringResource(R.string.show_my_code), Modifier.fillMaxWidth().padding(16.dp)) }
            }
            leg.status == "picked_up" && !reached -> PrimaryButton(stringResource(R.string.i_have_reached), color = SuccessGreen) { reached = true }
            leg.status == "picked_up" && isLast -> Text(stringResource(R.string.hand_to_hospital), style = MaterialTheme.typography.titleMedium)
            leg.status == "picked_up" -> {
                PrimaryButton("📷 " + stringResource(R.string.scan_receiver_code), color = SuccessGreen) {
                    scan.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE).setBeepEnabled(true).setOrientationLocked(true)
                        .setPrompt(""))
                }
                Text(stringResource(R.string.or_enter_code), color = TextMuted)
                OutlinedTextField(code, { code = it.filter(Char::isDigit).take(6) }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword), label = { Text(stringResource(R.string.six_digit_code)) })
                SecondaryButton(stringResource(R.string.i_have_handed_over), enabled = code.length == 6) {
                    scope.launch { error = vm.rides.handover(caseId, legId, null, code) }
                }
            }
            leg.status == "handed_over" -> Text("✔ " + stringResource(R.string.thank_you_handed), color = SuccessGreen, style = MaterialTheme.typography.titleMedium)
        }
        TextButton(onClick = onBack) { Text(stringResource(R.string.back)) }
    }
}

@Composable
private fun handoverError(code: String) = when (code) {
    "HANDOVER_CODE_INVALID" -> stringResource(R.string.code_invalid)
    "HANDOVER_LOCKED" -> stringResource(R.string.handover_locked)
    "NO_QR" -> stringResource(R.string.qr_not_ready)
    else -> code
}

@Composable
private fun Step(n: Int, text: String, done: Boolean) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(if (done) "✔" else "$n", color = if (done) SuccessGreen else TextMuted, style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.size(32.dp))
        Text(text, style = MaterialTheme.typography.titleMedium, color = if (done) SuccessGreen else Color.Unspecified)
    }
}

private fun qrBitmap(content: String): Bitmap {
    val m = QRCodeWriter().encode(content, BarcodeFormat.QR_CODE, 640, 640)
    val bmp = Bitmap.createBitmap(m.width, m.height, Bitmap.Config.RGB_565)
    for (x in 0 until m.width) for (y in 0 until m.height) bmp.setPixel(x, y, if (m[x, y]) android.graphics.Color.BLACK else android.graphics.Color.WHITE)
    return bmp
}

/** My Points: totals, verified rides, village top 5 (UI-UX §6.4). */
@Composable
private fun Points() {
    val vm = LocalVm.current
    val pts by produceState<`in`.aapatmitra.data.repo.Points?>(null) { value = vm.rides.points() }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        val p = pts ?: run { EmptyState("⭐", stringResource(R.string.points_offline)); return@Column }
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            AppCard(Modifier.weight(1f)) { Text("${p.credits}", style = MaterialTheme.typography.displaySmall); Text(stringResource(R.string.points)) }
            AppCard(Modifier.weight(1f)) { Text("${p.rides}", style = MaterialTheme.typography.displaySmall); Text(stringResource(R.string.rides_this_month)) }
        }
        SectionTitle(stringResource(R.string.my_rides))
        if (p.entries.isEmpty()) Text("—")
        p.entries.forEach { e ->
            AppCard {
                Text(listOfNotNull(e.str("caseShortCode"), e.str("villageName")).joinToString(" · "), style = MaterialTheme.typography.titleMedium)
                if (e.str("state") == "verified") StatusChip(stringResource(R.string.verified), Tone.DONE)
                else StatusChip(stringResource(R.string.pending), Tone.WAITING)
            }
        }
        SectionTitle(stringResource(R.string.village_top5))
        p.board.forEach { r ->
            Text("${r.str("rank")}. ${r.str("firstName") ?: "—"} · ${r.str("credits") ?: ""}",
                style = MaterialTheme.typography.bodyLarge, fontWeight = if (r.str("isMe") == "true") FontWeight.Bold else FontWeight.Normal)
        }
    }
}
