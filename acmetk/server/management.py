import collections

import aiohttp_jinja2
import cryptography
import sqlalchemy
import sqlalchemy.ext.asyncio
import sqlalchemy.dialects.postgresql
from sqlalchemy import select
from sqlalchemy.orm import selectinload, defer, defaultload
from sqlalchemy.sql import text

import aiohttp.web


from acmetk.models import (
    Change,
    Account,
    Order,
    Identifier,
    Certificate,
    Challenge,
    Authorization,
)
from acmetk.models.base import Entity
from acmetk.server.routes import routes
from acmetk.util import PerformanceMeasurementSystem
from .pagination import paginate


class AcmeManagementMixin:
    MGMT_GROUP = "it-admins"
    GROUPS_HEADER = "x-forwarded-groups"
    _mgmt_auth = False

    async def _session(
        self, request: aiohttp.web.Request
    ) -> sqlalchemy.ext.asyncio.AsyncSession:
        pass

    @aiohttp.web.middleware
    async def mgmt_auth_middleware(self, request: aiohttp.web.Request, handler):
        """Middleware that checks whether the request has a x-auth-request-groups header
        :returns:
            * HTTP status code *403* if the header is missing
        """

        if self._mgmt_auth is False or not request.path.startswith("/mgmt"):
            return await handler(request)

        if (
            groups := request.headers.get(self.GROUPS_HEADER)
        ) is None or self.MGMT_GROUP not in groups.split(","):
            return aiohttp.web.Response(
                status=403,
                text=f"{type(self).__name__}: This service requires {self.GROUPS_HEADER} & {self.MGMT_GROUP}"
                " Please contact your system administrator.\n",
            )
        return await handler(request)

    @routes.get("/mgmt", name="mgmt-index")
    @aiohttp_jinja2.template("index.jinja2")
    async def management_index(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        import datetime

        pms = PerformanceMeasurementSystem(enable=request.query.get("pms", False))
        async with self._session(request) as session:
            now = datetime.datetime.now()
            start_date = now - datetime.timedelta(days=28)
            q = (
                select(
                    sqlalchemy.func.date_trunc("day", Change.timestamp).label("dateof"),
                    sqlalchemy.func.count(Change.change).label("totalof"),
                    sqlalchemy.func.count(sqlalchemy.distinct(Change._entity)).label(
                        "uniqueof"
                    ),
                    Entity.identity.label("actionof"),
                )
                .select_from(Change)
                .join(Entity, Entity.entity == Change._entity)
                .filter(Change.timestamp.between(start_date, now))
                .group_by(text("dateof"), Entity.identity)
            )
            async with pms.measure():
                r = await session.execute(q)

            s = collections.defaultdict(
                lambda: {
                    k: {"total": 0, "unique": 0}
                    for k in [
                        "identifier",
                        "account",
                        "order",
                        "challenge",
                        "authorization",
                        "certificate",
                    ]
                }
            )

            for m in r.mappings():
                s[m["dateof"].date()][m["actionof"]].update(
                    {
                        "total": m["totalof"],
                        "unique": m["uniqueof"],
                    }
                )

            statistics = []
            for i in sorted(s.keys(), reverse=True):
                statistics.append(
                    (
                        i,
                        s[i],
                        sum(map(lambda x: x["total"], s[i].values())),
                        sum(map(lambda x: x["unique"], s[i].values())),
                    )
                )
            return {"statistics": statistics, "pms": pms}

    @routes.get("/mgmt/httpbin/headers", name="mgmt-httpbin-headers")
    async def management_httpbin_headers(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """
        c.f. https://httpbingo.org/

        :param request:
        :return:
        """
        d = {k: request.headers.getall(k) for k in request.headers.keys()}
        return aiohttp.web.json_response(d)

    @routes.get("/mgmt/changes", name="mgmt-changes")
    @aiohttp_jinja2.template("changes.jinja2")
    async def management_changes(self, request: aiohttp.web.Request):
        pms = PerformanceMeasurementSystem(enable=request.query.get("pms", False))
        async with self._session(request) as session:
            f = []
            for value in request.query.getall("q", []):
                # JSON Patch query for value regex like
                # FIXME … though ' is taken care of " in q still breaks it but everything below does not work either
                v = sqlalchemy.String().literal_processor(
                    dialect=session._proxied.bind.dialect
                )(value=value)
                v = v.replace('"', ".")
                f.append(
                    Change.data.op("@@")(
                        sqlalchemy.text(
                            f"'$[*].value like_regex \"{v[1:-1]}\"'::jsonpath"
                        )
                    )
                )

                # This text() construct doesn't define a bound parameter named 'n'
                #                f.append(Change.data.op('@@')(
                #                   sqlalchemy.text('\'$[*].value like_regex \"\:n\"\'::jsonpath').bindparams(n=value)))

                # the server expects 0 arguments for this query, 1 was passed
                #                f.append(Change.data.op('@@')(
                #                    sqlalchemy.text('\'$[*].value like_regex ":n"\'::jsonpath').params(n=value)))

                # the resultset is incomplete
                #                f.append(Change.data.op('@@')(
                #                    sqlalchemy.text('\'$[*].value like_regex \"\:n\"\'::jsonpath').params(n=value)))

                # remote host ipaddress cidr query
                try:
                    import ipaddress

                    ipaddress.ip_interface(value)
                    f.append(
                        Change.remote_host.op("<<=")(
                            sqlalchemy.cast(value, sqlalchemy.dialects.postgresql.INET)
                        )
                    )
                except ValueError:
                    pass

            q = select(sqlalchemy.func.count(Change.change))
            if f:
                q = q.filter(sqlalchemy.or_(*f))

            async with pms.measure():
                total = (await session.execute(q)).scalars().first()

            q = select(Change).options(
                selectinload(Change.entity).selectin_polymorphic([Account]),
                selectinload(Change.entity.of_type(Authorization))
                .selectinload(Authorization.identifier)
                .selectinload(Identifier.order)
                .selectinload(Order.account),
                selectinload(Change.entity.of_type(Challenge))
                .selectinload(Challenge.authorization)
                .selectinload(Authorization.identifier)
                .selectinload(Identifier.order)
                .selectinload(Order.account),
                selectinload(Change.entity.of_type(Certificate))
                .selectinload(Certificate.order)
                .selectinload(Order.account),
                selectinload(Change.entity.of_type(Identifier))
                .selectinload(Identifier.order)
                .selectinload(Order.account),
                selectinload(Change.entity.of_type(Order)).selectinload(Order.account),
            )

            if f:
                q = q.filter(sqlalchemy.or_(*f))
            q = q.order_by(Change.change.desc())
            if f:
                limit = "limit"
            else:
                limit = Change.change
            page = await paginate(session, request, q, limit, total, pms)

            return {"changes": page.items, "page": page, "pms": pms}

    @routes.get("/mgmt/accounts", name="mgmt-accounts")
    @aiohttp_jinja2.template("accounts.jinja2")
    async def management_accounts(self, request: aiohttp.web.Request):
        pms = PerformanceMeasurementSystem(enable=request.query.get("pms", False))
        async with self._session(request) as session:
            q = select(sqlalchemy.func.count(Account.account_id))
            async with pms.measure():
                total = (await session.execute(q)).scalars().first()

            q = (
                select(Account)
                .options(
                    selectinload(Account.orders),
                    selectinload(Account.changes).selectinload(Change.entity),
                )
                .order_by(Account._entity.desc())
            )

            page = await paginate(session, request, q, "limit", total, pms=pms)

            return {"accounts": page.items, "page": page, "pms": pms}

    @routes.get("/mgmt/accounts/{account}", name="mgmt-account")
    @aiohttp_jinja2.template("account.jinja2")
    async def management_account(self, request: aiohttp.web.Request):
        pms = PerformanceMeasurementSystem(enable=request.query.get("pms", False))
        account = request.match_info["account"]
        async with self._session(request) as session:
            q = (
                select(Account)
                .options(
                    selectinload(Account.orders)
                    .joinedload(Order.identifiers)
                    .joinedload(Identifier.authorization),
                    selectinload(Account.changes).joinedload(Change.entity),
                )
                .filter(Account.account_id == account)
            )
            async with pms.measure():
                a = await session.execute(q)
            a = a.scalars().first()
            return {
                "account": a,
                "orders": a.orders,
                "cryptography": cryptography,
                "pms": pms,
            }

    @routes.get("/mgmt/orders", name="mgmt-orders")
    @aiohttp_jinja2.template("orders.jinja2")
    async def management_orders(self, request: aiohttp.web.Request):
        pms = PerformanceMeasurementSystem(enable=request.query.get("pms", False))
        async with self._session(request) as session:
            q = select(sqlalchemy.func.count(Order.order_id))
            async with pms.measure():
                total = (await session.execute(q)).scalars().first()

            q = (
                select(Order)
                .options(
                    defer(Order.csr),
                    selectinload(Order.account).options(defer(Account.key)),
                    selectinload(Order.identifiers),
                    selectinload(Order.changes).options(
                        defer(Change.data),
                    ),
                )
                .order_by(Order._entity.desc())
            )

            page = await paginate(session, request, q, "limit", total)
            return {"orders": page.items, "page": page, "pms": pms}

    @routes.get("/mgmt/orders/{order}", name="mgmt-order")
    @aiohttp_jinja2.template("order.jinja2")
    async def management_order(self, request: aiohttp.web.Request):
        order = request.match_info["order"]
        pms = PerformanceMeasurementSystem(enable=request.query.get("pms", False))
        changes = []
        async with self._session(request) as session:
            q = (
                select(Order)
                .options(
                    defaultload(Order.csr),
                    selectinload(Order.account),
                    selectinload(Order.identifiers).options(
                        defaultload(Identifier.value),
                        selectinload(Identifier.authorization).options(
                            selectinload(Authorization.identifier).defaultload(
                                Identifier.value
                            ),
                            selectinload(Authorization.challenges)
                            .selectinload(Challenge.changes)
                            .selectinload(Change.entity),
                            selectinload(Authorization.changes).selectinload(
                                Change.entity
                            ),
                        ),
                        selectinload(Identifier.changes).selectinload(Change.entity),
                    ),
                    selectinload(Order.changes).selectinload(Change.entity),
                    selectinload(Order.certificate)
                    .selectinload(Certificate.changes)
                    .selectinload(Change.entity),
                )
                .filter(Order.order_id == order)
            )
            async with pms.measure():
                r = await session.execute(q)
            o = r.scalars().first()

            if o is None:
                from aiohttp.web_exceptions import HTTPNotFound

                raise HTTPNotFound()
            changes.extend(o.changes)

        async with pms.measure():
            for i in o.identifiers:
                changes.extend(i.changes)
                changes.extend(i.authorization.changes)
                for c in i.authorization.challenges:
                    changes.extend(c.changes)

            if o.certificate:
                changes.extend(o.certificate.changes)

            changes = sorted(changes, key=lambda x: x.timestamp, reverse=True)

        return {
            "order": o,
            "changes": changes,
            "pms": pms,
            "cryptography": cryptography,
        }

    @routes.get("/mgmt/certificates", name="mgmt-certificates")
    @aiohttp_jinja2.template("certificates.jinja2")
    async def management_certificates(self, request: aiohttp.web.Request):
        pms = PerformanceMeasurementSystem(enable=request.query.get("pms", False))
        async with self._session(request) as session:
            q = select(sqlalchemy.func.count(Certificate.certificate_id))
            async with pms.measure():
                total = (await session.execute(q)).scalars().first()

            q = (
                select(Certificate)
                .options(
                    defer(Certificate.cert),
                    selectinload(Certificate.changes).options(defer(Change.data)),
                    selectinload(Certificate.order).options(
                        defer(Order.csr),
                        selectinload(Order.identifiers),
                        selectinload(Order.account).options(defer(Account.key)),
                    ),
                )
                .order_by(Certificate._entity.desc())
            )

            page = await paginate(session, request, q, "limit", total, pms=pms)
            return {"certificates": page.items, "page": page, "pms": pms}

    @routes.get("/mgmt/certificates/{certificate}", name="mgmt-certificate")
    async def management_certificate(self, request: aiohttp.web.Request):
        certificate = request.match_info["certificate"]
        async with self._session(request) as session:
            q = (
                select(Certificate)
                .options(
                    selectinload(Certificate.changes),
                    selectinload(Certificate.order).selectinload(Order.account),
                )
                .filter(Certificate.certificate_id == certificate)
            )

            r = await session.execute(q)
            a = r.scalars().first()
            context = {"certificate": a.cert, "cryptography": cryptography}
            response = aiohttp_jinja2.render_template(
                "certificate.jinja2", request, context
            )
            response.content_type = "text"
            response.charset = "utf-8"
            return response

    @routes.get("/mgmt/orders/{order}/csr", name="mgmt-csr")
    async def management_csr(self, request: aiohttp.web.Request):
        order = request.match_info["order"]
        async with self._session(request) as session:
            q = (
                select(Order)
                .options(defaultload(Order.csr))
                .filter(Order.order_id == order)
            )

            r = await session.execute(q)
            a = r.scalars().first()
            context = {"csr": a.csr, "cryptography": cryptography}
            response = aiohttp_jinja2.render_template("csr.jinja2", request, context)
            response.content_type = "text"
            response.charset = "utf-8"
            return response
