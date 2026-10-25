"""Request and response schemas. Input is validated strictly: unknown labels, non-numeric or
out-of-range scores, NaN/inf and booleans are rejected with 422."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings

Label = Literal["minor", "sexual_violence", "bestiality", "self_harm", "real_person", "explicit"]
# Strict float accepts ints but not true/false or strings; allow_inf_nan=False rejects NaN/Infinity.
Score = Annotated[float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)]


class MediaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image", "video"]
    source: Literal["upload", "generation"]
    prompt: Annotated[str, Field(max_length=4000)] | None = None
    frames: Annotated[list[dict[Label, Score]], Field(min_length=1, max_length=settings.max_frame_count)]

    @model_validator(mode="after")
    def _shape(self) -> "MediaIn":
        if self.type == "image" and len(self.frames) != 1:
            raise ValueError("an image has exactly one frame")
        if self.source == "generation" and not (self.prompt and self.prompt.strip()):
            raise ValueError("generated media must carry its prompt")
        return self


class MediaAccepted(BaseModel):
    id: str
    status: Literal["pending"]


class MediaOut(BaseModel):
    id: str
    status: str
    reasons: list[str]
