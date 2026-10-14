"""Apple's signature check, run for real.

The purchase and notification tests fake the App Store at its boundary. This file does
the opposite: it runs the real [AppleAppStore] — Apple's own `SignedDataVerifier`
underneath — against transactions signed by a certificate chain made up inside the test,
shaped the way Apple's is (three certificates, Apple's two marker extensions on the leaf
and the intermediate), and trusted only because the test hands its root over.

A suite that faked the signature check would pass with the check removed. This one
fails if a forged chain, a tampered payload or another app's transaction is ever
accepted.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.billing.app_store_client import AppleAppStore, apple_root_certificates
from app.billing.stores import (
    APPLE,
    PURCHASED,
    REVOKED,
    SANDBOX,
    ProofRejected,
    StoreUnreachable,
)

BUNDLE = "com.jeene.test"
APP_ID = 1234567890
#: Apple marks the certificates that may sign App Store data with these two extensions;
#: its library refuses a chain without them.
LEAF_MARKER = "1.2.840.113635.100.6.11.1"
INTERMEDIATE_MARKER = "1.2.840.113635.100.6.2.1"


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _certificate(subject, subject_key, issuer, issuer_key, *, ca: bool, marker: str | None):
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=not ca, key_cert_sign=ca, crl_sign=ca,
                          content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False,
                          encipher_only=False, decipher_only=False),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()),
                       critical=False)
    )
    if subject != issuer:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    if marker:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(marker), b"\x05\x00"),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


class Chain:
    """A root, an intermediate and a leaf, shaped like Apple's."""

    def __init__(self, label: str = "Test") -> None:
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        self.intermediate_key = ec.generate_private_key(ec.SECP256R1())
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        self.root = _certificate(f"{label} Root", self.root_key, f"{label} Root",
                                 self.root_key, ca=True, marker=None)
        self.intermediate = _certificate(f"{label} Intermediate", self.intermediate_key,
                                         f"{label} Root", self.root_key, ca=True,
                                         marker=INTERMEDIATE_MARKER)
        self.leaf = _certificate(f"{label} Leaf", self.leaf_key, f"{label} Intermediate",
                                 self.intermediate_key, ca=False, marker=LEAF_MARKER)

    @property
    def root_der(self) -> bytes:
        return self.root.public_bytes(serialization.Encoding.DER)

    def sign(self, claims: dict) -> str:
        chain = [base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode()
                 for c in (self.leaf, self.intermediate, self.root)]
        key = self.leaf_key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())
        return jwt.encode(claims, key, algorithm="ES256", headers={"x5c": chain})


TRUSTED = Chain()


def _transaction(**overrides) -> dict:
    now = int(time.time() * 1000)
    claims = {
        "transactionId": "2000000099",
        "originalTransactionId": "2000000099",
        "bundleId": BUNDLE,
        "productId": "pro_pass_30d",
        "purchaseDate": now,
        "originalPurchaseDate": now,
        "quantity": 1,
        "type": "Non-Renewing Subscription",
        "appAccountToken": "6B0D6A3E-2F0C-4A53-9D3E-1F2A3B4C5D6E",
        "inAppOwnershipType": "PURCHASED",
        "signedDate": now,
        "environment": "Sandbox",
        "transactionReason": "PURCHASE",
        "storefront": "IND",
        "storefrontId": "143467",
        "price": 199000,
        "currency": "INR",
    }
    claims.update(overrides)
    return claims


def _store(**kwargs) -> AppleAppStore:
    options = {"bundle_id": BUNDLE, "app_apple_id": APP_ID,
               "root_certificates": [TRUSTED.root_der], "online_checks": False}
    options.update(kwargs)
    return AppleAppStore(**options)


def _verify(store: AppleAppStore, signed: str):
    return asyncio.run(store.verify_transaction(signed))


