package `in`.aapatmitra.domain

import `in`.aapatmitra.core.arr
import `in`.aapatmitra.core.dbl
import `in`.aapatmitra.core.obj
import `in`.aapatmitra.core.str
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull

/**
 * Device-side twin of the server's `routine_care/risk.py` (TRD §12A, ADR-08). Same grammar:
 *   {"any"|"all": [ {"field": <entry_vitals column>, "op": "<"|"<="|">"|">="|"=", "value": n}
 *                 | {"symptom": <code>, "present": true} ]}
 * Offline result is provisional (`highRiskSource = device`); the server re-evaluates and wins.
 */
object RiskEvaluator {
    /** API camelCase vitals → entry_vitals columns, as in risk.VITAL_COLUMNS. */
    val VITAL_COLUMNS = mapOf("bpSystolic" to "bp_systolic", "bpDiastolic" to "bp_diastolic", "pulseBpm" to "pulse_bpm",
        "respRate" to "resp_rate", "spo2Pct" to "spo2_pct", "tempC" to "temp_c", "weightKg" to "weight_kg",
        "heightCm" to "height_cm", "muacCm" to "muac_cm", "hbGDl" to "hb_g_dl", "rbsMgDl" to "rbs_mg_dl",
        "fetalHrBpm" to "fetal_hr_bpm", "gestationWeeks" to "gestation_weeks")

    data class Rule(val code: String, val cohort: String, val expression: JsonObject, val followUpDays: Int?,
                    val descriptionEn: String?, val descriptionHi: String?, val version: Int)

    fun parseRule(j: JsonObject) = Rule(j.str("code") ?: "", j.str("cohort") ?: "any", j.obj("expression") ?: JsonObject(emptyMap()),
        (j["followUpDays"] as? JsonPrimitive)?.content?.toIntOrNull(), j.str("descriptionEn"), j.str("descriptionHi"),
        (j["ruleSetVersion"] as? JsonPrimitive)?.content?.toIntOrNull() ?: 1)

    private fun term(t: JsonObject, vitals: Map<String, Double>, symptoms: Map<String, Boolean>): Boolean {
        t.str("symptom")?.let { return symptoms[it] == true }
        val v = vitals[t.str("field") ?: return false] ?: return false
        val x = t.dbl("value") ?: return false
        return when (t.str("op")) { "<" -> v < x; "<=" -> v <= x; ">" -> v > x; ">=" -> v >= x; "=" -> v == x; else -> false }
    }

    fun evaluate(expr: JsonObject, vitals: Map<String, Double>, symptoms: Map<String, Boolean>): Boolean {
        expr.arr("all")?.let { terms -> return terms.isNotEmpty() && terms.all { term(it as JsonObject, vitals, symptoms) } }
        expr.arr("any")?.let { terms -> return terms.any { term(it as JsonObject, vitals, symptoms) } }
        return false
    }

    /** @param vitalsApi camelCase vitals as entered; @param cohorts active cohorts of the patient. */
    fun fired(rules: List<Rule>, cohorts: Set<String>, vitalsApi: Map<String, Double>, symptoms: Map<String, Boolean>): List<Rule> {
        val cols = vitalsApi.mapNotNull { (k, v) -> VITAL_COLUMNS[k]?.let { it to v } }.toMap()
        val active = rules.filter { it.cohort == "any" || it.cohort in cohorts }
            .groupBy { it.code }.map { (_, rs) -> rs.maxBy { it.version } }
        return active.filter { evaluate(it.expression, cols, symptoms) }
    }

    @Suppress("unused") private fun JsonPrimitive.bool() = booleanOrNull
}
