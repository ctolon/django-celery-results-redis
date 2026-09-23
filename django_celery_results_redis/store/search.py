"""Optional RediSearch index for substring searches.

RediSearch matches whole tags and tokens, and its wildcard matching is capped
by ``MAXEXPANSIONS``, so neither can answer ``icontains`` exactly. This index
instead stores the trigrams of each searchable field. A term matches only
records holding every trigram of that term, which is a superset of the real
matches: the store evaluates the lookup itself on the records the index
returns, so the result is exactly what the in-memory evaluator would give.

Values longer than ``SEARCH_MAX_TEXT`` are not tokenized; their field is
marked as overflowing instead, which keeps the record in every candidate set
for that field.
"""

from hashlib import blake2b

from redis.commands.search.query import Query
from redis.exceptions import ResponseError

from ..conf import app_settings
from .query.ast import And, Condition, Or

GRAM_SIZE = 3
#: Marks a value that was too long to tokenize. Tokens are hexadecimal, so no
#: trigram can produce this tag.
OVERFLOW_TOKEN = "overflow"
DIALECT = 2
#: Lookups whose match implies a case-insensitive substring match.
SEARCHABLE_LOOKUPS = frozenset(
    {
        "exact",
        "iexact",
        "contains",
        "icontains",
        "startswith",
        "istartswith",
        "endswith",
        "iendswith",
    }
)


def gram_field(field):
    return f"g_{field}"


def token(gram):
    return blake2b(gram.encode(), digest_size=4).hexdigest()


def tokens(text):
    """Return the trigram tokens of ``text``, case-insensitively."""
    folded = text.casefold()
    return {
        token(folded[index : index + GRAM_SIZE])
        for index in range(len(folded) - GRAM_SIZE + 1)
    }


class SearchIndex:
    """Trigram index over the searchable fields of one model."""

    def __init__(self, store):
        self.store = store
        self.schema = store.schema
        self.keys = store.keys
        self.client = store.client
        self.index = store.client.ft(self.keys.text_index)
        self.max_text = app_settings.SEARCH_MAX_TEXT
        self.max_candidates = app_settings.SEARCH_MAX_CANDIDATES

    # Indexing.

    def ensure_index(self):
        schema = " ".join(
            f"{gram_field(field)} TAG SEPARATOR , CASESENSITIVE"
            for field in self.schema.search_fields
        )
        schema = f"ON HASH PREFIX 1 {self.keys.record('')} SCHEMA {schema}"
        digest = token(schema)
        digest_key = f"{self.keys.text_index}:schema"
        if self.client.get(digest_key) == digest and self._exists():
            return
        if self._exists():
            self.index.dropindex(delete_documents=False)
        try:
            self.client.execute_command(
                "FT.CREATE", self.keys.text_index, *schema.split()
            )
        except ResponseError as exc:
            if "already exists" not in str(exc).lower():
                raise
        self.client.set(digest_key, digest)

    def _exists(self):
        try:
            self.index.info()
        except ResponseError:
            return False
        return True

    def index_values(self, values):
        """Return the index fields to write for the record fields in ``values``."""
        indexed = {}
        for field in self.schema.search_fields:
            if field not in values:
                continue
            value = values[field]
            if value is None:
                indexed[gram_field(field)] = None
            elif len(str(value)) > self.max_text:
                indexed[gram_field(field)] = OVERFLOW_TOKEN
            else:
                grams = sorted(tokens(str(value)))
                indexed[gram_field(field)] = ",".join(grams) or None
        return indexed

    # Querying.

    def candidates(self, where):
        """Return the primary keys that can match ``where``, or ``None``.

        ``None`` means the index cannot narrow the query down, and the caller
        has to read every record.
        """
        clauses = where.children if isinstance(where, And) else (where,)
        queries = [query for query in map(self._clause, clauses) if query]
        if not queries:
            return None
        return self._search(" ".join(f"({query})" for query in queries))

    def _clause(self, node):
        if isinstance(node, Or):
            parts = [self._term(child) for child in node.children]
            if not parts or not all(parts):
                return None
            return " | ".join(f"({part})" for part in parts)
        return self._term(node)

    def _term(self, node):
        if (
            not isinstance(node, Condition)
            or node.field not in self.schema.search_fields
            or node.transform is not None
            or node.lookup not in SEARCHABLE_LOOKUPS
        ):
            return None
        grams = tokens(str(node.value))
        if not grams:
            return None
        field = gram_field(node.field)
        matched = " ".join(f"@{field}:{{{gram}}}" for gram in sorted(grams))
        return f"({matched}) | @{field}:{{{OVERFLOW_TOKEN}}}"

    def _search(self, query):
        total = self.index.search(Query(query).dialect(DIALECT).paging(0, 0)).total
        if total > self.max_candidates:
            return None
        if not total:
            return set()
        result = self.index.search(
            Query(query).dialect(DIALECT).no_content().paging(0, total)
        )
        return {self.keys.pk_from_record(document.id) for document in result.docs}
