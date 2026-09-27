package `in`.aapatmitra.ui.components

import android.view.HapticFeedbackConstants
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.CloudOff
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import `in`.aapatmitra.R
import `in`.aapatmitra.domain.Tone
import `in`.aapatmitra.domain.Vocab
import `in`.aapatmitra.ui.theme.Dim
import `in`.aapatmitra.ui.theme.MitraBlue
import `in`.aapatmitra.ui.theme.MitraBlueTint
import `in`.aapatmitra.ui.theme.OfflineGrey
import `in`.aapatmitra.ui.theme.SosRed
import `in`.aapatmitra.ui.theme.SuccessGreen
import `in`.aapatmitra.ui.theme.TextMuted
import `in`.aapatmitra.ui.theme.TextPrimary
import `in`.aapatmitra.ui.theme.WarningAmber
import kotlinx.coroutines.launch

@Composable
fun PrimaryButton(text: String, modifier: Modifier = Modifier, enabled: Boolean = true, color: Color = MitraBlue, onClick: () -> Unit) {
    Button(onClick = onClick, enabled = enabled, modifier = modifier.fillMaxWidth().heightIn(min = Dim.button),
        shape = RoundedCornerShape(Dim.radius), colors = ButtonDefaults.buttonColors(containerColor = color)) {
        Text(text, style = MaterialTheme.typography.labelLarge, textAlign = TextAlign.Center)
    }
}

@Composable
fun SecondaryButton(text: String, modifier: Modifier = Modifier, enabled: Boolean = true, onClick: () -> Unit) {
    OutlinedButton(onClick = onClick, enabled = enabled, modifier = modifier.fillMaxWidth().heightIn(min = Dim.button),
        shape = RoundedCornerShape(Dim.radius), border = BorderStroke(2.dp, MitraBlue)) {
        Text(text, style = MaterialTheme.typography.labelLarge, color = MitraBlue, textAlign = TextAlign.Center)
    }
}

/**
 * Hold-to-send SOS (UI-UX §6.2: hold 3 s, ring fills, vibrates; release early cancels). No animation other than
 * the ring (FR-A05). Accessibility alternative: double-tap, then confirm (UI-UX §10).
 */
@Composable
fun SosButton(holdMs: Int = 3000, onConfirmed: () -> Unit) {
    val progress = remember { Animatable(0f) }
    val scope = rememberCoroutineScope()
    val view = LocalView.current
    var confirmA11y by remember { mutableStateOf(false) }
    val label = stringResource(R.string.sos_button)
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier
            .size(Dim.sos + 24.dp)
            .semantics { contentDescription = label; onClick(label) { confirmA11y = true; true } }
            .pointerInput(Unit) {
                detectTapGestures(
                    onDoubleTap = { confirmA11y = true },
                    onPress = {
                        view.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
                        val job = scope.launch {
                            progress.snapTo(0f)
                            progress.animateTo(1f, tween(holdMs, easing = LinearEasing))
                            view.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
                            onConfirmed()
                        }
                        tryAwaitRelease()
                        if (progress.value < 1f) { job.cancel(); scope.launch { progress.snapTo(0f) } }
                    })
            }) {
            Canvas(Modifier.size(Dim.sos + 24.dp)) {
                val stroke = 12.dp.toPx()
                drawArc(Color(0x33D32F2F), 0f, 360f, false, style = Stroke(stroke),
                    topLeft = Offset(stroke / 2, stroke / 2), size = Size(size.width - stroke, size.height - stroke))
                drawArc(SosRed, -90f, 360f * progress.value, false, style = Stroke(stroke),
                    topLeft = Offset(stroke / 2, stroke / 2), size = Size(size.width - stroke, size.height - stroke))
            }
            Box(Modifier.size(Dim.sos).clip(CircleShape).background(SosRed), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text("SOS", color = Color.White, fontSize = 40.sp, fontWeight = FontWeight.Bold)
                    Text(label, color = Color.White, style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center)
                }
            }
        }
        Spacer(Modifier.padding(4.dp))
        Text(stringResource(R.string.sos_hold), style = MaterialTheme.typography.bodyLarge, color = TextPrimary)
    }
    if (confirmA11y) ConfirmDialog(stringResource(R.string.sos_confirm_title), stringResource(R.string.sos_confirm_body),
        stringResource(R.string.sos_send_now), onDismiss = { confirmA11y = false }) { confirmA11y = false; onConfirmed() }
}

private fun toneColor(t: Tone) = when (t) {
    Tone.DONE -> SuccessGreen; Tone.WAITING -> WarningAmber; Tone.PROBLEM -> SosRed; Tone.OFFLINE -> OfflineGrey; Tone.INFO -> MitraBlue
}

private fun toneIcon(t: Tone): ImageVector = when (t) {
    Tone.DONE -> Icons.Filled.CheckCircle; Tone.WAITING -> Icons.Filled.Schedule; Tone.PROBLEM -> Icons.Filled.Error
    Tone.OFFLINE -> Icons.Filled.CloudOff; Tone.INFO -> Icons.Filled.Info
}

