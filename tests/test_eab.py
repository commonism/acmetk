import urllib.parse
import unittest
from unittest.mock import Mock
from cryptography.hazmat.primitives.asymmetric import rsa
from acme_broker.server.external_account_binding import ExternalAccountBinding


class TestEAB(unittest.TestCase):
    def setUp(self):
        self._eab = ExternalAccountBinding()

    def test_create(self):
        data = """-----BEGIN CERTIFICATE-----
MIIF9jCCBN6gAwIBAgIMILAmADIIoBt+NfL7MA0GCSqGSIb3DQEBCwUAMIGNMQsw
CQYDVQQGEwJERTFFMEMGA1UECgw8VmVyZWluIHp1ciBGb2VyZGVydW5nIGVpbmVz
IERldXRzY2hlbiBGb3JzY2h1bmdzbmV0emVzIGUuIFYuMRAwDgYDVQQLDAdERk4t
UEtJMSUwIwYDVQQDDBxERk4tVmVyZWluIEdsb2JhbCBJc3N1aW5nIENBMB4XDTE5
MDMxOTA4MjkzOVoXDTIyMDMxODA4MjkzOVowgYgxCzAJBgNVBAYTAkRFMRYwFAYD
VQQIDA1OaWVkZXJzYWNoc2VuMREwDwYDVQQHDAhIYW5ub3ZlcjEmMCQGA1UECgwd
TGVpYm5peiBVbml2ZXJzaXRhZXQgSGFubm92ZXIxDTALBgNVBAsMBExVSVMxFzAV
BgNVBAMMDk1hcmt1cyBLb2V0dGVyMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIB
CgKCAQEA0N/Xv7cd/EgUWebMielajetwne6dH6e7pASaJW0xdiSCRPJh072h/8mt
bqIls8Z5Gtnv7GRP5XkjpKXWR0YXxwTWL2mbYdsxes1ehvA48XUX57mCxvnYZlDv
h1AyjJnYVW8FU93D9zXNOPxxcW9LIZRIzLeXbo62i+lDUuHTx1JTqFiCrSWGcMwm
sovVAUoy2T0KEHmoTeL5Y0XUrrV6BH+eWgg+FG6FEFDBnRuVakIvOg3YZ04icEiL
OugCTYel8mLbb0uxDlnMDISyMzQfgukGclk/+aefU3rAmbPKxbaCjUDdfjMKeW4w
CLz/eRVTV81r9nxSG+Nbw/KhWN9EwQIDAQABo4ICVzCCAlMwQAYDVR0gBDkwNzAP
Bg0rBgEEAYGtIYIsAQEEMBEGDysGAQQBga0hgiwBAQQDCTARBg8rBgEEAYGtIYIs
AgEEAwkwCQYDVR0TBAIwADAOBgNVHQ8BAf8EBAMCBeAwHQYDVR0lBBYwFAYIKwYB
BQUHAwIGCCsGAQUFBwMEMB0GA1UdDgQWBBTII+AASk5YQ69R9PrPSTWu8K/XTTAf
BgNVHSMEGDAWgBRrOpiL+fJTidrgrbIyHgkf6Ko7dDAnBgNVHREEIDAegRxrb2V0
dGVyQGx1aXMudW5pLWhhbm5vdmVyLmRlMIGNBgNVHR8EgYUwgYIwP6A9oDuGOWh0
dHA6Ly9jZHAxLnBjYS5kZm4uZGUvZGZuLWNhLWdsb2JhbC1nMi9wdWIvY3JsL2Nh
Y3JsLmNybDA/oD2gO4Y5aHR0cDovL2NkcDIucGNhLmRmbi5kZS9kZm4tY2EtZ2xv
YmFsLWcyL3B1Yi9jcmwvY2FjcmwuY3JsMIHbBggrBgEFBQcBAQSBzjCByzAzBggr
BgEFBQcwAYYnaHR0cDovL29jc3AucGNhLmRmbi5kZS9PQ1NQLVNlcnZlci9PQ1NQ
MEkGCCsGAQUFBzAChj1odHRwOi8vY2RwMS5wY2EuZGZuLmRlL2Rmbi1jYS1nbG9i
YWwtZzIvcHViL2NhY2VydC9jYWNlcnQuY3J0MEkGCCsGAQUFBzAChj1odHRwOi8v
Y2RwMi5wY2EuZGZuLmRlL2Rmbi1jYS1nbG9iYWwtZzIvcHViL2NhY2VydC9jYWNl
cnQuY3J0MA0GCSqGSIb3DQEBCwUAA4IBAQAT8elr9UyROAQpgShWhnwSLtnXNL6Z
IuF/iVA4rxWNKaQMX56+nyHlhHMJnqoorMai4HaCZyjCRhvDCQY22VJ8KbJKq4Hq
aVRnrqUPGXy7PsJeKywn3MdVlLOPACzAn6n9rOut6uBpptG+8mPru0vOZB2Mk3r4
y20m9Dsb/7cZ3TAET84iPAmZf6W2VRIpxe5563MWc6iwO3J5CTEIxYfFH9gMj5Yk
uCZmAFE5abolxwWEanGiEwrYOy8H3KhrWwX7vA1hF1dG54BB5xKcEIbHzMUxuToi
oF45C4zWPYXCtz3rtFi7w9f6rf5OWiJZ01VZ4U+vbe/fSC7DgRMWUyWB
-----END CERTIFICATE-----"""
        URL = "http://localhost/new-account"
        request = Mock(headers={"X-SSL-CERT": urllib.parse.quote(data)}, url=URL)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        kid, hmac = self._eab.create(request, key.public_key())

        # signature = ""
        # self.assertTrue(self._eab.verify(kid, signature))
        self.assertFalse(self._eab.verify(kid + "x", hmac))
        self.assertFalse(self._eab.verify(kid, "x" + hmac))
