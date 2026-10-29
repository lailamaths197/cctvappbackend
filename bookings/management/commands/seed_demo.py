"""Create demo services, an admin and two technicians for local development.

    python manage.py seed_demo
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from accounts.models import User
from bookings.models import ServiceCategory

INSTALL = ServiceCategory.ServiceType.INSTALLATION
COMPLAINT = ServiceCategory.ServiceType.SERVICE_COMPLAINT

SERVICES = [
    ("New CCTV Installation", "Site survey, cameras, DVR/NVR and cabling", "4999.00", INSTALL, "📹"),
    ("DVR / NVR Setup", "Install and configure a recorder with remote viewing", "1499.00", INSTALL, "💾"),
    ("Camera Repair", "Diagnose and fix faulty cameras or wiring", "799.00", COMPLAINT, "🔧"),
    ("Annual Maintenance", "Scheduled checkups and cleaning for your system", "2999.00", COMPLAINT, "🛡️"),
]

USERS = [
    ("admin@example.com", "Admin User", "9000000001", User.Role.ADMIN, True),
    ("tech1@example.com", "Ravi Kumar", "9000000002", User.Role.TECHNICIAN, False),
    ("tech2@example.com", "Meena Devi", "9000000003", User.Role.TECHNICIAN, False),
]


class Command(BaseCommand):
    help = "Seed demo services, an admin and technicians (password: Demo@12345)"

    def handle(self, *args, **options):
        for name, desc, price, service_type, icon in SERVICES:
            ServiceCategory.objects.update_or_create(
                name=name,
                defaults={
                    "description": desc,
                    "base_price": Decimal(price),
                    "service_type": service_type,
                    "icon": icon,
                },
            )
        for email, full_name, phone, role, staff in USERS:
            first, _, last = full_name.partition(" ")
            user, created = User.objects.get_or_create(
                email=email,
                defaults=dict(
                    username=email, first_name=first, last_name=last,
                    phone_number=phone, role=role, is_staff=staff, is_superuser=staff,
                ),
            )
            if created:
                user.set_password("Demo@12345")
                user.save()
        self.stdout.write(self.style.SUCCESS("Seeded. Demo logins: admin@example.com, tech1@example.com, tech2@example.com / Demo@12345"))
