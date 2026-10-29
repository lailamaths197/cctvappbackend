from rest_framework.permissions import BasePermission

from .models import User


class _RolePermission(BasePermission):
    role = None

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.role == self.role)


class IsCustomer(_RolePermission):
    role = User.Role.CUSTOMER


class IsTechnician(_RolePermission):
    role = User.Role.TECHNICIAN


class IsAdminRole(_RolePermission):
    role = User.Role.ADMIN
