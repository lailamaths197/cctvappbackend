"""Who gets told what, when.  One function per booking state transition.

    Event                      Customer   Technician   Admin(s)
    -------------------------  ---------  -----------  --------
    booking created               yes         -           yes
    technician assigned           yes        yes          -
    job started (In Progress)     yes         -           yes
    payment received              yes        yes          yes
    job completed                 yes         -           yes

Rows are written in the same request that changes the state, so they can never
disagree with the booking. The apps poll GET /api/notifications/ (unread badge)
and their dashboards refresh on the same interval.
"""
from accounts.models import User

from .models import Notification


def _unique(users):
    return list({u.pk: u for u in users if u is not None}.values())


def notify(users, title, message, booking=None):
    rows = [
        Notification(user=u, booking=booking, title=title[:120], message=message[:300])
        for u in _unique(users)
    ]
    if rows:
        Notification.objects.bulk_create(rows)


def _admins():
    return list(User.objects.filter(role=User.Role.ADMIN, is_active=True))


def _label(booking):
    return f"#{booking.pk} ({booking.service.name})"


def booking_created(booking):
    notify([booking.customer], "Booking placed", f"Booking {_label(booking)} is pending confirmation.", booking)
    notify(
        _admins(),
        "New booking needs a technician",
        f"{booking.customer.get_full_name() or booking.customer.username} booked {_label(booking)} "
        f"for {booking.date} {booking.get_time_slot_display()}.",
        booking,
    )


def technician_assigned(booking):
    tech = booking.technician
    name = tech.get_full_name() or tech.username
    notify(
        [booking.customer],
        "Technician assigned",
        f"{name} will handle booking {_label(booking)} on {booking.date}, {booking.get_time_slot_display()}.",
        booking,
    )
    notify(
        [tech],
        "New job assigned",
        f"Job {_label(booking)} on {booking.date}, {booking.get_time_slot_display()}. Check the address in your dashboard.",
        booking,
    )


def job_started(booking):
    notify([booking.customer] + _admins(), "Work started", f"Booking {_label(booking)} is now In Progress.", booking)


def job_completed(booking):
    notify(
        [booking.customer] + _admins(),
        "Job completed",
        f"Booking {_label(booking)} has been completed.",
        booking,
    )


def payment_received(booking, payment):
    amount = f"{payment.currency} {payment.amount}"
    notify([booking.customer], "Payment successful", f"We received {amount} for booking {_label(booking)}.", booking)
    notify(
        _admins() + ([booking.technician] if booking.technician_id else []),
        "Payment received",
        f"Booking {_label(booking)} has been paid ({amount}).",
        booking,
    )
