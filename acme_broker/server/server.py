import asyncio
import json
import logging
import typing
from typing import Any, Optional

import acme.messages
import josepy
from aiohttp import web
from aiohttp.helpers import sentinel
from aiohttp.typedefs import JSONEncoder, LooseHeaders
from aiohttp.web_middlewares import middleware
from cryptography.exceptions import InvalidSignature

from acme_broker import models
from acme_broker.database import Database
from acme_broker.util import generate_nonce, sha256_hex_digest, serialize_pubkey, url_for

logger = logging.getLogger(__name__)


async def handle_get(request):
    return web.Response(status=405)


class AcmeResponse(web.Response):
    def __init__(self, *args, nonce, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers.update({'Replay-Nonce': nonce, 'Cache-Control': 'no-store'})

    @staticmethod
    def json(data: Any = sentinel, *, nonce=None,
             text: str = None,
             body: bytes = None,
             status: int = 200,
             reason: Optional[str] = None,
             headers: LooseHeaders = None,
             content_type: str = 'application/json',
             dumps: JSONEncoder = json.dumps) -> web.Response:
        if data is not sentinel:
            if text or body:
                raise ValueError("only one of data, text, or body should be specified")
            else:
                text = dumps(data)
        return AcmeResponse(text=text, body=body, status=status, reason=reason,
                            headers=headers, content_type=content_type, nonce=nonce)


class AcmeCA:
    def __init__(self, host, base_route='/acme'):
        self._host = host
        self._base_route = base_route

        self.main_app = web.Application()
        self.ca_app = web.Application(middlewares=[self._error_middleware])

        self.ca_app.add_routes([
            web.post('/new-account', self._new_account, name='new-account'),
            web.head('/new-nonce', self._new_nonce, name='new-nonce'),
            web.post('/new-order', self._new_order, name='new-order'),
            web.post('/order/{id}', self._order, name='order'),
            web.post('/order/{id}/finalize', self._finalize_order, name='finalize-order'),
            web.post('/revoke-cert', self._revoke_cert, name='revoke-cert'),
            web.post('/accounts/{kid}', self._accounts, name='accounts'),
            web.post('/authz/{id}', self._authz, name='authz'),
            web.post('/challenge/{id}', self._challenge, name='challenge'),

        ])
        self.ca_app.router.add_route('GET', '/new-nonce', self._new_nonce)

        # catch-all get
        self.ca_app.router.add_route('GET', '/{tail:.*}', handle_get),

        self.main_app.add_routes([
            web.get('/directory', self._get_directory, name='directory'),
            # catch-all get
            # web.get('/{tail:.*}', handle_get),
        ])
        self.main_app.add_subapp(base_route, self.ca_app)

        self._nonces = set()

        self._db: typing.Optional[Database] = None
        self._session = None

    @classmethod
    async def runner(cls, hostname='localhost', **kwargs):
        log_level = logging.getLevelName(kwargs.pop('log_level', logging.INFO))
        log_file = kwargs.pop('log_file', None)
        port = kwargs.pop('port', 8000)
        debug = kwargs.pop('debug', False)
        db_user = kwargs.pop('db_user')
        db_pass = kwargs.pop('db_pass')
        db_host = kwargs.pop('db_host')
        db_port = kwargs.pop('db_port', 5432)
        db_database = kwargs.pop('db_database')

        logging.basicConfig(filename=log_file, level=log_level)
        logger.debug("""Passed Args: Log level '%s'
                                Log file '%s', 
                                Port %d, 
                                Debug '%s',
                                DB-user '%s',
                                DB-pass %s,
                                DB-host '%s',
                                DB-port %d,
                                DB-database '%s'""", log_level, log_file, port,
                     debug, db_user,
                     '***' if db_pass else None,
                     db_host, db_port, db_database)

        ca = AcmeCA(host=f'http://{hostname}:{port}', base_route='/acme')
        db = Database(db_user, db_pass, db_host, db_port, db_database)

        await db.begin()

        ca._db = db
        ca._session = db.session

        runner = web.AppRunner(ca.main_app)
        await runner.setup()

        site = web.TCPSite(runner, hostname, port)
        await site.start()

        return runner, ca

    def _issue_nonce(self):
        nonce = generate_nonce()
        logger.debug('Storing new nonce %s', nonce)
        self._nonces.add(nonce)
        return nonce

    def _verify_nonce(self, nonce):
        if nonce in self._nonces:
            logger.debug('Successfully verified nonce %s', nonce)
            self._nonces.remove(nonce)
        else:
            raise acme.messages.Error.with_code('badNonce', detail=nonce)

    async def _verify_request(self, request, key_auth=False):
        logger.debug('Verifying request')

        data = await request.text()
        jws = acme.jws.JWS.json_loads(data)
        sig = jws.signature

        # TODO: send error if verification unsuccessful
        protected = json.loads(sig.protected)

        nonce = protected.get('nonce', None)
        self._verify_nonce(nonce)

        assert (sig.combined.jwk is not None) ^ (
                sig.combined.kid is not None)  # Check whether we have *either* a jwk or a kid
        logger.debug('Request has a %s', 'jwk' if sig.combined.jwk else 'kid')

        if key_auth:
            try:
                jws.verify(sig.combined.jwk)
            except InvalidSignature:
                raise acme.messages.Error.with_code('badPublicKey')
            else:
                kid = None
        elif sig.combined.kid:
            kid = sig.combined.kid.split('/')[-1]
        else:
            raise acme.messages.Error.with_code('malformed')

        return jws, kid

    async def _get_directory(self, request):
        directory = acme.messages.Directory({
            'newAccount': url_for(request, 'new-account'),
            'newNonce': url_for(request, 'new-nonce'),
            'newOrder': url_for(request, 'new-order'),
            'revokeCert': url_for(request, 'revoke-cert'),
        })

        return AcmeResponse.json(directory.to_json(), nonce=self._issue_nonce())

    async def _new_nonce(self, request):
        return AcmeResponse(status=204, headers={
            'Cache-Control': 'no-store',
        }, nonce=self._issue_nonce())

    async def _new_account(self, request):
        jws, _ = await self._verify_request(request, key_auth=True)
        reg = acme.messages.Registration.json_loads(jws.payload)

        key = jws.signature.combined.jwk.key

        async with self._session() as session:
            account = await self._db.get_account(session, key)

            if account:
                if account.status != models.AccountStatus.VALID:
                    raise acme.messages.Error.with_code('unauthorized')
                else:
                    return AcmeResponse.json(account.serialize(), nonce=self._issue_nonce(),
                                             headers={'Location': url_for(request, 'accounts', account=account)})
            else:
                if reg.only_return_existing:
                    raise acme.messages.Error.with_code('accountDoesNotExist')
                elif not reg.terms_of_service_agreed:
                    # TODO: make available and link to ToS
                    raise acme.messages.Error(typ='urn:ietf:params:acme:error:termsOfServiceNotAgreed',
                                              title='The client must agree to the terms of service.')
                else:  # create new account
                    new_account = models.Account(key=key, kid=sha256_hex_digest(serialize_pubkey(key)),
                                                 status=models.AccountStatus.VALID,
                                                 contact=json.dumps(reg.contact))
                    serialized = new_account.serialize()
                    session.add(new_account)
                    kid = new_account.kid
                    await session.commit()
                    return AcmeResponse.json(serialized, status=201, nonce=self._issue_nonce(), headers={
                        'Location': url_for(request, 'accounts', kid=kid)
                    })

    async def _accounts(self, request):
        jws, kid = await self._verify_request(request)
        kid = request.match_info['kid']

        assert url_for(request, 'accounts', kid=kid) == jws.signature.combined.kid

        async with self._session() as session:
            account = await self._db.get_account(session, kid=kid)
            if not account:
                logger.info('Could not find account with kid %s', kid)
                raise acme.messages.Error.with_code('accountDoesNotExist')

            upd = acme.messages.Registration.json_loads(jws.payload)

            if contact := upd.contact:
                logger.debug('Updating contact info for account %s: %s', kid, contact)
                account.contact = json.dumps(contact)

            serialized = account.serialize()

            await session.commit()
            return AcmeResponse.json(serialized, status=200, nonce=self._issue_nonce())

    async def _new_order(self, request):
        jws, kid = await self._verify_request(request)
        obj = acme.messages.NewOrder.json_loads(jws.payload)

        async with self._session() as session:
            account = await self._db.get_account(session, kid=kid)
            order = models.Order.from_obj(account, obj)
            session.add(order)

            for identifier in order.identifiers:
                authorization = models.Authorization.from_identifier(identifier)
                challenges = models.Challenge.all_challenges_from_authz(authorization)
                session.add(authorization)

            await session.flush()
            serialized = order.serialize(request=request)

            await session.commit()

        return AcmeResponse.json(serialized, status=201, nonce=self._issue_nonce())

    async def _authz(self, request):
        jws, kid = await self._verify_request(request)
        authz_id = request.match_info['id']

        async with self._session() as session:
            authorization = await self._db.get_authz(session, kid, authz_id)
            await authorization.finalize(session)
            serialized = authorization.serialize(request=request)
            await session.commit()

        return AcmeResponse.json(serialized, nonce=self._issue_nonce(), status=200)

    async def _challenge(self, request):
        jws, kid = await self._verify_request(request)
        challenge_id = request.match_info['id']

        async with self._session() as session:
            challenge = await self._db.get_challenge(session, kid, challenge_id)
            # TODO: validate challenge, simulate HTTP request by sleeping
            await asyncio.sleep(1)
            challenge.status = models.ChallengeStatus.VALID
            serialized = challenge.serialize(request=request)
            await session.commit()

        return AcmeResponse.json(serialized, nonce=self._issue_nonce(), status=200)

    async def _revoke_cert(self, request):
        jws, kid = await self._verify_request(request, key_auth=True)

        return AcmeResponse(nonce=self._issue_nonce(), status=404)

    async def _order(self, request):
        jws, kid = await self._verify_request(request, key_auth=False)
        order_id = request.match_info['id']

        async with self._session() as session:
            order = await self._db.get_order(session, kid, order_id)
            _ = await order.finalize(session)
            serialized = order.serialize(request)

            await session.commit()

        return AcmeResponse.json(serialized, nonce=self._issue_nonce(), status=200)

    async def _finalize_order(self, request):
        jws, kid = await self._verify_request(request, key_auth=False)
        order_id = request.match_info['id']
        # obj = acme.messages.CertificateRequest.json_loads(jws.payload)

        async with self._session() as session:
            order = await self._db.get_order(session, kid, order_id)
            status = await order.finalize(session)

            if status != models.OrderStatus.READY:
                raise acme.messages.Error.with_code('orderNotReady')

            await session.commit()

        obj = json.loads(jws.payload)
        csr = josepy.decode_csr(obj['csr'])

        return AcmeResponse(nonce=self._issue_nonce(), status=404)

    @middleware
    async def _error_middleware(self, request, handler):
        """
        Converts errors thrown in handlers to ACME compliant JSON and
        attaches the specified status code to the response.
        """
        try:
            response = await handler(request)
        except acme.messages.errors.Error as error:
            status = 400
            if error.code == 'orderNotReady':
                status = 403

            return AcmeResponse.json(error.json_dumps(), status=status, nonce=self._issue_nonce(),
                                     content_type='application/problem+json')
        else:
            return response


class AcmeProxy(AcmeCA):
    pass


class AcmeBroker(AcmeCA):
    pass
