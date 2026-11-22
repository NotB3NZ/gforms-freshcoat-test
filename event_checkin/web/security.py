import hashlib
import hmac
import secrets


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password):
    if len(password) < 12:
        raise ValueError("Choose a shared password of at least 12 characters.")
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return "scrypt$" + salt + "$" + key


def verify_password(password, encoded):
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or len(salt) != 32 or len(expected) != 128:
            raise ValueError()
        bytes.fromhex(salt + expected)
    except (ValueError, AttributeError):
        raise ValueError("Invalid WEB_PASSWORD_HASH; regenerate with the password command.") from None
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(expected, actual)
