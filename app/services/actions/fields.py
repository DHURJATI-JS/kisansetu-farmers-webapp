"""Reusable strict field types for action payloads."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Name = Annotated[str, StringConstraints(min_length=2, max_length=120, strip_whitespace=True)]
ShortText = Annotated[str, StringConstraints(max_length=300, strip_whitespace=True)]
LongText = Annotated[str, StringConstraints(max_length=2000, strip_whitespace=True)]
Money = Annotated[Decimal, Field(ge=0, le=Decimal("100000000"), max_digits=12, decimal_places=2)]
PositiveMoney = Annotated[Decimal, Field(gt=0, le=Decimal("100000000"), max_digits=12, decimal_places=2)]
Qty = Annotated[Decimal, Field(gt=0, le=Decimal("1000000"), max_digits=12, decimal_places=2)]
Lat = Annotated[float, Field(ge=-90, le=90)]
Lon = Annotated[float, Field(ge=-180, le=180)]
Phone = Annotated[str, StringConstraints(min_length=10, max_length=16, pattern=r"^[+0-9 \-]+$")]
Acres = Annotated[Decimal, Field(ge=0, le=Decimal("1000000"), max_digits=12, decimal_places=2)]


class TypedAddress(BaseModel):
    """A location typed by hand instead of GPS: a state of India from the list, district, village / town, optional PIN."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    state: Annotated[str, StringConstraints(min_length=2, max_length=64)]
    district: Annotated[str, StringConstraints(min_length=2, max_length=96)]
    place: Annotated[str, StringConstraints(min_length=2, max_length=96)]
    pincode: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{5}$")] | None = None

    @field_validator("pincode", mode="before")
    @classmethod
    def _blank_pin(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("state")
    @classmethod
    def _indian_state(cls, v: str) -> str:
        from app.services.geo import state_of

        s = state_of(v)
        if not s:
            raise ValueError("choose a state of India from the list")
        return s
