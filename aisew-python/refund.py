from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models import Booking, Refund, utcnow

FULL_REFUND_WINDOW = timedelta(hours=24)


def cancel_booking(session, booking_id: str, paid_amount, payment_method: str, now: datetime | None = None):
    """Cancel a booking.

    A full refund (to the original payment method) is created only when the booking is cancelled
    at least 24 hours before it starts. Cancelling later cancels the booking but gives no refund.
    Returns the Refund, or None when there is no refund.
    """
    now = now or utcnow()
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise ValueError("Booking not found")
    if booking.status == "cancelled":
        raise ValueError("Booking is already cancelled")

    booking.status = "cancelled"
    refund = None
    if booking.start_date - now >= FULL_REFUND_WINDOW:
        refund = Refund(
            id=str(uuid4()),
            booking_id=booking.id,
            amount=Decimal(str(paid_amount)),
            payment_method=payment_method,
        )
        session.add(refund)
    session.commit()
    return refund
