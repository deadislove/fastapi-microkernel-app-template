from __future__ import annotations

from passlib.context import CryptContext

# bcrypt is the only scheme; deprecated="auto" auto-upgrades old hashes on verify
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)