/** Every status has a word + colour + icon (UI-UX §3.1); amber chips carry dark text. */
@Composable
fun StatusChip(text: String, tone: Tone) {
    val c = toneColor(tone)
    val fg = if (tone == Tone.WAITING) TextPrimary else Color.White
    Row(Modifier.clip(RoundedCornerShape(50)).background(c).padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(toneIcon(tone), null, tint = fg, modifier = Modifier.size(18.dp))
        Spacer(Modifier.width(6.dp))
        Text(text, color = fg, style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
fun AppCard(modifier: Modifier = Modifier, color: Color = Color.White, onClick: (() -> Unit)? = null, content: @Composable () -> Unit) {
    Card(modifier = modifier.fillMaxWidth().let { if (onClick != null) it.clickable(onClick = onClick) else it },
        shape = RoundedCornerShape(Dim.radius), colors = CardDefaults.cardColors(containerColor = color),
        border = BorderStroke(1.dp, Color(0xFFDDE3EC))) {
        Column(Modifier.padding(16.dp)) { content() }
    }
}

/** Vertical lifecycle stepper with the shared wording (UI-UX §14). */
@Composable
fun CaseStepper(status: String, patientView: Boolean, facility: String?, driver: String?, etaMin: Int?) {
    val idx = Vocab.STEPS.indexOf(status).coerceAtLeast(0)
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Vocab.STEPS.forEachIndexed { i, st ->
            if (patientView && st == "closed") return@forEachIndexed
            val done = i < idx || (i == idx && Vocab.tone(status) == Tone.DONE)
            val current = i == idx && !done
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(28.dp).clip(CircleShape)
                    .background(if (done) SuccessGreen else if (current) WarningAmber else Color.Transparent)
                    .border(2.dp, if (done) SuccessGreen else if (current) WarningAmber else OfflineGrey, CircleShape),
                    contentAlignment = Alignment.Center) {
                    Text(if (done) "✔" else if (current) "⏳" else "", color = if (done) Color.White else TextPrimary, fontSize = 14.sp)
                }
                Spacer(Modifier.width(12.dp))
                var line = stringResource(Vocab.statusRes(st, patientView))
                if (st == "matched" && facility != null && i <= idx) line += ": $facility"
                if (st == "transport_assigned" && driver != null && i <= idx) line += " — $driver"
                if (current && etaMin != null && st in setOf("transport_assigned", "in_transit")) line += " · " +
                    stringResource(R.string.eta_minutes, etaMin)
                Text(line, style = MaterialTheme.typography.bodyLarge,
                    color = if (i <= idx) TextPrimary else TextMuted, fontWeight = if (current) FontWeight.SemiBold else FontWeight.Normal)
            }
        }
    }
}

/** Name + role + Call button (UI-UX §5.1 trust element). */
@Composable
fun PersonCallCard(role: String, name: String?, phone: String?, onCall: (String) -> Unit) {
    AppCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(role, style = MaterialTheme.typography.bodySmall, color = TextMuted)
                Text(name ?: "—", style = MaterialTheme.typography.titleMedium)
            }
            if (phone != null && phone.none { it == 'x' }) {
                Button(onClick = { onCall(phone) }, modifier = Modifier.heightIn(min = Dim.touch), shape = RoundedCornerShape(Dim.radius),
                    colors = ButtonDefaults.buttonColors(containerColor = SuccessGreen)) {
                    Icon(Icons.Filled.Call, null)
                    Spacer(Modifier.width(6.dp))
                    Text(stringResource(R.string.call))
                }
            }
        }
    }
}

data class Choice(val key: String, val emoji: String, val label: String)

/** 2-column picture tiles (UI-UX §3.4: pictures for choices). */
@Composable
fun PictureChoiceGrid(choices: List<Choice>, selected: String? = null, onPick: (String) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        choices.chunked(2).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                row.forEach { c ->
                    Tile(c.emoji, c.label, Modifier.weight(1f), selected = c.key == selected) { onPick(c.key) }
                }
                if (row.size == 1) Spacer(Modifier.weight(1f))
            }
        }
    }
}

@Composable
fun Tile(emoji: String, label: String, modifier: Modifier = Modifier, selected: Boolean = false, onClick: () -> Unit) {
    Column(modifier.clip(RoundedCornerShape(Dim.radius)).background(if (selected) MitraBlueTint else Color.White)
        .border(if (selected) 3.dp else 1.dp, if (selected) MitraBlue else Color(0xFFDDE3EC), RoundedCornerShape(Dim.radius))
        .clickable(onClick = onClick).heightIn(min = 112.dp).padding(12.dp),
        horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
        Text(emoji, fontSize = 36.sp)
        Text(label, style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center)
    }
}

