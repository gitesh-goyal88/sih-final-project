package `in`.aapatmitra.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// UI-UX §13.1 design tokens — red is reserved for emergency only
val MitraBlue = Color(0xFF1E4FA3)
val MitraBlueDark = Color(0xFF163B7A)
val MitraBlueTint = Color(0xFFEAF1FB)
val SosRed = Color(0xFFD32F2F)
val SuccessGreen = Color(0xFF1E8E3E)
val WarningAmber = Color(0xFFF9A825)
val AccentSaffron = Color(0xFFF7931E)
val OfflineGrey = Color(0xFF6B7280)
val Surface = Color(0xFFF4F6FA)
val Card = Color(0xFFFFFFFF)
val TextPrimary = Color(0xFF1F2937)
val TextMuted = Color(0xFF5B6475)

object Dim {
    val unit = 8.dp
    val screen = 16.dp
    val touch = 56.dp          // minimum touch target (UI-UX §3.3)
    val button = 60.dp
    val radius = 12.dp
    val sos = 180.dp           // ≥ 160 dp
    val icon = 30.dp
}

// UI-UX §3.2 — Indic line height 1.7 (system Noto Sans Devanagari is the Devanagari fallback font)
private val type = Typography(
    headlineSmall = TextStyle(fontSize = 22.sp, fontWeight = FontWeight.Bold, lineHeight = 34.sp),
    titleMedium = TextStyle(fontSize = 18.sp, fontWeight = FontWeight.SemiBold, lineHeight = 28.sp),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 27.sp),
    bodyMedium = TextStyle(fontSize = 16.sp, lineHeight = 27.sp),
    labelLarge = TextStyle(fontSize = 18.sp, fontWeight = FontWeight.SemiBold),
    bodySmall = TextStyle(fontSize = 14.sp, lineHeight = 22.sp),
    displaySmall = TextStyle(fontSize = 36.sp, fontWeight = FontWeight.Bold),
)

@Composable
fun AapatTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = lightColorScheme(
            primary = MitraBlue, onPrimary = Color.White, primaryContainer = MitraBlueTint, onPrimaryContainer = MitraBlueDark,
            secondary = AccentSaffron, background = Surface, surface = Card, onSurface = TextPrimary, onBackground = TextPrimary,
            surfaceVariant = MitraBlueTint, onSurfaceVariant = TextMuted, error = SosRed, outline = Color(0xFFCBD2DC),
        ),
        typography = type,
        content = content,
    )
}
