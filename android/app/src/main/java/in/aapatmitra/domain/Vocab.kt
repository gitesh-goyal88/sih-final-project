package `in`.aapatmitra.domain

import `in`.aapatmitra.R

/** UI-UX §14 — one vocabulary for the case lifecycle; patients never see "Declined" in red. */
enum class Tone { DONE, WAITING, PROBLEM, OFFLINE, INFO }

object Vocab {
    fun statusRes(status: String, patientView: Boolean): Int = when (status) {
        "created" -> if (patientView) R.string.st_p_created else R.string.st_created
        "matched" -> if (patientView) R.string.st_p_matched else R.string.st_matched
        "accepted" -> if (patientView) R.string.st_p_accepted else R.string.st_accepted
        "transport_assigned" -> if (patientView) R.string.st_p_transport else R.string.st_transport
        "in_transit" -> if (patientView) R.string.st_p_in_transit else R.string.st_in_transit
        "arrived_seen" -> if (patientView) R.string.st_p_arrived else R.string.st_arrived
        "closed" -> if (patientView) R.string.st_p_closed else R.string.st_closed
        "follow_up" -> if (patientView) R.string.st_p_follow_up else R.string.st_follow_up
        "cancelled" -> R.string.st_cancelled
        else -> R.string.st_created
    }

    fun tone(status: String): Tone = when (status) {
        "accepted", "arrived_seen", "closed" -> Tone.DONE
        "follow_up" -> Tone.INFO
        "cancelled" -> Tone.OFFLINE
        else -> Tone.WAITING
    }

    /** Ordered steps for the CaseStepper. */
    val STEPS = listOf("created", "matched", "accepted", "transport_assigned", "in_transit", "arrived_seen", "closed")

    fun categoryRes(code: String?): Int = when (code) {
        "pregnancy" -> R.string.cat_pregnancy
        "newborn" -> R.string.cat_newborn
        "injury" -> R.string.cat_injury
        "breathing" -> R.string.cat_breathing
        "unconscious" -> R.string.cat_unconscious
        else -> R.string.cat_other
    }

    val CATEGORIES = listOf("pregnancy", "newborn", "injury", "breathing", "unconscious", "other")

    fun cohortRes(code: String): Int = when (code) {
        "pregnant" -> R.string.cohort_pregnant
        "newborn" -> R.string.cohort_newborn
        "chronic" -> R.string.cohort_chronic
        else -> R.string.cohort_elderly
    }

    fun taskIcon(type: String): String = when {
        type.contains("anc") || type.contains("preg") -> "🤰"
        type.contains("newborn") || type.contains("pnc") || type.contains("immun") -> "👶"
        type.contains("bp") || type.contains("chronic") -> "💊"
        else -> "📋"
    }
}
