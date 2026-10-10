# -*- coding: utf-8 -*-
"""
Pydantic v2 Validation Schemas for API requests and responses.
"""

import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator

VALID_REGIONS = {
    "ME", "IND", "BD", "SG", "TH", "PH", "VN", "MY", "ID",
    "HK", "TW", "PK", "CIS", "BR", "NA", "SAC", "EUROPE"
}

UID_REGEX = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


class AccountCreateRequest(BaseModel):
    uid: str = Field(..., min_length=3, max_length=64, description="Guest/Login UID")
    password: str = Field(..., min_length=3, max_length=256, description="Account password/secret")
    display_name: Optional[str] = Field(default=None, max_length=80)
    region: str = Field(default="ME", max_length=16)
    mode_id: int = Field(default=1, ge=1, le=20)
    map_id: int = Field(default=1, ge=1, le=20)
    notes: Optional[str] = Field(default="", max_length=500)

    @field_validator("uid")
    @classmethod
    def validate_uid(cls, v: str) -> str:
        v = v.strip()
        if not UID_REGEX.match(v):
            raise ValueError("UID must be 3-64 alphanumeric characters, hyphens, or underscores.")
        return v

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in VALID_REGIONS:
            raise ValueError(f"Unsupported region '{v}'. Valid regions: {', '.join(sorted(VALID_REGIONS))}")
        return v


class BulkAccountCreateRequest(BaseModel):
    raw_lines: str = Field(..., min_length=3, max_length=50000, description="Lines of UID:PASSWORD or UID,PASSWORD,REGION,DISPLAY_NAME")
    default_region: str = Field(default="ME")

    @field_validator("default_region")
    @classmethod
    def validate_default_region(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in VALID_REGIONS:
            raise ValueError(f"Unsupported region '{v}'.")
        return v


class AccountUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    password: Optional[str] = Field(default=None, max_length=256, description="Leave empty to keep existing encrypted password")
    region: Optional[str] = Field(default=None, max_length=16)
    mode_id: Optional[int] = Field(default=None, ge=1, le=20)
    map_id: Optional[int] = Field(default=None, ge=1, le=20)
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in VALID_REGIONS:
            raise ValueError(f"Unsupported region '{v}'.")
        return v


class BulkActionRequest(BaseModel):
    account_ids: List[str] = Field(..., min_length=1, max_length=200)
    action: str = Field(..., description="One of: start, stop, restart, delete, change_region, clear_errors")
    region: Optional[str] = Field(default=None)

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"start", "stop", "restart", "delete", "change_region", "clear_errors"}
        v = v.strip().lower()
        if v not in allowed:
            raise ValueError(f"Invalid bulk action '{v}'. Allowed: {', '.join(sorted(allowed))}")
        return v

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in VALID_REGIONS:
            raise ValueError(f"Unsupported region '{v}'.")
        return v


class MetadataImportItem(BaseModel):
    uid: str
    display_name: Optional[str] = None
    region: Optional[str] = "ME"
    password: Optional[str] = None
    mode_id: Optional[int] = 1
    map_id: Optional[int] = 1
    notes: Optional[str] = ""


class MetadataImportRequest(BaseModel):
    accounts: List[Dict[str, Any]] = Field(..., min_length=1, max_length=500)
    default_region: str = Field(default="ME")
