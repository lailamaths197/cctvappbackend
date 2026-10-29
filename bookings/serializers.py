from datetime import date as date_cls

from django.core.validators import RegexValidator
from rest_framework import serializers

from accounts.models import User

from .models import Address, Booking, BookingImage, Notification, Payment, ServiceCategory, Review
from .utils import process_images

MIN_COMPLAINT_DESCRIPTION = 10


# --- Services ----------------------------------------------------------------


class ServiceCategorySerializer(serializers.ModelSerializer):
    """Read-only public view (customers only ever see active services)."""

    class Meta:
        model = ServiceCategory
        fields = ("id", "name", "description", "base_price", "service_type", "icon")


class AdminServiceSerializer(serializers.ModelSerializer):
    """Admin create / update of the service catalog."""

    class Meta:
        model = ServiceCategory
        fields = ("id", "name", "description", "base_price", "service_type", "icon", "is_active")

    def validate_name(self, value):
        value = value.strip()
        if len(value) < 3:
            raise serializers.ValidationError("Name must be at least 3 characters.")
        return value


# --- Bookings Serializers Module ---------------------------------------------

class AddressSerializer(serializers.ModelSerializer):
    full_address = serializers.CharField(read_only=True)

    class Meta:
        model = Address
        fields = (
            "id", 
            "house_no", 
            "street", 
            "landmark", 
            "city", 
            "state", 
            "pincode", 
            "full_address",
            # 🛰️ Exposed Telemetry Fields for Frontend Mobile App Mapping
            "address_mode",
            "latitude",
            "longitude",
        )


class PersonSerializer(serializers.ModelSerializer):
    """Minimal public view of a customer/technician inside a booking."""

    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "full_name", "phone_number")

    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username


class BookingImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingImage
        fields = ("id", "kind", "image", "created_at")
        read_only_fields = fields


class BookingSerializer(serializers.ModelSerializer):
    service = ServiceCategorySerializer(read_only=True)
    address = AddressSerializer(read_only=True)
    customer = PersonSerializer(read_only=True)
    technician = PersonSerializer(read_only=True)
    images = BookingImageSerializer(many=True, read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    time_slot_label = serializers.CharField(source="get_time_slot_display", read_only=True)
    
    total_payable_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Booking
        fields = (
            "id",
            "service",
            "customer",
            "technician",
            "address",
            "date",
            "time_slot",
            "time_slot_label",
            "issue_description",
            "images",
            "status",
            "status_label",
            "amount",
            "is_paid",
            "created_at",
            "extra_service_title",
            "extra_service_description",
            "extra_service_charge",
            "technician_incentive",
            "total_payable_amount",
        )
        read_only_fields = fields


class BookingCreateSerializer(serializers.Serializer):
    """Flat fields layout optimized for multi-part parsing.
    
    Handles both live map pins and manual address fallbacks cleanly.
    """
    service = serializers.PrimaryKeyRelatedField(
        queryset=ServiceCategory.objects.filter(is_active=True)
    )
    date = serializers.DateField()
    time_slot = serializers.ChoiceField(choices=Booking.TimeSlot.choices)
    issue_description = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")
    images = serializers.ListField(child=serializers.ImageField(), required=False)

    # 🛰️ Map Mode Mapping Extensions
    address_mode = serializers.ChoiceField(choices=[('gps', 'GPS Location'), ('manual', 'Manual Entry')], default='gps')
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)

    # Physical Address Variables (Made optional dynamically during validation if address_mode is 'gps')
    house_no = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    street = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    landmark = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    city = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    state = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    pincode = serializers.CharField(
        required=False, 
        allow_blank=True, 
        default="",
        validators=[RegexValidator(r"^[1-9]\d{5}$", "Enter a valid 6-digit pincode.")]
    )

    def validate_date(self, value):
        if value < date_cls.today():
            raise serializers.ValidationError("Date cannot be in the past.")
        return value

    def validate_images(self, value):
        return process_images(value)

    def validate(self, attrs):
        # 1. Structural Complaint Text Validation Logic
        description = (attrs.get("issue_description") or "").strip()
        if attrs["service"].service_type == ServiceCategory.ServiceType.SERVICE_COMPLAINT:
            if len(description) < MIN_COMPLAINT_DESCRIPTION:
                raise serializers.ValidationError(
                    {"issue_description": f"Describe the problem (at least {MIN_COMPLAINT_DESCRIPTION} characters) for service complaints."}
                )
        attrs["issue_description"] = description
        attrs.setdefault("images", [])

        # 2. Dynamic Field Constraints based on User Address Mode
        mode = attrs.get("address_mode", "gps")
        if mode == "manual":
            required_manual_fields = ["house_no", "street", "city", "state", "pincode"]
            for field in required_manual_fields:
                if not attrs.get(field):
                    raise serializers.ValidationError({field: f"{field.replace('_', ' ').title()} is mandatory in manual entry mode."})
        else:
            if not attrs.get("latitude") or not attrs.get("longitude"):
                raise serializers.ValidationError({"non_field_errors": "Interactive map coordinate telemetry data must be locked in GPS mode."})
            
            # Pre-fill placeholder parameters for backend table consistency
            attrs["house_no"] = attrs.get("house_no") or "GPS Location"
            attrs["street"] = attrs.get("street") or "Detected Area"
            attrs["city"] = attrs.get("city") or "Locked Coordinates"
            attrs["state"] = attrs.get("state") or "Locked Coordinates"
            attrs["pincode"] = attrs.get("pincode") or "000000"

        return attrs

    def create(self, validated_data):
        user = self.context['request'].user
        images_list = validated_data.pop("images", [])

        # Create Address instance relation mapping
        address = Address.objects.create(
            user=user,
            address_mode=validated_data.get("address_mode"),
            house_no=validated_data.get("house_no"),
            street=validated_data.get("street"),
            landmark=validated_data.get("landmark"),
            city=validated_data.get("city"),
            state=validated_data.get("state"),
            pincode=validated_data.get("pincode"),
            latitude=validated_data.get("latitude"),
            longitude=validated_data.get("longitude")
        )

        service_obj = validated_data.get("service")
        
        booking = Booking.objects.create(
            customer=user,
            service=service_obj,
            address=address,
            date=validated_data.get("date"),
            time_slot=validated_data.get("time_slot"),
            issue_description=validated_data.get("issue_description"),
            amount=service_obj.base_price
        )

        # Process accompanying multi-part photos array
        for processed_img in images_list:
            BookingImage.objects.create(
                booking=booking,
                kind=BookingImage.Kind.COMPLAINT,
                image=processed_img,
                uploaded_by=user
            )

        return booking


class UpdateStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[Booking.Status.IN_PROGRESS, Booking.Status.COMPLETED])
    # 🟢 FIXED: Accept the raw list files from FormData directly without any middleware interception
    images = serializers.ListField(child=serializers.ImageField(), required=False, default=list)

    def validate(self, attrs):
        attrs.setdefault("images", [])
        return attrs

class AssignTechnicianSerializer(serializers.Serializer):
    technician_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role="technician", is_active=True),
        source="technician",
    )

# --- Payments & notifications ------------------------------------------------


class PaymentSerializer(serializers.ModelSerializer):
    booking_id = serializers.IntegerField(read_only=True)
    service_name = serializers.CharField(source="booking.service.name", read_only=True)
    # Receipt fields
    receipt_no = serializers.SerializerMethodField()
    customer_name = serializers.CharField(source="booking.customer.get_full_name", read_only=True)
    booking_date = serializers.DateField(source="booking.date", read_only=True)
    time_slot_label = serializers.CharField(source="booking.get_time_slot_display", read_only=True)
    address = serializers.CharField(source="booking.address.full_address", read_only=True)

    class Meta:
        model = Payment
        fields = (
            "id",
            "receipt_no",
            "booking_id",
            "service_name",
            "customer_name",
            "booking_date",
            "time_slot_label",
            "address",
            "razorpay_order_id",
            "razorpay_payment_id",
            "amount",
            "currency",
            "status",
            "created_at",
            "paid_at",
        )
        read_only_fields = fields

    def get_receipt_no(self, obj):
        return f"RCPT-{obj.pk:06d}"


class VerifyPaymentSerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField()
    razorpay_payment_id = serializers.CharField()
    razorpay_signature = serializers.CharField()


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ("id", "title", "message", "booking", "is_read", "created_at")
        read_only_fields = fields
class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = ('id', 'booking', 'rating', 'comment', 'created_at')
        read_only_fields = ('customer',)

    def validate(self, attrs):
        booking = attrs['booking']
        request = self.context.get('request')
        
        # Guard 1: Verify ownership
        if booking.customer != request.user:
            raise serializers.ValidationError("You can only review your own bookings.")
            
        # Guard 2: Enforce status check
        if booking.status != Booking.Status.COMPLETED:
            raise serializers.ValidationError("Reviews can only be added to completed jobs.")
            
        return attrs

    def create(self, validated_data):
        validated_data['customer'] = self.context['request'].user
        return super().create(validated_data)
