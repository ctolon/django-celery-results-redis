"""The ``raw`` driver: record hashes only, queries scan every record."""

from .base import Store


class RawStore(Store):
    """Store without secondary structures.

    Every query reads all records of the model. Suitable for small result sets
    and for development.
    """
