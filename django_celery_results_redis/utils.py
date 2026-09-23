"""Utilities."""
# This module must not use translation as that causes a recursive loader import.

from django.conf import settings
from django.utils import timezone

now_localtime = getattr(timezone, "template_localtime", timezone.localtime)


def now():
    """Return the current date and time."""
    if getattr(settings, "USE_TZ", False):
        return now_localtime(timezone.now())
    return timezone.now()