/** Vitals input with instant normal/abnormal feedback (UI-UX §6.3 Screening). */
@Composable
fun NumberPadField(label: String, value: String, onChange: (String) -> Unit, modifier: Modifier = Modifier, decimal: Boolean = false) {
    OutlinedTextField(value = value, onValueChange = { v -> onChange(v.filter { it.isDigit() || (decimal && it == '.') }.take(5)) },
        label = { Text(label) }, singleLine = true, modifier = modifier.heightIn(min = 72.dp),
        textStyle = MaterialTheme.typography.displaySmall.copy(textAlign = TextAlign.Center),
        keyboardOptions = KeyboardOptions(keyboardType = if (decimal) KeyboardType.Decimal else KeyboardType.NumberPassword))
}

@Composable
fun Feedback(ok: Boolean?, okText: String, badText: String) {
    if (ok == null) return
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(if (ok) Icons.Filled.CheckCircle else Icons.Filled.Error, null, tint = if (ok) SuccessGreen else WarningAmber)
        Spacer(Modifier.width(8.dp))
        Text(if (ok) okText else badText, style = MaterialTheme.typography.titleMedium, color = TextPrimary)
    }
}

@Composable
fun TaskRow(icon: String, title: String, sub: String, action: String, urgent: Boolean = false, onClick: () -> Unit) {
    AppCard(color = if (urgent) Color(0xFFFDECEC) else Color.White, onClick = onClick) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(icon, fontSize = 28.sp)
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.titleMedium)
                Text(sub, style = MaterialTheme.typography.bodySmall, color = TextMuted)
            }
            TextButton(onClick = onClick, modifier = Modifier.heightIn(min = Dim.touch)) { Text(action) }
        }
    }
}

/** Offline / pending banner — never hides the SOS button (UI-UX §5, §9). */
@Composable
fun ConnectivityBanner(online: Boolean, pending: Int, onClick: () -> Unit) {
    if (online && pending == 0) return
    val text = when {
        !online && pending > 0 -> pluralStringResource(R.plurals.banner_offline_pending, pending, pending)
        !online -> stringResource(R.string.banner_offline)
        else -> pluralStringResource(R.plurals.banner_pending, pending, pending)
    }
    Row(Modifier.fillMaxWidth().background(OfflineGrey).clickable(onClick = onClick).padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.CloudOff, null, tint = Color.White)
        Spacer(Modifier.width(8.dp))
        Text(text, color = Color.White, style = MaterialTheme.typography.bodyLarge)
    }
}

@Composable
fun EmptyState(emoji: String, text: String, button: String? = null, onClick: () -> Unit = {}) {
    Column(Modifier.fillMaxWidth().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(emoji, fontSize = 56.sp)
        Text(text, style = MaterialTheme.typography.bodyLarge, textAlign = TextAlign.Center, color = TextMuted)
        if (button != null) PrimaryButton(button, onClick = onClick)
    }
}

@Composable
fun ConfirmDialog(title: String, body: String, confirm: String, danger: Boolean = false, onDismiss: () -> Unit, onConfirm: () -> Unit) {
    AlertDialog(onDismissRequest = onDismiss, title = { Text(title) }, text = { Text(body, style = MaterialTheme.typography.bodyLarge) },
        confirmButton = {
            Button(onClick = onConfirm, modifier = Modifier.heightIn(min = Dim.touch),
                colors = ButtonDefaults.buttonColors(containerColor = if (danger) SosRed else MitraBlue)) { Text(confirm) }
        },
        dismissButton = { TextButton(onClick = onDismiss, modifier = Modifier.heightIn(min = Dim.touch)) { Text(stringResource(R.string.back)) } })
}

@Composable
fun SectionTitle(text: String, color: Color = TextPrimary) {
    Text(text, style = MaterialTheme.typography.titleMedium, color = color, modifier = Modifier.padding(top = 8.dp, bottom = 4.dp))
}

@Composable
fun ChipRow(content: @Composable RowScope.() -> Unit) {
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically, content = content)
}

@Composable
fun SelectChip(text: String, selected: Boolean, onClick: () -> Unit) {
    Box(Modifier.clip(RoundedCornerShape(50)).background(if (selected) MitraBlue else Color.White)
        .border(1.dp, MitraBlue, RoundedCornerShape(50)).clickable(onClick = onClick).heightIn(min = 48.dp)
        .padding(horizontal = 16.dp, vertical = 12.dp), contentAlignment = Alignment.Center) {
        Text(text, color = if (selected) Color.White else MitraBlue, style = MaterialTheme.typography.bodyLarge)
    }
}

/** "Updated N min ago" under live data (UI-UX §9). */
@Composable
fun UpdatedAgo(ms: Long?) {
    if (ms == null) return
    val min = ((System.currentTimeMillis() - ms) / 60_000).coerceAtLeast(0)
    Text(if (min < 1) stringResource(R.string.updated_now) else stringResource(R.string.updated_min_ago, min.toInt()),
        style = MaterialTheme.typography.bodySmall, color = TextMuted)
}

@Composable
fun LaunchedPoll(key: Any?, everyMs: Long, block: suspend () -> Unit) {
    LaunchedEffect(key) {
        while (true) { block(); kotlinx.coroutines.delay(everyMs) }
    }
}
