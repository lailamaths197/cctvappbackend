from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q

from django.core.validators import MinValueValidator, MaxValueValidator
class ServiceCategory(models.Model):
    """A bookable service, managed by the Admin (add / edit / deactivate)."""

    class ServiceType(models.TextChoices):
        INSTALLATION = "installation", "Installation"
        SERVICE_COMPLAINT = "service_complaint", "Service Complaint"

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    base_price = models.DecimalField(
        "price", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    service_type = models.CharField(
        max_length=20, choices=ServiceType.choices, default=ServiceType.SERVICE_COMPLAINT
    )
    icon = models.CharField(max_length=50, blank=True, help_text="Emoji shown in the app")
    is_active = models.BooleanField(default=True, help_text="Inactive services are hidden from customers")

    class Meta:
        verbose_name_plural = "service categories"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_service_type_display()})"


class Address(models.Model):
    """Service address captured in the booking flow including map tracking telemetry."""

    ADDRESS_MODES = (
        ('gps', 'GPS Location'),
        ('manual', 'Manual Entry'),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="addresses"
    )
    house_no = models.CharField("House / Flat No", max_length=50)
    street = models.CharField("Street / Locality", max_length=200)
    landmark = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    pincode = models.CharField(
        max_length=6,
        validators=[RegexValidator(r"^[1-9]\d{5}$", "Enter a valid 6-digit pincode.")],
    )
    
    # 🛰️ Map Tracking Extensions
    address_mode = models.CharField(max_length=10, choices=ADDRESS_MODES, default='gps', db_index=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "addresses"
        ordering = ["-created_at"]

    def __str__(self):
        return self.full_address

    @property
    def full_address(self):
        parts = [self.house_no, self.street, self.landmark, self.city, self.state, self.pincode]
        base_address = ", ".join(p for p in parts if p)
        if self.address_mode == 'gps' and self.latitude and self.longitude:
            return f"{base_address} (Lat: {self.latitude}, Lon: {self.longitude})"
        return base_address

class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class TimeSlot(models.TextChoices):
        S1 = "09:00-11:00", "09:00 AM - 11:00 AM"
        S2 = "11:00-13:00", "11:00 AM - 01:00 PM"
        S3 = "14:00-16:00", "02:00 PM - 04:00 PM"
        S4 = "16:00-18:00", "04:00 PM - 06:00 PM"

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookings"
    )
    technician = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_jobs",
        limit_choices_to={"role": "technician"},
    )
    service = models.ForeignKey(ServiceCategory, on_delete=models.PROTECT, related_name="bookings")
    address = models.ForeignKey(Address, on_delete=models.PROTECT, related_name="bookings")

    date = models.DateField()
    time_slot = models.CharField(max_length=11, choices=TimeSlot.choices)
    # Mandatory only for 'service_complaint' services (enforced in the serializer).
    issue_description = models.TextField(max_length=1000, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    is_paid = models.BooleanField(default=False, db_index=True)
    extra_service_title = models.CharField(max_length=255, null=True, blank=True)
    extra_service_description = models.TextField(max_length=2000, null=True, blank=True)
    extra_service_charge = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    technician_incentive = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Final safety net against double-booking, enforced by the database
            # even if two requests race past the application-level checks.
            models.UniqueConstraint(
                fields=["technician", "date", "time_slot"],
                condition=Q(technician__isnull=False) & ~Q(status="cancelled"),
                name="uniq_active_technician_date_slot",
            )
        ]
        indexes = [models.Index(fields=["date", "time_slot"])]

    def __str__(self):
        return f"Booking #{self.pk} - {self.service.name} on {self.date} {self.time_slot}"
    @property
    def total_payable_amount(self):
        """Calculates original contract plus on-site field additions dynamically."""
        return self.amount + self.extra_service_charge

def booking_image_path(instance, filename):
    # Filenames are randomised by bookings.utils.process_image
    return f"bookings/{instance.booking_id}/{instance.kind}/{filename}"

 
class BookingImage(models.Model):
    """One photo attached to a booking. `kind` says who/why it was uploaded."""

    class Kind(models.TextChoices):
        COMPLAINT = "complaint", "Customer photo"  # uploaded by the customer at booking time
        BEFORE = "before", "Before work"  # technician, when starting the job
        AFTER = "after", "After work"  # technician, completing a service complaint
        INSTALLATION = "installation", "Installation"  # technician, completing an installation
        EXTRA_BEFORE = "extra_before", "Extra service before photo"
        EXTRA_AFTER = "extra_after", "Extra service after photo"
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="images")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    image = models.ImageField(upload_to=booking_image_path)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="uploaded_images"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.get_kind_display()} image for booking #{self.booking_id}"


class Payment(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="payments")
    razorpay_order_id = models.CharField(max_length=64, unique=True)
    razorpay_payment_id = models.CharField(max_length=64, blank=True)
    razorpay_signature = models.CharField(max_length=256, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=7, choices=Status.choices, default=Status.CREATED)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.razorpay_order_id} ({self.status})"


class Notification(models.Model):
    """In-app notification row. Created on the server whenever a booking changes
    state; the apps poll /api/notifications/ and show an unread badge."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    booking = models.ForeignKey(
        Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    title = models.CharField(max_length=120)
    message = models.CharField(max_length=300)
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.user_id}: {self.title}"


class Review(models.Model):
    booking = models.OneToOneField(Booking, on_delete=models.CASCADE, related_name="review")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Review for Booking #{self.booking_id} - {self.rating} Stars"