def test_a_genuine_transaction_is_read_into_our_shape():
    purchase = _verify(_store(), TRUSTED.sign(_transaction()))

    assert purchase.store == APPLE
    assert purchase.ref == "2000000099"
    assert purchase.store_product_id == "pro_pass_30d"
    assert purchase.state == PURCHASED
    assert purchase.environment == SANDBOX
    # StoreKit writes UUIDs in capitals; the database compares them as UUIDs, and the
    # record keeps one spelling.
    assert purchase.account_token == "6b0d6a3e-2f0c-4a53-9d3e-1f2a3b4c5d6e"
    assert purchase.price_micros == 199_000_000
    assert purchase.currency == "INR"


def test_a_refunded_transaction_grants_nothing():
    signed = TRUSTED.sign(_transaction(revocationDate=int(time.time() * 1000),
                                       revocationReason=0))
    assert _verify(_store(), signed).state == REVOKED


def test_another_apps_transaction_is_refused_however_good_its_signature():
    with pytest.raises(ProofRejected):
        _verify(_store(), TRUSTED.sign(_transaction(bundleId="com.someone.else")))


def test_a_chain_that_does_not_lead_to_the_trusted_root_is_refused():
    impostor = Chain("Impostor")
    with pytest.raises(ProofRejected):
        _verify(_store(), impostor.sign(_transaction()))


def test_a_payload_edited_after_signing_is_refused():
    header, _payload, signature = TRUSTED.sign(_transaction()).split(".")
    claims = _transaction(productId="pro_pass_365d")
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(ProofRejected):
        _verify(_store(), f"{header}.{forged}.{signature}")


def test_xcodes_unsigned_transactions_are_refused_unless_development_says_so():
    xcode = jwt.encode(_transaction(environment="Xcode"), "x" * 32, algorithm="HS256")
    with pytest.raises(ProofRejected):
        _verify(_store(), xcode)
    assert _verify(_store(accept_xcode=True), xcode).environment == SANDBOX


def test_a_production_transaction_waits_for_the_app_id_rather_than_being_refused():
    signed = TRUSTED.sign(_transaction(environment="Production"))
    with pytest.raises(StoreUnreachable):
        _verify(_store(app_apple_id=None), signed)
    assert _verify(_store(), signed).environment == "production"


def test_a_notification_and_the_transaction_inside_it_are_both_verified():
    transaction = TRUSTED.sign(_transaction())
    notification = TRUSTED.sign({
        "notificationType": "ONE_TIME_CHARGE",
        "notificationUUID": str(uuid.uuid4()),
        "version": "2.0",
        "signedDate": int(time.time() * 1000),
        "data": {"environment": "Sandbox", "bundleId": BUNDLE, "appAppleId": APP_ID,
                 "signedTransactionInfo": transaction},
    })
    note = asyncio.run(_store().verify_notification(notification))

    assert note.kind == "ONE_TIME_CHARGE"
    assert note.purchase is not None and note.purchase.ref == "2000000099"


def test_a_notification_wrapping_a_forged_transaction_is_refused():
    forged = Chain("Impostor").sign(_transaction())
    notification = TRUSTED.sign({
        "notificationType": "ONE_TIME_CHARGE",
        "notificationUUID": str(uuid.uuid4()),
        "version": "2.0",
        "signedDate": int(time.time() * 1000),
        "data": {"environment": "Sandbox", "bundleId": BUNDLE, "appAppleId": APP_ID,
                 "signedTransactionInfo": forged},
    })
    with pytest.raises(ProofRejected):
        asyncio.run(_store().verify_notification(notification))


def test_the_committed_root_is_apples_g3():
    """Pinned: a different file here would make every real purchase fail, or worse."""
    roots = apple_root_certificates()
    assert len(roots) == 1
    root = x509.load_der_x509_certificate(roots[0])
    assert root.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == \
        "Apple Root CA - G3"
    assert root.fingerprint(hashes.SHA256()).hex(":").upper() == (
        "63:34:3A:BF:B8:9A:6A:03:EB:B5:7E:9B:3F:5F:A7:BE:"
        "7C:4F:5C:75:6F:30:17:B3:A8:C4:88:C3:65:3E:91:79"
    )
