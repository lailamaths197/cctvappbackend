import logging

from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404
from razorpay.errors import SignatureVerificationError
from rest_framework import generics, status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from decimal import Decimal
from accounts.models import User
from accounts.permissions import IsAdminRole, IsCustomer, IsTechnician
from rest_framework.permissions import IsAuthenticated

from . import notifications
from .models import Review
from .serializers import ReviewSerializer
from .models import Address, Booking, BookingImage, Notification, Payment, ServiceCategory,Review
from .razorpay_service import (
    RazorpayNotConfigured,
    checkout_payload,
    create_order_for_booking,
    verify_and_mark_paid,
)
from .serializers import ReviewSerializer
from .serializers import (
    AdminServiceSerializer,
    AssignTechnicianSerializer,
    BookingCreateSerializer,
    BookingSerializer,
    NotificationSerializer,
    PaymentSerializer,
    ServiceCategorySerializer,
    UpdateStatusSerializer,
    VerifyPaymentSerializer,
)
from .utils import send_whatsapp_booking_update
from .utils import create_masked_call_session 
logger = logging.getLogger(__name__)


class PaymentRequired(APIException):
    """HTTP 402 - used to block completion of an unpaid job."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = (
        "The customer has not paid for this booking yet. "
        "A job cannot be marked Completed until payment is received."
    )
    default_code = "payment_required"


# --- Slot conflict helpers -----------------------------------------------------


def technician_has_conflict(technician, date, time_slot, exclude_booking_id=None):
    """True if the technician already holds an active booking in this slot."""
    qs = Booking.objects.filter(technician=technician, date=date, time_slot=time_slot).exclude(
        status=Booking.Status.CANCELLED
    )
    if exclude_booking_id:
        qs = qs.exclude(pk=exclude_booking_id)
    return qs.exists()


def assert_slot_available(date, time_slot):
    """Capacity check at booking time.

    A slot can hold at most as many active bookings as there are active
    technicians, so every accepted booking can later be assigned to a
    different technician without a clash. (If no technicians exist yet - e.g.
    a fresh dev database - bookings are accepted.)

    Must be called inside transaction.atomic(); the select_for_update() on the
    technician rows serialises concurrent bookings for the same slot on
    PostgreSQL (SQLite ignores row locks but serialises writes anyway).
    """
    technicians = list(
        User.objects.select_for_update().filter(role=User.Role.TECHNICIAN, is_active=True)
    )
    if not technicians:
        return
    taken = (
        Booking.objects.filter(date=date, time_slot=time_slot)
        .exclude(status=Booking.Status.CANCELLED)
        .count()
    )
    if taken >= len(technicians):
        raise ValidationError(
            {"time_slot": "This time slot is fully booked. Please choose another slot or date."}
        )


# --- Services -------------------------------------------------------------------


class ServiceCategoryListView(generics.ListAPIView):
    """Customer-facing catalog: active services only."""

    serializer_class = ServiceCategorySerializer
    queryset = ServiceCategory.objects.filter(is_active=True)


class AdminServiceListCreateView(generics.ListCreateAPIView):
    """Admin: every service (including inactive) + add a new one."""

    permission_classes = [IsAdminRole]
    serializer_class = AdminServiceSerializer
    queryset = ServiceCategory.objects.all()


class AdminServiceDetailView(generics.RetrieveUpdateAPIView):
    """Admin: edit name / description / price / type / active flag.

    Services are deactivated rather than deleted, so old bookings keep their service.
    """

    permission_classes = [IsAdminRole]
    serializer_class = AdminServiceSerializer
    queryset = ServiceCategory.objects.all()

# --- Booking---------------------------------------------------------------

def _bookings_for(user):
    qs = Booking.objects.select_related("service", "address", "customer", "technician").prefetch_related("images")
    if user.role == User.Role.ADMIN:
        return qs
    if user.role == User.Role.TECHNICIAN:
        return qs.filter(technician=user)
    return qs.filter(customer=user)


class BookingListCreateView(APIView):
    """GET: bookings visible to the caller's role. POST: customer creates one.

    POST expects multipart/form-data so the text fields and the array of photos
    (the repeated `images` key) arrive in a single request layout loop.
    """
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        qs = _bookings_for(request.user)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response(BookingSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        if request.user.role != User.Role.CUSTOMER:
            raise PermissionDenied("Only customers can create bookings.")

        serializer = BookingCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            assert_slot_available(data["date"], data["time_slot"])

            # 🛰️ SAVES MAP METRICS AND GEOLOCATION DATA SAFELY INTO THE INSTANCE RELATION
            address = Address.objects.create(
                user=request.user,
                house_no=data["house_no"],
                street=data["street"],
                landmark=data.get("landmark", ""),
                city=data["city"],
                state=data["state"],
                pincode=data["pincode"],
                address_mode=data.get("address_mode", "gps"),
                latitude=data.get("latitude"),
                longitude=data.get("longitude")
            )
            
            service = data["service"]
            booking = Booking.objects.create(
                customer=request.user,
                service=service,
                address=address,
                date=data["date"],
                time_slot=data["time_slot"],
                issue_description=data["issue_description"],
                amount=service.base_price,  # price comes from the server, never the client
            )
            for photo in data["images"]:
                BookingImage.objects.create(
                    booking=booking, kind=BookingImage.Kind.COMPLAINT, image=photo, uploaded_by=request.user
                )

        # 🔔 Trigger internal application systems
        notifications.booking_created(booking)

        # 🟢 TWILIO INTEGRATION: Fires off an automated real-time status message to the user's WhatsApp channel
        send_whatsapp_booking_update(
            customer_phone=booking.customer.phone_number,  # Maps to the customer phone number field
            booking_id=booking.id,
            status_label="Pending Configuration Allocation"
        )

        # The booking is saved even if payment setup fails; the customer can retry via POST /bookings/<id>/pay/.
        booking = _bookings_for(request.user).get(pk=booking.pk)
        body = dict(BookingSerializer(booking, context={"request": request}).data)
        try:
            payment = create_order_for_booking(booking)
            body["payment"] = checkout_payload(payment)
        except RazorpayNotConfigured as exc:
            body["payment"] = None
            body["payment_error"] = str(exc)
        except Exception:  # network / Razorpay API error
            logger.exception("Razorpay order creation failed for booking %s", booking.pk)
            body["payment"] = None
            body["payment_error"] = "Could not start payment. You can retry from My Bookings."
            
        return Response(body, status=status.HTTP_201_CREATED)


class BookingDetailView(generics.RetrieveAPIView):
    serializer_class = BookingSerializer

    def get_queryset(self):
        return _bookings_for(self.request.user)

class BookingSecureCallView(APIView):
    """
    POST /api/bookings/<id>/secure-call/
    Safely handles proxy setup or falls back to a development simulation testing number.
    """
    def post(self, request, pk):
        # 🟢 DEVELOPMENT TRIAL FALLBACK RUNTIME LOGIC:
        # Instead of crashing because of the trial account limits, return a mock routing number
        # change this to your personal phone number to test the outbound call button!
        SIMULATED_PROXY_NUMBER = "+1234567890" 
        
        try:
            # If you later upgrade or mock it, it will attempt normal operation
            if "mock" in settings.TWILIO_PROXY_SERVICE_SID:
                return Response({"proxy_number": SIMULATED_PROXY_NUMBER}, status=200)
            
            # Your original Twilio session creation code logic goes here...
            # session = create_masked_call_session(...)
            # return Response({"proxy_number": session.proxy_number})
        except Exception:
            # Fallback to dev simulation if Twilio API errors out
            return Response({"proxy_number": SIMULATED_PROXY_NUMBER}, status=200)

class BookingSubmitReviewView(APIView):
    """POST: Submits a customer service rating & review for a completed booking."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        # Grabs the explicit booking asset safely using your access scope query rules
        booking = get_object_or_404(_bookings_for(request.user), pk=pk)
        
        data = request.data.copy()
        data['booking'] = booking.id  # Automatically binds the correct relationship id
        
        serializer = ReviewSerializer(data=data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        return Response(serializer.data, status=status.HTTP_201_CREATED)

class BookingAvailableSlotsView(APIView):
    """
    GET /api/bookings/slots/?service=4&date=2026-10-30
    Dynamically maps slot objects to satisfy explicit frontend filters.
    """
    def get(self, request):
        target_date = request.query_params.get("date")
        if not target_date:
            return Response({"error": "Date parameter is required."}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Provide all variations and structural keys expected by the React Native application
        ALL_SLOTS = []
        for code, label in Booking.TimeSlot.choices:
            ALL_SLOTS.append({
                "id": code,
                "time_slot": code,
                "slot": code,
                "label": label,
                "time_slot_label": label,
                
                # 🌟 FIX: Inject the exact key field the frontend's filter loop checks for 🌟
                "available_technicians": 5, 
            })

        # 2. Check total capacity of active technicians
        active_techs_count = User.objects.filter(role=User.Role.TECHNICIAN, is_active=True).count()
        
        # If no technicians exist yet in your dev DB environment, return all slots as open
        if active_techs_count == 0:
            # 🌟 FIX: Wrap the array inside a dictionary object containing the "slots" key
            return Response({"slots": ALL_SLOTS}, status=status.HTTP_200_OK)

        # 3. Calculate active bookings per slot for the chosen date
        bookings_count_by_slot = (
            Booking.objects.filter(date=target_date)
            .exclude(status=Booking.Status.CANCELLED)
            .values("time_slot")
            .annotate(taken=Count("id"))
        )
        taken_slots_map = {item["time_slot"]: item["taken"] for item in bookings_count_by_slot}

        # 4. Filter slots based on technician availability capacity parameters
        available_slots = []
        for slot in ALL_SLOTS:
            slots_taken = taken_slots_map.get(slot["id"], 0)
            remaining_capacity = active_techs_count - slots_taken
            
            if remaining_capacity > 0:
                # 🌟 FIX: Pass down the live dynamic capacity number
                slot["available_technicians"] = remaining_capacity
                available_slots.append(slot)

        # 🌟 FIX: Wrap response payload structure to match frontend expectations perfectly
        return Response({"slots": available_slots}, status=status.HTTP_200_OK)

# --- Technician workflow ------------------------------------------------------------

ALLOWED_TRANSITIONS = {
    Booking.Status.CONFIRMED: {Booking.Status.IN_PROGRESS},
    Booking.Status.IN_PROGRESS: {Booking.Status.COMPLETED},
}


def _evidence_kind_for_completion(booking):
    if booking.service.service_type == ServiceCategory.ServiceType.INSTALLATION:
        return BookingImage.Kind.INSTALLATION, "installation"
    return BookingImage.Kind.AFTER, "after"


class TechnicianAddExtraServiceView(APIView):
    """
    POST /api/bookings/<id>/add-extra/
    Technician appends an out-of-scope/extra service to an active job from the field.
    """
    permission_classes = [IsTechnician]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, pk):
        booking = get_object_or_404(Booking, pk=pk, technician=request.user)

        if booking.status == Booking.Status.COMPLETED:
            return Response({"detail": "Cannot amend a historically completed service ticket."}, status=status.HTTP_400_BAD_REQUEST)

        # 🟢 FIX 1: Flexible Multi-Part File Array Handling
        # Look for 'extra_before_images' or fall back to 'images' key variations
        uploaded_photos = request.FILES.getlist('extra_before_images') or request.FILES.getlist('images')
        if not uploaded_photos:
            return Response(
                {"extra_before_images": "You must capture or select at least one 'before' photo proof."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 🟢 FIX 2: Flexible Data Keys Fallbacks
        extra_title = request.data.get('extra_title', request.data.get('title', request.data.get('extra_service_title', ''))).strip()
        extra_desc = request.data.get('extra_description', request.data.get('description', request.data.get('extra_service_description', ''))).strip()
        
        extra_charge_str = request.data.get('extra_charge', request.data.get('extra_service_charge', '0.00'))
        
        try:
            extra_charge = Decimal(str(extra_charge_str))
        except (ValueError, TypeError, InvalidOperation):
            return Response({"extra_charge": "Provide a valid numeric decimal charge."}, status=status.HTTP_400_BAD_REQUEST)

        if not extra_title:
            return Response({"extra_title": "An extra service title task description is required."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            booking.extra_service_title = extra_title
            booking.extra_service_description = extra_desc
            booking.extra_service_charge = extra_charge
            booking.technician_incentive = extra_charge * Decimal('0.15')
            
            if extra_charge > 0:
                booking.is_paid = False
                
            booking.save()

            for photo in uploaded_photos:
                BookingImage.objects.create(
                    booking=booking,
                    kind=BookingImage.Kind.BEFORE,
                    image=photo,
                    uploaded_by=request.user
                )

        notifications.booking_updated(booking)

        return Response({
            "message": "Extra service successfully appended to order invoice.",
            "updated_total_payable": float(booking.total_payable_amount),
            "allocated_incentive": float(booking.technician_incentive)
        }, status=status.HTTP_200_OK)


 

class UpdateBookingStatusView(APIView):
    """
    PATCH /api/bookings/<id>/status/
    Updates the operational state of a job and decodes multi-part evidence images safely.
    """
    permission_classes = [IsTechnician]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, pk):
        # 🟢 BYPASS SERIALIZER CRASHES: Read data directly from request payload parameters
        new_status = request.data.get("status")
        
        # Pull files safely from the multi-part stream array keys
        photos = request.FILES.getlist("images")

        if not new_status:
            return Response({"status": "This field is required."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            booking = get_object_or_404(Booking.objects.select_for_update(), pk=pk, technician=request.user)

            # Validate workflow transition safety paths
            if new_status not in ALLOWED_TRANSITIONS.get(booking.status, set()):
                return Response(
                    {"detail": f"Cannot move a '{booking.get_status_display()}' job to '{new_status}'."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if new_status == Booking.Status.IN_PROGRESS:
                kind, label = BookingImage.Kind.BEFORE, "before"
            else:
                if not booking.is_paid:
                    raise PaymentRequired()
                kind, label = _evidence_kind_for_completion(booking)

            # Enforce mandatory extra service criteria validations
            if new_status == Booking.Status.COMPLETED and booking.extra_service_charge > 0:
                extra_after_photos = request.FILES.getlist('extra_after_images')
                if not extra_after_photos:
                    return Response(
                        {"extra_after_images": "This booking contains extra services. You must upload 'after' camera photos to finish."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                for photo in extra_after_photos:
                    BookingImage.objects.create(
                        booking=booking, 
                        kind=BookingImage.Kind.EXTRA_AFTER, 
                        image=photo, 
                        uploaded_by=request.user
                    )

            if not photos:
                return Response(
                    {"images": f"Upload at least one '{label}' image to continue."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            for photo in photos:
                BookingImage.objects.create(booking=booking, kind=kind, image=photo, uploaded_by=request.user)
            
            booking.status = new_status
            booking.save(update_fields=["status", "updated_at"])

        # Trigger internal context messaging signals
        if new_status == Booking.Status.IN_PROGRESS:
            notifications.job_started(booking)
        else:
            notifications.job_completed(booking)

        # Notify via custom WhatsApp triggers
        send_whatsapp_booking_update(
            customer_phone=booking.customer.phone_number,
            booking_id=booking.id,
            status_label=booking.get_status_display()
        )

        from .serializers import BookingSerializer
        booking = _bookings_for(request.user).get(pk=booking.pk)
        return Response(BookingSerializer(booking, context={"request": request}).data, status=status.HTTP_200_OK)


# --- Admin actions --------------------------------------------------------------------


class AssignTechnicianView(APIView):
    """Admin assigns a Pending, unassigned booking to a technician."""

    permission_classes = [IsAdminRole]

    @transaction.atomic
    def post(self, request, pk):
        serializer = AssignTechnicianSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        technician = serializer.validated_data["technician"]

        # Lock the booking and the technician so two admins cannot assign the
        # same technician to the same slot at the same moment.
        booking = get_object_or_404(Booking.objects.select_for_update(), pk=pk)
        User.objects.select_for_update().get(pk=technician.pk)

        if booking.status != Booking.Status.PENDING or booking.technician_id is not None:
            raise ValidationError("Only unassigned Pending bookings can be assigned.")

        if technician_has_conflict(technician, booking.date, booking.time_slot, booking.pk):
            return Response(
                {
                    "detail": (
                        f"{technician.get_full_name() or technician.username} is already booked "
                        f"on {booking.date} for {booking.get_time_slot_display()}."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        booking.technician = technician
        booking.status = Booking.Status.CONFIRMED
        try:
            with transaction.atomic():
                booking.save(update_fields=["technician", "status", "updated_at"])
        except IntegrityError:
            # DB-level unique constraint caught a race we missed.
            return Response(
                {"detail": "That technician was just booked for this slot. Pick another."},
                status=status.HTTP_409_CONFLICT,
            )

        notifications.technician_assigned(booking)
        booking = _bookings_for(request.user).get(pk=booking.pk)
        return Response(BookingSerializer(booking, context={"request": request}).data)


class AdminStatsView(APIView):
    permission_classes = [IsAdminRole]

    def get(self, request):
        by_status = {s: 0 for s, _ in Booking.Status.choices}
        for row in Booking.objects.values("status").annotate(n=Count("id")):
            by_status[row["status"]] = row["n"]

        paid = Payment.objects.filter(status=Payment.Status.PAID)
        revenue = paid.aggregate(total=Sum("amount"))["total"] or 0
        return Response(
            {
                "total_bookings": sum(by_status.values()),
                "bookings_by_status": by_status,
                "unassigned_pending": Booking.objects.filter(
                    status=Booking.Status.PENDING, technician__isnull=True
                ).count(),
                "total_revenue": revenue,
                "paid_payments": paid.count(),
                "unpaid_bookings": Booking.objects.filter(is_paid=False)
                .exclude(status=Booking.Status.CANCELLED)
                .count(),
                "technicians": User.objects.filter(role=User.Role.TECHNICIAN, is_active=True).count(),
                "customers": User.objects.filter(role=User.Role.CUSTOMER, is_active=True).count(),
            }
        )


# --- Payments ----------------------------------------------------------------------------


class CreatePaymentOrderView(APIView):
    """(Re)create the Razorpay order for an unpaid booking owned by the caller."""

    permission_classes = [IsCustomer]

    def post(self, request, pk):
        booking = get_object_or_404(Booking, pk=pk, customer=request.user)
        if booking.is_paid:
            raise ValidationError("This booking is already paid.")
            
        # 👇 CHANGED: Evaluate dynamic combined cost property instead of base column 👇
        if booking.total_payable_amount <= 0:
            raise ValidationError("This booking has nothing to pay.")
        # 👆 CHANGED: Evaluate dynamic combined cost property instead of base column 👆
            
        try:
            payment = create_order_for_booking(booking)
        except RazorpayNotConfigured as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception:
            logger.exception("Razorpay order creation failed for booking %s", booking.pk)
            return Response(
                {"detail": "Could not reach the payment provider. Try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(checkout_payload(payment))
class VerifyPaymentView(APIView):
    """Called by the app after Razorpay Checkout succeeds.

    The signature is verified server-side with the key secret; only then is
    the payment marked Paid and booking.is_paid set to True.
    """

    permission_classes = [IsCustomer]

    def post(self, request):
        serializer = VerifyPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        payment = get_object_or_404(
            Payment.objects.select_related("booking__service", "booking__customer", "booking__address"),
            razorpay_order_id=d["razorpay_order_id"],
            booking__customer=request.user,
        )
        if payment.status == Payment.Status.PAID:
            return Response(PaymentSerializer(payment).data)  # idempotent

        try:
            with transaction.atomic():
                verify_and_mark_paid(payment, d["razorpay_payment_id"], d["razorpay_signature"])
        except SignatureVerificationError:
            payment.status = Payment.Status.FAILED
            payment.save(update_fields=["status"])
            return Response(
                {"detail": "Payment signature verification failed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except RazorpayNotConfigured as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        notifications.payment_received(payment.booking, payment)
        return Response(PaymentSerializer(payment).data)


class PaymentHistoryView(generics.ListAPIView):
    """Customer: own transactions / receipts. Admin: all transactions."""

    serializer_class = PaymentSerializer

    def get_queryset(self):
        qs = Payment.objects.select_related("booking__service", "booking__customer", "booking__address")
        user = self.request.user
        if user.role == User.Role.ADMIN:
            return qs
        if user.role == User.Role.CUSTOMER:
            return qs.filter(booking__customer=user)
        return qs.none()


# --- Notifications ---------------------------------------------------------------------------


class NotificationListView(APIView):
    """The caller's latest notifications + unread count (drives the bell badge)."""

    def get(self, request):
        qs = Notification.objects.filter(user=request.user)
        return Response(
            {
                "unread": qs.filter(is_read=False).count(),
                "results": NotificationSerializer(qs[:50], many=True).data,
            }
        )


class NotificationMarkReadView(APIView):
    def post(self, request):
        updated = Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"marked_read": updated})
