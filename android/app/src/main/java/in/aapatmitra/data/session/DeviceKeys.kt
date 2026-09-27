package `in`.aapatmitra.data.session

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * Per-device signing key for custody hand-over QR assertions (SECURITY §8.1, SEC-CUS-01).
 * Android Keystore supports Ed25519 only from API 33; this app targets API 24+, so every device uses the
 * documented fallback — ECDSA P-256 / SHA-256, non-exportable, in the Keystore. The server accepts either
 * (SubjectPublicKeyInfo DER, `legs._verify_signature`).
 */
object DeviceKeys {
    private const val ALIAS = "am_device_sign"

    private fun ks() = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun ensure() {
        if (ks().containsAlias(ALIAS)) return
        val gen = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore")
        gen.initialize(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256).build())
        gen.generateKeyPair()
    }

    /** Base64 SPKI DER sent as `device.publicKeyEd25519` at OTP verify (field name kept from the API). */
    fun publicSpkiB64(): String {
        ensure()
        return Base64.encodeToString(ks().getCertificate(ALIAS).publicKey.encoded, Base64.NO_WRAP)
    }

    fun sign(message: ByteArray): ByteArray {
        ensure()
        val key = ks().getKey(ALIAS, null) as PrivateKey
        return Signature.getInstance("SHA256withECDSA").run { initSign(key); update(message); sign() }
    }
}
