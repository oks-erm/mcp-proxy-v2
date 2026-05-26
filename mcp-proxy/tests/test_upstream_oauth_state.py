"""OAuth state token sign/verify."""

import pytest
from upstream_oauth.state import sign_oauth_state, verify_oauth_state


def test_sign_verify_roundtrip():
    t = sign_oauth_state({"user_id": "u1", "server_id": "srv", "v": "verifier-code-verifier"})
    d = verify_oauth_state(t)
    assert d["user_id"] == "u1"
    assert d["server_id"] == "srv"
    assert d["v"] == "verifier-code-verifier"


def test_verify_tampered_fails():
    t = sign_oauth_state({"user_id": "u1", "server_id": "srv", "v": "v"})
    bad = t[:-4] + "xxxx"
    with pytest.raises(ValueError):
        verify_oauth_state(bad)
