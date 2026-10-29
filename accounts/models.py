from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import RegexValidator
from django.db import models


class CustomUserManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        # `createsuperuser` should produce someone who can use the Admin dashboard.
        extra_fields.setdefault("role", "admin")
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    """Custom user with a role, phone number and optional profile image.

    Login is by username, but the registration API sets username = email
    so the mobile app can treat email as the login identifier.
    """

    class Role(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        TECHNICIAN = "technician", "Technician"
        ADMIN = "admin", "Admin"

    email = models.EmailField(unique=True)
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.CUSTOMER)
    phone_number = models.CharField(
        max_length=10,
        unique=True,
        validators=[RegexValidator(r"^[6-9]\d{9}$", "Enter a valid 10-digit Indian mobile number.")],
    )
    profile_image = models.ImageField(upload_to="profiles/", blank=True, null=True)

    objects = CustomUserManager()
    REQUIRED_FIELDS = ["email", "phone_number"]

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.role})"

    @property
    def is_customer(self):
        return self.role == self.Role.CUSTOMER

    @property
    def is_technician(self):
        return self.role == self.Role.TECHNICIAN

    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN
