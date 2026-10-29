import io
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers

MAX_DIMENSION = 1600
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def process_image(upload):
    """Validate, normalise and re-encode one uploaded photo.

    * rejects anything that is not a real JPEG/PNG/WEBP (checked with Pillow,
      not just the file extension or Content-Type header)
    * enforces the size limit from settings.MAX_UPLOAD_SIZE_MB
    * applies EXIF rotation, downsizes to MAX_DIMENSION and strips metadata
      (including GPS data) by re-encoding as JPEG
    * returns a ContentFile with a random filename so user-supplied names are
      never used on disk
    """
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if upload.size > max_bytes:
        raise serializers.ValidationError(
            f"Image is too large. Maximum size is {settings.MAX_UPLOAD_SIZE_MB} MB each."
        )

    try:
        upload.seek(0)
        probe = Image.open(upload)
        probe.verify()  # integrity check; invalidates the image object
        upload.seek(0)
        image = Image.open(upload)
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise serializers.ValidationError("Upload valid images only (JPEG, PNG or WEBP).")

    if image.format not in ALLOWED_FORMATS:
        raise serializers.ValidationError("Unsupported image type. Use JPEG, PNG or WEBP.")

    image = ImageOps.exif_transpose(image)
    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    if image.mode != "RGB":
        image = image.convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85, optimize=True)
    return ContentFile(buffer.getvalue(), name=f"{uuid.uuid4().hex}.jpg")


def process_images(uploads):
    """Process an array of uploads (the `images` multipart key, repeated)."""
    limit = settings.MAX_IMAGES_PER_UPLOAD
    if len(uploads) > limit:
        raise serializers.ValidationError(f"You can upload at most {limit} images at a time.")
    return [process_image(u) for u in uploads]
# =============================================================================
# 🛰️ TWILIO COMMUNICATION PIPELINES (WHATSAPP NOTIFICATIONS & MASKED CALLS)
# =============================================================================
import logging
from twilio.rest import Client

logger = logging.getLogger(__name__)

def send_whatsapp_booking_update(customer_phone: str, booking_id: int, status_label: str):
    """
    Dispatches explicit transactional updates directly to a customer's WhatsApp line
    whenever an installation or service event status state changes.
    """
    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    whatsapp_from = getattr(settings, "TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

    if not account_sid or not auth_token:
        logger.warning(f"[WhatsApp] Credentials missing. Skipping notice for Booking #{booking_id}.")
        return

    try:
        client = Client(account_sid, auth_token)
        # Normalize the phone destination to Twilio WhatsApp scheme
        to_phone = f"whatsapp:{customer_phone}" if not customer_phone.startswith("whatsapp:") else customer_phone
        
        message_body = (
            f"Vetri IT Systems Private Limited 🛠️\n\n"
            f"Update for Booking *#{booking_id}*:\n"
            f"Your CCTV service assignment state is now: *{status_label.upper()}*.\n\n"
            f"Track real-time engineering location telemetry directly inside our mobile application."
        )

        client.messages.create(
            body=message_body,
            from_=whatsapp_from,
            to=to_phone
        )
        logger.info(f"[WhatsApp] Status notice sent for Booking #{booking_id} successfully.")
    except Exception as e:
        logger.error(f"[WhatsApp] Notification layer failure on Booking #{booking_id}: {str(e)}")


def create_masked_call_session(technician_phone: str, customer_phone: str) -> str:
    """
    Creates a transient, secure communication bridge using Twilio Proxy Service.
    
    Protects both parties by returning a single masked proxy number for connection loops.
    """
    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    proxy_service_sid = getattr(settings, "TWILIO_PROXY_SERVICE_SID", "")

    if not account_sid or not auth_token or not proxy_service_sid:
        logger.warning("[Twilio Proxy] Configuration metadata missing.")
        return ""

    try:
        client = Client(account_sid, auth_token)

        # 1. Spawn a dedicated, dynamic session block
        session = client.proxy.v1.services(proxy_service_sid).sessions.create()

        # 2. Add Technician Participant
        client.proxy.v1.services(proxy_service_sid) \
            .sessions(session.sid).participants.create(identifier=technician_phone)

        # 3. Add Customer Participant
        customer_part = client.proxy.v1.services(proxy_service_sid) \
            .sessions(session.sid).participants.create(identifier=customer_phone)

        # Return proxy identifier safely to let the dialling pipeline map calls anonymously
        return customer_part.proxy_identifier

    except Exception as e:
        logger.error(f"[Twilio Proxy] Failed to establish communication session: {str(e)}")
        return ""
