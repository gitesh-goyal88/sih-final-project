package `in`.aapatmitra

import `in`.aapatmitra.core.AppJson
import `in`.aapatmitra.core.Ids
import `in`.aapatmitra.domain.RiskEvaluator
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Golden values produced by the backend (`app.core.ids.idem_tag`, `routine_care.risk.evaluate`) — the device
 * and the server must agree, or SMS/app merges and offline risk flags drift (TRD §7.1, ADR-08).
 */
class GoldenTest {
    @Test fun idemTagMatchesServer() {
        assertEquals("06GDQPBZ", Ids.idemTag("01a0dbd9-7f75-78b8-b68e-f9087d45887d"))
        assertEquals("00000000", Ids.idemTag("00000000-0000-0000-0000-000000000000"))
        assertEquals("ZZZZZZZZ", Ids.idemTag("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    }

    @Test fun uuid7IsVersion7() {
        val u = java.util.UUID.fromString(Ids.uuid7())
        assertEquals(7, u.version())
        assertEquals(2, u.variant())
    }

    private fun expr(s: String) = AppJson.parseToJsonElement(s).jsonObject

    @Test fun riskEvaluatorMatchesServer() {
        val bp = expr("""{"all":[{"field":"bp_systolic","op":">=","value":140}]}""")
        assertTrue(RiskEvaluator.evaluate(bp, mapOf("bp_systolic" to 150.0), emptyMap()))
        assertFalse(RiskEvaluator.evaluate(bp, mapOf("bp_systolic" to 139.0), emptyMap()))
        val any = expr("""{"any":[{"symptom":"bleeding","present":true},{"field":"spo2_pct","op":"<","value":90}]}""")
        assertTrue(RiskEvaluator.evaluate(any, emptyMap(), mapOf("bleeding" to true)))
        assertFalse(RiskEvaluator.evaluate(expr("""{"all":[]}"""), emptyMap(), emptyMap()))
    }

    @Test fun cohortFilteringAndCamelCaseMapping() {
        val rule = RiskEvaluator.parseRule(expr(
            """{"code":"preg_bp","cohort":"pregnant","ruleSetVersion":1,"expression":{"all":[{"field":"bp_systolic","op":">=","value":140}]}}"""))
        val vitals = mapOf("bpSystolic" to 150.0)
        assertEquals(1, RiskEvaluator.fired(listOf(rule), setOf("pregnant"), vitals, emptyMap()).size)
        assertEquals(0, RiskEvaluator.fired(listOf(rule), setOf("chronic"), vitals, emptyMap()).size)
    }
}
