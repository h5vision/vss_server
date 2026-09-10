"""Generate an Argon2 password hash for the Admin users registry."""

from __future__ import annotations

from getpass import getpass

from pwdlib import PasswordHash


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    return PasswordHash.recommended().hash(password)


def main() -> None:
    password = getpass("Admin password: ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(password_hash)


if __name__ == "__main__":
    main()
