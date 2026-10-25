"""Request and response schemas."""

from typing import Literal

from pydantic import BaseModel, model_validator


class MediaIn(BaseModel):
    type: Literal["image", "video"]
    source: Literal["upload", "generation"]
    prompt: str | None = None
    frames: list[dict[str, float]]

    @model_validator(mode="after")
    def _shape(self) -> "MediaIn":
        if not self.frames:
            raise ValueError("frames must not be empty")
        if self.type == "image" and len(self.frames) != 1:
            raise ValueError("an image has exactly one frame")
        if self.source == "generation" and not self.prompt:
            raise ValueError("generated media must carry its prompt")
        return self


class MediaAccepted(BaseModel):
    id: str
    status: Literal["pending"]


class MediaOut(BaseModel):
    id: str
    status: str
    reasons: list[str]
