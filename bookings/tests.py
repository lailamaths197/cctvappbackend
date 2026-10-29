import io
import shutil
import tempfile
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from razorpay.errors import SignatureVerificationError
from rest_framework.test import APITestCase

from accounts.models import User

from .models import Booking, BookingImage, Notification, Payment, ServiceCategory

TMP_MEDIA = tempfile.mkdtemp()


def make_image(fmt="PNG", size=(40, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format=fmt)
    ext = "jpg" if fmt == "JPEG" else fmt.lower()
    return SimpleUploadedFile(f"photo.{ext}", buf.getvalue(), content_type=f"image/{ext}")


def make_user(email, role, phone):
    return User.objects.create_user(
        username=email, email=email, password="Str0ng!Pass", phone_number=phone,
        first_name=email.split("@")[0], role=role,
    )


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class BaseCase(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.install = ServiceCategory.objects.create(
            name="Install", base_price="4999.00", service_type="installation"
        )
        self.repair = ServiceCategory.objects.create(
            name="Repair", base_price="799.00", service_type="service_complaint"
        )
        self.customer = make_user("cust@x.com", User.Role.CUSTOMER, "9111111111")
        self.customer2 = make_user("cust2@x.com", User.Role.CUSTOMER, "9111111112")
        self.tech1 = make_user("t1@x.com", User.Role.TECHNICIAN, "9222222221")
        self.tech2 = make_user("t2@x.com", User.Role.TECHNICIAN, "9222222222")
        self.admin = make_user("admin@x.com", User.Role.ADMIN, "9333333331")
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()

    def payload(self, service=None, **over):
        data = {
            "service": (service or self.install).pk, "date": self.tomorrow, "time_slot": "09:00-11:00",
            "issue_description": "Camera 2 shows no signal at night",
            "house_no": "12A", "street": "MG Road", "landmark": "Near temple",
            "city": "Trichy", "state": "Tamil Nadu", "pincode": "620001",
        }
        data.update(over)
        return data

    def book(self, user, n_images=0, service=None, **over):
        self.client.force_authenticate(user)
        data = self.payload(service, **over)
        if n_images:
            data["images"] = [make_image() for _ in range(n_images)]
        return self.client.post("/api/bookings/", data, format="multipart")

    def assigned_booking(self, tech=None, service=None, customer=None, **over):
        """Create a booking and have the admin assign it. Returns its id."""
        booking_id = self.book(customer or self.customer, service=service, **over).data["id"]
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            f"/api/bookings/{booking_id}/assign/", {"technician_id": (tech or self.tech1).pk}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        return booking_id

    def set_status(self, user, booking_id, status, n_images=0):
        self.client.force_authenticate(user)
        data = {"status": status}
        if n_images:
            data["images"] = [make_image() for _ in range(n_images)]
        return self.client.patch(f"/api/bookings/{booking_id}/status/", data, format="multipart")


# --- Accounts ---------------------------------------------------------------------


class AuthTests(BaseCase):
    def test_register_login_and_role_cannot_be_self_assigned(self):
        r = self.client.post("/api/auth/register/", {
            "full_name": "Asha Rao", "email": "Asha@Example.com", "phone_number": "9876543210",
            "password": "Str0ng!Pass1", "confirm_password": "Str0ng!Pass1", "role": "admin",
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["role"], "customer")
        r = self.client.post("/api/auth/login/", {"email": "asha@example.com", "password": "Str0ng!Pass1"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)
        self.assertEqual(r.data["user"]["role"], "customer")

    def test_password_mismatch_and_bad_phone_rejected(self):
        r = self.client.post("/api/auth/register/", {
            "full_name": "A B", "email": "a@b.com", "phone_number": "12345",
            "password": "Str0ng!Pass1", "confirm_password": "different",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("phone_number", r.data)


class AdminUserManagementTests(BaseCase):
    def test_admin_creates_technician_with_generated_credentials(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/auth/technicians/", {
            "full_name": "Kiran Raj", "email": "kiran@x.com", "phone_number": "9444444444",
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["role"], "technician")
        temp = r.data["temporary_password"]
        self.assertTrue(temp and len(temp) >= 8)
        # the generated credentials really work
        self.client.force_authenticate(None)
        login = self.client.post("/api/auth/login/", {"email": "kiran@x.com", "password": temp})
        self.assertEqual(login.status_code, 200, login.data)
        self.assertEqual(login.data["user"]["role"], "technician")

    def test_admin_can_choose_the_password(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/auth/technicians/", {
            "full_name": "Kiran Raj", "email": "kiran@x.com", "phone_number": "9444444444",
            "password": "Chosen!Pass77",
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(r.data["temporary_password"])
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.post("/api/auth/login/", {"email": "kiran@x.com", "password": "Chosen!Pass77"}).status_code, 200
        )

    def test_only_admin_can_create_technicians(self):
        body = {"full_name": "Kiran Raj", "email": "kiran@x.com", "phone_number": "9444444444"}
        for user in (self.customer, self.tech1):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.post("/api/auth/technicians/", body).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post("/api/auth/technicians/", body).status_code, 401)

    def test_duplicate_email_rejected(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/auth/technicians/", {
            "full_name": "Dup", "email": "T1@X.com", "phone_number": "9555555555",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("email", r.data)

    def test_admin_lists_and_filters_all_users(self):
        self.assertEqual(self.book(self.customer).status_code, 201)
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/auth/admin/users/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 5)
        r = self.client.get("/api/auth/admin/users/?role=customer")
        self.assertEqual({u["email"] for u in r.data}, {"cust@x.com", "cust2@x.com"})
        counts = {u["email"]: u["job_count"] for u in r.data}
        self.assertEqual(counts["cust@x.com"], 1)
        self.assertEqual(len(self.client.get("/api/auth/admin/users/?search=9222222222").data), 1)

    def test_non_admin_cannot_list_users(self):
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.client.get("/api/auth/admin/users/").status_code, 403)

    def test_admin_can_deactivate_but_not_self(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f"/api/auth/admin/users/{self.tech2.pk}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.tech2.refresh_from_db()
        self.assertFalse(self.tech2.is_active)
        r = self.client.patch(f"/api/auth/admin/users/{self.admin.pk}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 400)


# --- Service catalog -----------------------------------------------------------------


class ServiceCatalogTests(BaseCase):
    def test_admin_adds_and_edits_services(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/admin/services/", {
            "name": "Night Vision Upgrade", "description": "IR cameras", "base_price": "2500.00",
            "service_type": "installation", "icon": "🌙",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        sid = r.data["id"]
        r = self.client.patch(f"/api/admin/services/{sid}/", {"base_price": "2750.00", "is_active": False}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["base_price"], "2750.00")
        self.assertFalse(r.data["is_active"])

    def test_invalid_type_and_price_rejected(self):
        self.client.force_authenticate(self.admin)
        base = {"name": "Thing", "description": "", "base_price": "100.00", "service_type": "installation"}
        self.assertEqual(self.client.post("/api/admin/services/", {**base, "service_type": "other"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/admin/services/", {**base, "base_price": "0"}, format="json").status_code, 400)

    def test_customers_see_only_active_services_and_cannot_edit(self):
        self.repair.is_active = False
        self.repair.save()
        self.client.force_authenticate(self.customer)
        r = self.client.get("/api/services/")
        self.assertEqual([s["name"] for s in r.data], ["Install"])
        self.assertEqual(r.data[0]["service_type"], "installation")
        self.assertEqual(self.client.get("/api/admin/services/").status_code, 403)
        self.assertEqual(self.client.post("/api/admin/services/", {"name": "x"}, format="json").status_code, 403)

    def test_inactive_service_cannot_be_booked(self):
        self.repair.is_active = False
        self.repair.save()
        self.assertEqual(self.book(self.customer, service=self.repair).status_code, 400)


# --- Booking creation --------------------------------------------------------------------


class BookingCreateTests(BaseCase):
    def test_multipart_booking_with_multiple_images(self):
        r = self.book(self.customer, n_images=3)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "pending")
        self.assertEqual(r.data["amount"], "4999.00")
        self.assertFalse(r.data["is_paid"])
        self.assertEqual(len(r.data["images"]), 3)
        self.assertTrue(all(i["kind"] == "complaint" and i["image"].endswith(".jpg") for i in r.data["images"]))
        self.assertEqual(r.data["address"]["pincode"], "620001")
        self.assertIsNone(r.data["payment"])  # no Razorpay keys in tests
        self.assertIn("payment_error", r.data)

    def test_booking_without_images_is_allowed(self):
        r = self.book(self.customer)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["images"], [])

    def test_description_optional_for_installation(self):
        r = self.book(self.customer, issue_description="")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["issue_description"], "")

    def test_description_required_for_service_complaint(self):
        r = self.book(self.customer, service=self.repair, issue_description="")
        self.assertEqual(r.status_code, 400)
        self.assertIn("issue_description", r.data)
        r = self.book(self.customer, service=self.repair, issue_description="too short")
        self.assertEqual(r.status_code, 400)
        r = self.book(self.customer, service=self.repair, issue_description="Camera 2 has no picture at night")
        self.assertEqual(r.status_code, 201, r.data)

    def test_too_many_images_rejected(self):
        r = self.book(self.customer, n_images=7)
        self.assertEqual(r.status_code, 400)
        self.assertIn("images", r.data)
        self.assertEqual(Booking.objects.count(), 0)

    def test_client_cannot_set_price(self):
        r = self.book(self.customer, amount="1.00")
        self.assertEqual(r.data["amount"], "4999.00")

    def test_past_date_and_fake_image_rejected(self):
        r = self.book(self.customer, date=(date.today() - timedelta(days=1)).isoformat())
        self.assertEqual(r.status_code, 400)
        self.client.force_authenticate(self.customer)
        data = self.payload()
        data["images"] = [make_image(), SimpleUploadedFile("evil.jpg", b"not an image", content_type="image/jpeg")]
        r = self.client.post("/api/bookings/", data, format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertIn("images", r.data)
        self.assertEqual(Booking.objects.count(), 0)

    def test_only_customers_can_book(self):
        self.assertEqual(self.book(self.tech1).status_code, 403)

    def test_slot_capacity_equals_technician_count(self):
        self.tech2.is_active = False
        self.tech2.save()  # only 1 active technician -> capacity 1
        self.assertEqual(self.book(self.customer).status_code, 201)
        r = self.book(self.customer2)
        self.assertEqual(r.status_code, 400)
        self.assertIn("time_slot", r.data)
        self.assertEqual(self.book(self.customer2, time_slot="11:00-13:00").status_code, 201)

    def test_customers_only_see_own_bookings(self):
        self.book(self.customer)
        self.client.force_authenticate(self.customer2)
        self.assertEqual(len(self.client.get("/api/bookings/").data), 0)


# --- Assignment / double booking ---------------------------------------------------------


class AssignmentTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.a = self.book(self.customer).data["id"]
        self.b = self.book(self.customer2).data["id"]  # same slot, 2 technicians -> allowed

    def assign(self, booking_id, tech, as_user=None):
        self.client.force_authenticate(as_user or self.admin)
        return self.client.post(f"/api/bookings/{booking_id}/assign/", {"technician_id": tech.pk}, format="json")

    def test_technician_cannot_be_double_booked(self):
        self.assertEqual(self.assign(self.a, self.tech1).status_code, 200)
        r = self.assign(self.b, self.tech1)
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(self.assign(self.b, self.tech2).status_code, 200)

    def test_db_constraint_blocks_duplicate(self):
        from django.db import IntegrityError, transaction
        self.assign(self.a, self.tech1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.filter(pk=self.b).update(technician=self.tech1)

    def test_non_admin_cannot_assign(self):
        self.assertEqual(self.assign(self.a, self.tech1, as_user=self.customer).status_code, 403)

    def test_cannot_assign_twice(self):
        self.assign(self.a, self.tech1)
        self.assertEqual(self.assign(self.a, self.tech2).status_code, 400)

    def test_admin_stats(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/admin/stats/")
        self.assertEqual(r.data["total_bookings"], 2)
        self.assertEqual(r.data["unassigned_pending"], 2)
        self.assertEqual(r.data["customers"], 2)


# --- Technician workflow: evidence images + payment gate -----------------------------------


class WorkflowTests(BaseCase):
    def test_cannot_start_without_before_images(self):
        bid = self.assigned_booking()
        r = self.set_status(self.tech1, bid, "in_progress")
        self.assertEqual(r.status_code, 400)
        self.assertIn("images", r.data)
        self.assertEqual(Booking.objects.get(pk=bid).status, "confirmed")

    def test_start_with_before_images(self):
        bid = self.assigned_booking()
        r = self.set_status(self.tech1, bid, "in_progress", n_images=2)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "in_progress")
        self.assertEqual(BookingImage.objects.filter(booking_id=bid, kind="before").count(), 2)

    def test_cannot_complete_when_unpaid_even_with_images(self):
        bid = self.assigned_booking()
        self.set_status(self.tech1, bid, "in_progress", n_images=1)
        r = self.set_status(self.tech1, bid, "completed", n_images=2)
        self.assertEqual(r.status_code, 402, r.data)
        booking = Booking.objects.get(pk=bid)
        self.assertEqual(booking.status, "in_progress")
        self.assertFalse(BookingImage.objects.filter(booking=booking, kind__in=["installation", "after"]).exists())

    def test_installation_completion_needs_installation_images(self):
        bid = self.assigned_booking(service=self.install)
        self.set_status(self.tech1, bid, "in_progress", n_images=1)
        Booking.objects.filter(pk=bid).update(is_paid=True)
        r = self.set_status(self.tech1, bid, "completed")  # no images
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Booking.objects.get(pk=bid).status, "in_progress")
        r = self.set_status(self.tech1, bid, "completed", n_images=2)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "completed")
        self.assertEqual(BookingImage.objects.filter(booking_id=bid, kind="installation").count(), 2)
        self.assertFalse(BookingImage.objects.filter(booking_id=bid, kind="after").exists())

    def test_service_complaint_completion_needs_after_images(self):
        bid = self.assigned_booking(service=self.repair)
        self.set_status(self.tech1, bid, "in_progress", n_images=1)
        Booking.objects.filter(pk=bid).update(is_paid=True)
        self.assertEqual(self.set_status(self.tech1, bid, "completed").status_code, 400)
        r = self.set_status(self.tech1, bid, "completed", n_images=1)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(BookingImage.objects.filter(booking_id=bid, kind="after").count(), 1)
        self.assertFalse(BookingImage.objects.filter(booking_id=bid, kind="installation").exists())

    def test_cannot_skip_steps(self):
        bid = self.assigned_booking()
        Booking.objects.filter(pk=bid).update(is_paid=True)
        r = self.set_status(self.tech1, bid, "completed", n_images=1)  # still 'confirmed'
        self.assertEqual(r.status_code, 400)

    def test_other_technician_and_customer_cannot_update(self):
        bid = self.assigned_booking()
        self.assertEqual(self.set_status(self.tech2, bid, "in_progress", n_images=1).status_code, 404)
        self.assertEqual(self.set_status(self.customer, bid, "in_progress", n_images=1).status_code, 403)

    def test_technician_sees_only_own_jobs_with_customer_images(self):
        bid = self.assigned_booking()
        self.client.force_authenticate(self.tech1)
        jobs = self.client.get("/api/bookings/").data
        self.assertEqual([j["id"] for j in jobs], [bid])
        self.assertEqual(jobs[0]["customer"]["phone_number"], "9111111111")
        self.client.force_authenticate(self.tech2)
        self.assertEqual(len(self.client.get("/api/bookings/").data), 0)

    def test_notifications_follow_state_changes(self):
        bid = self.assigned_booking()
        self.assertTrue(Notification.objects.filter(user=self.admin, title__icontains="New booking").exists())
        self.assertTrue(Notification.objects.filter(user=self.customer, title="Technician assigned").exists())
        self.assertTrue(Notification.objects.filter(user=self.tech1, title="New job assigned").exists())
        self.set_status(self.tech1, bid, "in_progress", n_images=1)
        self.assertTrue(Notification.objects.filter(user=self.customer, title="Work started").exists())
        Booking.objects.filter(pk=bid).update(is_paid=True)
        self.set_status(self.tech1, bid, "completed", n_images=1)
        self.assertTrue(Notification.objects.filter(user=self.customer, title="Job completed").exists())
        self.assertTrue(Notification.objects.filter(user=self.admin, title="Job completed").exists())

    def test_notification_api_list_and_mark_read(self):
        self.assigned_booking()
        self.client.force_authenticate(self.customer)
        r = self.client.get("/api/notifications/")
        self.assertGreaterEqual(r.data["unread"], 2)
        self.assertEqual(self.client.post("/api/notifications/read/").status_code, 200)
        self.assertEqual(self.client.get("/api/notifications/").data["unread"], 0)
        # another user's notifications are untouched
        self.client.force_authenticate(self.tech1)
        self.assertEqual(self.client.get("/api/notifications/").data["unread"], 1)


# --- Payments ---------------------------------------------------------------------------------


class PaymentTests(BaseCase):
    def fake_client(self, verify_ok=True):
        c = MagicMock()
        c.order.create.return_value = {"id": "order_TEST123", "currency": "INR"}
        if not verify_ok:
            c.utility.verify_payment_signature.side_effect = SignatureVerificationError("bad")
        return c

    @override_settings(RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="secret")
    def test_order_created_payment_verified_and_gate_opens(self):
        fake = self.fake_client()
        with patch("bookings.razorpay_service.razorpay.Client", return_value=fake):
            r = self.book(self.customer)
            self.assertEqual(r.status_code, 201, r.data)
            bid = r.data["id"]
            self.assertEqual(r.data["payment"]["order_id"], "order_TEST123")
            self.assertEqual(r.data["payment"]["amount"], 499900)  # paise
            fake.order.create.assert_called_once()

            # assign + start the job
            self.client.force_authenticate(self.admin)
            self.client.post(f"/api/bookings/{bid}/assign/", {"technician_id": self.tech1.pk}, format="json")
            self.set_status(self.tech1, bid, "in_progress", n_images=1)
            # unpaid -> blocked
            self.assertEqual(self.set_status(self.tech1, bid, "completed", n_images=1).status_code, 402)

            # customer pays
            self.client.force_authenticate(self.customer)
            r2 = self.client.post("/api/payments/verify/", {
                "razorpay_order_id": "order_TEST123", "razorpay_payment_id": "pay_1", "razorpay_signature": "sig",
            }, format="json")
            self.assertEqual(r2.status_code, 200, r2.data)
            self.assertTrue(r2.data["receipt_no"].startswith("RCPT-"))
            self.assertEqual(r2.data["customer_name"], "cust")
            self.assertTrue(Booking.objects.get(pk=bid).is_paid)
            history = self.client.get("/api/payments/").data
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["address"], "12A, MG Road, Near temple, Trichy, Tamil Nadu, 620001")

            # payment notifications reached customer, admin and technician
            for user in (self.customer, self.admin, self.tech1):
                self.assertTrue(Notification.objects.filter(user=user, title__startswith="Payment").exists(), user)

            # gate is now open
            r3 = self.set_status(self.tech1, bid, "completed", n_images=1)
            self.assertEqual(r3.status_code, 200, r3.data)
            self.assertEqual(r3.data["status"], "completed")
            self.assertTrue(r3.data["is_paid"])

    @override_settings(RAZORPAY_KEY_ID="rzp_test_x", RAZORPAY_KEY_SECRET="secret")
    def test_bad_signature_rejected(self):
        with patch("bookings.razorpay_service.razorpay.Client", return_value=self.fake_client(verify_ok=False)):
            self.book(self.customer)
            r = self.client.post("/api/payments/verify/", {
                "razorpay_order_id": "order_TEST123", "razorpay_payment_id": "pay_1", "razorpay_signature": "forged",
            }, format="json")
            self.assertEqual(r.status_code, 400)
            self.assertFalse(Booking.objects.get().is_paid)
            self.assertEqual(Payment.objects.get().status, "failed")
