from django.contrib import admin

from .models import Address, Booking, BookingImage, Notification, Payment, ServiceCategory


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "service_type", "base_price", "is_active")
    list_editable = ("base_price", "is_active")
    list_filter = ("service_type", "is_active")


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("user", "house_no", "street", "city", "state", "pincode")
    search_fields = ("city", "pincode", "user__email")


class BookingImageInline(admin.TabularInline):
    model = BookingImage
    extra = 0
    readonly_fields = ("uploaded_by", "created_at")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "service", "customer", "technician", "date", "time_slot", "status", "is_paid")
    list_filter = ("status", "is_paid", "date", "service__service_type")
    search_fields = ("customer__email", "technician__email")
    inlines = [BookingImageInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("razorpay_order_id", "booking", "amount", "status", "created_at")
    list_filter = ("status",)
    readonly_fields = ("razorpay_signature",)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "is_read", "created_at")
    list_filter = ("is_read",)
