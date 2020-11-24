import enum
import uuid
from datetime import datetime, timezone, timedelta

import cryptography
from sqlalchemy import (
    Column,
    Enum,
    DateTime,
    String,
    ForeignKey,
    LargeBinary,
    TypeDecorator,
    Integer,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .identifier import Identifier
from .authorization import AuthorizationStatus, Authorization
from .challenge import Challenge
from .base import Serializer, Entity
from ..util import url_for, names_of


class CSRType(TypeDecorator):
    """x509 Certificate as PEM"""

    impl = LargeBinary

    def process_bind_param(self, value, dialect):
        if value:
            return value.public_bytes(
                encoding=cryptography.hazmat.primitives.serialization.Encoding.PEM
            )
        return value

    def process_result_value(self, value, dialect):
        if value:
            return cryptography.x509.load_pem_x509_csr(value)
        return value


class OrderStatus(str, enum.Enum):
    # subclassing str simplifies json serialization using json.dumps
    PENDING = "pending"
    READY = "ready"
    PROCESSING = "processing"
    VALID = "valid"
    INVALID = "invalid"


class Order(Entity, Serializer):
    __tablename__ = "orders"
    __serialize__ = __diff__ = frozenset(["status", "expires", "notBefore", "notAfter"])
    __mapper_args__ = {
        "polymorphic_identity": "order",
    }

    _entity = Column(Integer, ForeignKey("entities.entity"), nullable=False, index=True)

    order_id = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, unique=True
    )
    proxied_url = Column(String, nullable=True, unique=True)
    status = Column("status", Enum(OrderStatus), nullable=False)
    expires = Column(DateTime(timezone=True), nullable=False)
    identifiers = relationship(
        "Identifier",
        cascade="all, delete",
        lazy="joined",
        foreign_keys="Identifier.order_id",
    )
    notBefore = Column(DateTime(timezone=True))
    notAfter = Column(DateTime(timezone=True))
    account_kid = Column(String, ForeignKey("accounts.kid"), nullable=False)
    account = relationship("Account", back_populates="orders", foreign_keys=account_kid)
    certificate = relationship(
        "Certificate",
        uselist=False,
        single_parent=True,
        back_populates="order",
        lazy="joined",
        foreign_keys="Certificate.order_id",
    )
    csr = Column(CSRType)

    def url(self, request):
        return url_for(request, "orders", id=str(self.order_id))

    def finalize_url(self, request):
        return url_for(request, "finalize-order", id=str(self.order_id))

    def certificate_url(self, request):
        return url_for(request, "certificate", id=str(self.certificate.certificate_id))

    def validate_csr(self, csr):
        identifiers = set(identifier.value.lower() for identifier in self.identifiers)

        return identifiers == names_of(csr, lower=True)

    async def validate(self):
        if self.status != OrderStatus.PENDING:
            return self.status

        if datetime.now(timezone.utc) > self.expires:
            self.status = OrderStatus.INVALID
            return self.status

        for identifier in self.identifiers:
            if identifier.authorization.status == AuthorizationStatus.INVALID:
                self.status = OrderStatus.INVALID
                break
            if not identifier.authorization.is_valid():
                break
        else:
            self.status = OrderStatus.READY

        return self.status

    def serialize(self, request=None):
        d = Serializer.serialize(self)
        d["identifiers"] = Serializer.serialize_list(self.identifiers)

        # Section on which authorizations to include:
        # https://tools.ietf.org/html/rfc8555#section-7.1.3
        def show_authz(authorization) -> bool:
            if self.status in (OrderStatus.VALID, OrderStatus.INVALID):
                return authorization.is_valid()
            else:  # self.status in (OrderStatus.PENDING, OrderStatus.PROCESSING, OrderStatus.READY):
                return (
                    authorization.status == AuthorizationStatus.PENDING
                    or authorization.is_valid()
                )

        d["authorizations"] = [
            identifier.authorization.url(request)
            for identifier in self.identifiers
            if show_authz(identifier.authorization)
        ]

        d["finalize"] = self.finalize_url(request)

        if self.status == OrderStatus.VALID:
            d["certificate"] = self.certificate_url(request)

        return d

    @classmethod
    def from_obj(cls, account, obj, challenge_types):
        identifiers = [
            Identifier.from_obj(identifier) for identifier in obj.identifiers
        ]

        for identifier in identifiers:
            identifier.authorization = Authorization.for_identifier(identifier)
            identifier.authorization.challenges = Challenge.create_types(
                challenge_types
            )

        order = Order(
            expires=datetime.now(timezone.utc) + timedelta(days=7),
            status=OrderStatus.PENDING,
            account=account,
            identifiers=identifiers,
        )

        return order
