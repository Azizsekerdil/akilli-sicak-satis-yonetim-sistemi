"""Company, region and branch — the organisational backbone."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import AuthorMixin, Base, CodeNameMixin, SoftDeleteMixin, TimestampMixin, fk, pk


class Company(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """The operating company (single-tenant by default, multi-tenant ready)."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("code", name="uq_companies_code"),)

    id: Mapped[int] = pk()
    legal_name: Mapped[str | None] = mapped_column(String(255))
    tax_office: Mapped[str | None] = mapped_column(String(128))
    tax_number: Mapped[str | None] = mapped_column(String(32), index=True)
    mersis_no: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(96))
    district: Mapped[str | None] = mapped_column(String(96))
    country: Mapped[str] = mapped_column(String(64), default="Türkiye")
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(String(255))
    logo_path: Mapped[str | None] = mapped_column(String(512))
    currency: Mapped[str] = mapped_column(String(8), default="TRY")
    default_vat_rate: Mapped[float] = mapped_column(Float, default=20.0)

    regions: Mapped[list["Region"]] = relationship(back_populates="company", lazy="selectin")


class Region(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Sales region / territory.  Drives REGION-scoped data visibility."""

    __tablename__ = "regions"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_regions_company_code"),)

    id: Mapped[int] = pk()
    company_id: Mapped[int] = fk("companies.id")
    parent_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")
    manager_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(96))
    center_lat: Mapped[float | None] = mapped_column(Float)
    center_lng: Mapped[float | None] = mapped_column(Float)

    company: Mapped["Company"] = relationship(back_populates="regions", lazy="joined")
    parent: Mapped["Region | None"] = relationship(
        "Region", back_populates="children", remote_side="Region.id"
    )
    children: Mapped[list["Region"]] = relationship("Region", back_populates="parent")


class Branch(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Physical branch / depot site belonging to a region."""

    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_branches_company_code"),)

    id: Mapped[int] = pk()
    company_id: Mapped[int] = fk("companies.id")
    region_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(96))
    district: Mapped[str | None] = mapped_column(String(96))
    phone: Mapped[str | None] = mapped_column(String(32))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    is_headquarters: Mapped[bool] = mapped_column(Boolean, default=False)
