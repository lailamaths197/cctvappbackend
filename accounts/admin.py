from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("email", "first_name", "last_name", "role", "phone_number", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("email", "first_name", "last_name", "phone_number")
    fieldsets = UserAdmin.fieldsets + (
        ("CCTV app", {"fields": ("role", "phone_number", "profile_image")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("CCTV app", {"fields": ("email", "role", "phone_number")}),
    )
