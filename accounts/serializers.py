import secrets
import string

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User


def generate_password(length=12):
    """Random temporary password: guaranteed upper, lower, digit and symbol."""
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "@#$%&*!"]
    chars = [secrets.choice(p) for p in pools]
    alphabet = string.ascii_letters + string.digits
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "full_name", "email", "phone_number", "role", "profile_image")
        read_only_fields = fields

    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username


class AdminUserSerializer(UserSerializer):
    """What the Admin sees when monitoring every account in the system."""

    job_count = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ("is_active", "date_joined", "job_count")
        read_only_fields = fields

    def get_job_count(self, obj):
        # annotated by AdminUserListView: bookings made (customer) / jobs assigned (technician)
        if obj.role == User.Role.TECHNICIAN:
            return getattr(obj, "assigned_count", 0)
        if obj.role == User.Role.CUSTOMER:
            return getattr(obj, "customer_bookings", 0)
        return 0


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    """The Admin may only switch an account on/off (a deactivated user cannot log in)."""

    class Meta:
        model = User
        fields = ("is_active",)


class RegisterSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ("full_name", "email", "phone_number", "password", "confirm_password")

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        validate_password(attrs["password"])
        return attrs

    def create(self, validated_data):
        validated_data.pop("confirm_password")
        full_name = validated_data.pop("full_name").strip()
        first, _, last = full_name.partition(" ")
        # Public registration ALWAYS creates a customer. Technicians and
        # admins are created by staff, never self-assigned.
        return User.objects.create_user(
            username=validated_data["email"],
            email=validated_data["email"],
            phone_number=validated_data["phone_number"],
            password=validated_data["password"],
            first_name=first,
            last_name=last,
            role=User.Role.CUSTOMER,
        )


class TechnicianCreateSerializer(serializers.ModelSerializer):
    """Admin-only: create a technician account and its login credentials.

    If the Admin leaves `password` blank a strong temporary one is generated;
    the view returns it exactly once so the Admin can hand it to the technician.
    """

    full_name = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ("full_name", "email", "phone_number", "password")

    generated_password = None

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate(self, attrs):
        if attrs.get("password"):
            validate_password(attrs["password"])
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password", "") or ""
        if not password:
            password = generate_password()
            self.generated_password = password
        first, _, last = validated_data.pop("full_name").strip().partition(" ")
        return User.objects.create_user(
            username=validated_data["email"],
            email=validated_data["email"],
            phone_number=validated_data["phone_number"],
            password=password,
            first_name=first,
            last_name=last,
            role=User.Role.TECHNICIAN,
        )


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login with email + password; response also carries the user object."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"] = serializers.EmailField(write_only=True)
        self.fields["username"].required = False

    def validate(self, attrs):
        # The app sends `email`. Look the account up by email so users created
        # in the Django admin (whose username may differ) can still log in.
        email = attrs.pop("email").lower().strip()
        match = User.objects.filter(email__iexact=email).first()
        attrs["username"] = match.username if match else email
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user, context=self.context).data
        return data
