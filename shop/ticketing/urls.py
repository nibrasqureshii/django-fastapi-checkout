from django.urls import path

from . import views

urlpatterns = [
    path("", views.checkout_page, name="checkout"),
    path("orders/<uuid:order_id>/", views.checkout_page, name="order-page"),
    path("api/events/<int:event_id>/", views.event_detail, name="event-detail"),
    path("api/orders/", views.create_order, name="order-create"),
    path("api/orders/<uuid:order_id>/", views.order_detail, name="order-detail"),
    path("api/orders/<uuid:order_id>/pay/", views.pay, name="order-pay"),
    path("api/webhooks/gateway/", views.gateway_webhook, name="gateway-webhook"),
]
