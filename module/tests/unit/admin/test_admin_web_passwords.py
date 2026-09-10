from pwdlib import PasswordHash

from admin_web.passwords import hash_password


def test_hash_password_generates_verifiable_argon2_hash() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert password_hash.startswith("$argon2")
    assert PasswordHash.recommended().verify(
        "correct horse battery staple",
        password_hash,
    )
