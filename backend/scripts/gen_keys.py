"""Generate LOCAL/DEMO key material into an env file (never for staging/prod — use Vault/KMS).

    python scripts/gen_keys.py > .env.keys

Keys: KEK (wraps per-subject DEKs), HMAC blind-index key, Ed25519 JWT signing key.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    jwt_key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    print(f"AM_KEK_B64={base64.b64encode(os.urandom(32)).decode()}")
    print(f"AM_HMAC_KEY_B64={base64.b64encode(os.urandom(32)).decode()}")
    print(f"AM_JWT_PRIVATE_KEY_PEM_B64={base64.b64encode(jwt_key).decode()}")
    print(f"AM_TURN_SECRET={base64.urlsafe_b64encode(os.urandom(24)).decode()}")


if __name__ == "__main__":
    main()
