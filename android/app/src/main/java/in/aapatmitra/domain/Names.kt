package `in`.aapatmitra.domain

import java.text.Normalizer

object Names {
    /** Lower-case, accents stripped, single spaces — the FTS column for offline name search. */
    fun normalise(name: String): String = Normalizer.normalize(name, Normalizer.Form.NFKD)
        .replace(Regex("\\p{Mn}+"), "").lowercase().replace(Regex("\\s+"), " ").trim()

    /** FTS4 prefix query: every word as `word*`. */
    fun ftsQuery(q: String): String = normalise(q).split(" ").filter { it.isNotBlank() }
        .joinToString(" ") { it.replace("\"", "") + "*" }
}
