"""Key names."""

import uuid


class KeySpace:
    """Key names of one model's records and indexes under a prefix."""

    def __init__(self, prefix, kind):
        self.prefix = prefix
        self.kind = kind
        self._record_prefix = f"{prefix}:{kind}:"

    def record(self, pk):
        return f"{self._record_prefix}{pk}"

    @property
    def record_pattern(self):
        return f"{self._record_prefix}*"

    def pk_from_record(self, key):
        return key[len(self._record_prefix) :]

    @property
    def index_prefix(self):
        return f"{self.prefix}:idx:{self.kind}:"

    @property
    def all_members(self):
        return f"{self.index_prefix}all"

    def members(self, field, value):
        return f"{self.index_prefix}s:{field}:{value}"

    def values(self, field):
        return f"{self.index_prefix}v:{field}"

    def nulls(self, field):
        return f"{self.index_prefix}n:{field}"

    def scores(self, field):
        return f"{self.index_prefix}z:{field}"

    def counts(self, field):
        return f"{self.index_prefix}c:{field}"

    @property
    def search_index(self):
        return f"{self.prefix}:ft:{self.kind}"

    @property
    def text_index(self):
        return f"{self.prefix}:fts:{self.kind}"

    @property
    def meta(self):
        return f"{self.prefix}:meta"

    def temporary(self):
        return f"{self.prefix}:tmp:{uuid.uuid4().hex}"
