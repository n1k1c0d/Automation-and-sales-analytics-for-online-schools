from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type PercentagePoints = Annotated[int, Field(ge=-100, le=100)]
type ProfileCode = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,24}$")]


class FunnelAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    lesson_completion_pp: PercentagePoints = 0
    bank_application_pp: PercentagePoints = 0
    payment_success_pp: PercentagePoints = 0


NO_ADJUSTMENT = FunnelAdjustment()


class ConversionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: ProfileCode
    managers: dict[str, FunnelAdjustment] = Field(default_factory=dict)
    sources: dict[str, FunnelAdjustment] = Field(default_factory=dict)

    def offsets_for(self, manager: str, source: str) -> tuple[int, int, int]:
        manager_effect = self.managers.get(manager, NO_ADJUSTMENT)
        source_effect = self.sources.get(source, NO_ADJUSTMENT)
        return (
            manager_effect.lesson_completion_pp + source_effect.lesson_completion_pp,
            manager_effect.bank_application_pp + source_effect.bank_application_pp,
            manager_effect.payment_success_pp + source_effect.payment_success_pp,
        )
