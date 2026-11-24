import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from app import email_service
from app.models import Customer, PasswordResetLink, utcnow

LINK_LIFETIME = timedelta(hours=1)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return f"{salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    salt_hex, _, digest_hex = stored.partition("$")
    return hash_password(password, bytes.fromhex(salt_hex)) == f"{salt_hex}${digest_hex}"


def request_reset(session, email: str, now: datetime | None = None) -> PasswordResetLink | None:
    """Email the customer a reset link that is valid for 1 hour and works once.

    Any older unused links of that customer stop working. For an unknown email nothing is sent and
    None is returned, so the answer never reveals whether an email is registered.
    """
    now = now or utcnow()
    customer = session.query(Customer).filter(Customer.email == email).first()
    if customer is None:
        return None

    for old in session.query(PasswordResetLink).filter_by(customer_id=customer.id, used=False):
        old.used = True

    link = PasswordResetLink(
        id=str(uuid4()),
        customer_id=customer.id,
        link=secrets.token_urlsafe(32),
        expires_at=now + LINK_LIFETIME,
        used=False,
    )
    session.add(link)
    session.commit()
    email_service.send_email(customer.email, link.link)
    return link


def use_reset_link(session, token: str, new_password: str, now: datetime | None = None) -> Customer:
    """Set a new password with a reset link. Raises ValueError with the message to show the customer."""
    now = now or utcnow()
    link = session.query(PasswordResetLink).filter(PasswordResetLink.link == token).first()
    if link is None:
        raise ValueError("This link is not valid")
    if link.used:
        raise ValueError("This link is no longer valid")
    if now >= link.expires_at:
        raise ValueError("This link has expired")
    if not new_password:
        raise ValueError("The new password must not be empty")

    customer = session.get(Customer, link.customer_id)
    customer.password_hash = hash_password(new_password)
    link.used = True
    session.commit()
    return customer
