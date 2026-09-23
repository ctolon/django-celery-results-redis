"""Storage description of a model, derived from its Django fields."""

from dataclasses import dataclass

from django.db import models

TEXT = "text"
DATETIME = "datetime"
INTEGER = "integer"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    null: bool
    field: models.Field


@dataclass(frozen=True)
class Schema:
    """What a store needs to know about a model.

    ``kind`` is the key namespace of the model's records. ``tag_fields`` and
    ``date_fields`` are the fields the indexing drivers index by value and by
    time respectively, ``search_fields`` the text fields the optional search
    index covers.
    """

    model: type[models.Model]
    kind: str
    pk: str
    fields: dict[str, FieldSpec]
    tag_fields: tuple[str, ...]
    date_fields: tuple[str, ...]
    search_fields: tuple[str, ...]

    @classmethod
    def for_model(cls, model):
        fields = {
            field.name: FieldSpec(field.name, _kind(field), field.null, field)
            for field in model._meta.concrete_fields
        }
        indexed = model.redis_indexed_fields
        return cls(
            model=model,
            kind=model.redis_kind,
            pk=model._meta.pk.name,
            fields=fields,
            tag_fields=tuple(f for f in indexed if fields[f].kind != DATETIME),
            date_fields=tuple(f for f in indexed if fields[f].kind == DATETIME),
            search_fields=tuple(model.redis_search_fields),
        )

    @property
    def nullable_fields(self):
        return tuple(name for name, spec in self.fields.items() if spec.null)


def _kind(field):
    if isinstance(field, models.DateTimeField):
        return DATETIME
    if isinstance(field, models.IntegerField):
        return INTEGER
    return TEXT
