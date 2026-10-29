from django.urls import path

from .views import (
    AdminServiceDetailView,
    AdminServiceListCreateView,
    AdminStatsView,
    AssignTechnicianView,
    TechnicianAddExtraServiceView,
    BookingDetailView,
    BookingListCreateView,
    BookingSecureCallView,
    CreatePaymentOrderView,
    NotificationListView,
    NotificationMarkReadView,
    PaymentHistoryView,
    ServiceCategoryListView,
    UpdateBookingStatusView,
    VerifyPaymentView,
    BookingSubmitReviewView,
    BookingAvailableSlotsView,
)

urlpatterns = [
    path("services/", ServiceCategoryListView.as_view(), name="services"),
    path("bookings/", BookingListCreateView.as_view(), name="bookings"),
    path("bookings/slots/", BookingAvailableSlotsView.as_view(), name="booking-slots"),
    path("bookings/<int:pk>/", BookingDetailView.as_view(), name="booking-detail"),
    path("bookings/<int:pk>/status/", UpdateBookingStatusView.as_view(), name="booking-status"),
    path("bookings/<int:pk>/assign/", AssignTechnicianView.as_view(), name="booking-assign"),
    path("bookings/<int:pk>/pay/", CreatePaymentOrderView.as_view(), name="booking-pay"),
    path("bookings/<int:pk>/add-extra/", TechnicianAddExtraServiceView.as_view(), name="booking-add-extra"),
    path("payments/", PaymentHistoryView.as_view(), name="payments"),
    path("payments/verify/", VerifyPaymentView.as_view(), name="payment-verify"),
    path("notifications/", NotificationListView.as_view(), name="notifications"),
    path("notifications/read/", NotificationMarkReadView.as_view(), name="notifications-read"),
    path("admin/stats/", AdminStatsView.as_view(), name="admin-stats"),
    path("admin/services/", AdminServiceListCreateView.as_view(), name="admin-services"),
    path("admin/services/<int:pk>/", AdminServiceDetailView.as_view(), name="admin-service-detail"),
    path('<int:pk>/secure-call/', BookingSecureCallView.as_view(), name='booking-secure-call'),
    path('<int:pk>/submit-review/', BookingSubmitReviewView.as_view(), name='booking-submit-review'),

]
