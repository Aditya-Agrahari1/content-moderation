from app.signing import sign, verify

SECRET = "s3cret"
BODY = b'{"media_id":"m","frames":[]}'


def test_valid_signature_hex_and_prefixed():
    signature = sign(SECRET, BODY)
    assert verify(SECRET, BODY, signature)
    assert verify(SECRET, BODY, f"sha256={signature}")
    assert verify(SECRET, BODY, signature.upper())


def test_rejected_signatures():
    signature = sign(SECRET, BODY)
    assert not verify(SECRET, BODY, None)
    assert not verify(SECRET, BODY, "")
    assert not verify(SECRET, BODY, "not-hex")
    assert not verify("wrong-secret", BODY, signature)
    assert not verify(SECRET, BODY.replace(b'"frames"', b'"frames" '), signature)  # whitespace changes the bytes
    assert not verify(SECRET, BODY + b"\n", signature)
    assert not verify(SECRET, BODY.replace(b"m", b"x", 1), signature)  # tampered body
