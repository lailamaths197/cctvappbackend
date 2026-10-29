from django.db.models import Count, Q
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import User
from .permissions import IsAdminRole
from .serializers import (
    AdminUserSerializer,
    AdminUserUpdateSerializer,
    EmailTokenObtainPairSerializer,
    RegisterSerializer,
    TechnicianCreateSerializer,
    UserSerializer,
)


class RegisterView(generics.CreateAPIView):
    """Public sign-up. Always creates a CUSTOMER."""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class LoginView(TokenObtainPairView):
    serializer_class = EmailTokenObtainPairSerializer
    permission_classes = [permissions.AllowAny]


class MeView(generics.RetrieveAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class TechnicianListCreateView(generics.ListCreateAPIView):
    """Admin only.  GET: technicians (for the assignment picker).
    POST: create a technician + login credentials."""

    permission_classes = [IsAdminRole]

    def get_serializer_class(self):
        return TechnicianCreateSerializer if self.request.method == "POST" else UserSerializer

    def get_queryset(self):
        return User.objects.filter(role=User.Role.TECHNICIAN, is_active=True).order_by("first_name")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        body = dict(UserSerializer(user).data)
        # Shown ONCE, only when the system generated it. Never stored in plain text.
        body["temporary_password"] = serializer.generated_password
        return Response(body, status=status.HTTP_201_CREATED)


class AdminUserListView(generics.ListAPIView):
    """Admin only: every account, with filters  ?role=customer|technician|admin  &  ?search=text."""

    permission_classes = [IsAdminRole]
    serializer_class = AdminUserSerializer

    def get_queryset(self):
        qs = User.objects.annotate(
            customer_bookings=Count("bookings", distinct=True),
            assigned_count=Count("assigned_jobs", distinct=True),
        ).order_by("-date_joined")
        role = self.request.query_params.get("role")
        if role in dict(User.Role.choices):
            qs = qs.filter(role=role)
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
                | Q(phone_number__icontains=search)
            )
        return qs


class AdminUserDetailView(generics.UpdateAPIView):
    """Admin only: activate / deactivate an account."""

    permission_classes = [IsAdminRole]
    serializer_class = AdminUserUpdateSerializer
    queryset = User.objects.all()
    http_method_names = ["patch", "options"]

    def perform_update(self, serializer):
        target = serializer.instance
        if target.pk == self.request.user.pk:
            raise ValidationError({"detail": "You cannot deactivate your own account."})
        if target.role == User.Role.ADMIN:
            raise ValidationError({"detail": "Admin accounts are managed in the Django admin site."})
        serializer.save()
