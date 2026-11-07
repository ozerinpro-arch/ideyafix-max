"""Explicit school clarifications layered over unchanged source quotations."""
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .scheduling_models import Identifier, StrictModel


class SourceRuleUpdate(StrictModel):
    conditionId: Identifier
    textHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    teacher: str = Field(min_length=1, max_length=120)
    effect: Literal['personal_exemption', 'effective_from']
    effectiveFrom: str | None = None
    sourceId: Identifier
    sourceLocator: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def dates(self):
        if self.effectiveFrom is not None:
            parsed = date.fromisoformat(self.effectiveFrom)
            if parsed.isoformat() != self.effectiveFrom:
                raise ValueError('Требуется точная дата YYYY-MM-DD')
        if self.effect == 'personal_exemption' and self.effectiveFrom is not None:
            raise ValueError('Датированное исключение требует отдельного подтверждения области действия')
        return self
