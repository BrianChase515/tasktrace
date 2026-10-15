from tasktrace.updater import is_newer, parse_version, same_signer

from tasktrace.updater import STABLE_CERT

CERT = f'identifier "io.github.brianchase515.tasktrace" and certificate leaf = H"{STABLE_CERT}"'
OTHER = 'identifier "io.github.brianchase515.tasktrace" and certificate leaf = H"ff00ff00"'
ADHOC = 'cdhash H"0123456789"'


def test_versions():
    assert parse_version("0.5.0") == (0, 5, 0)
    assert is_newer("0.5.0", "0.4.1") and is_newer("0.10.0", "0.9.9")
    assert not is_newer("0.4.1", "0.4.1") and not is_newer("0.4.0", "0.4.1")


def test_same_signer_rules():
    assert same_signer(CERT, CERT)
    assert not same_signer(CERT, OTHER)  # a different certificate can never take over
    assert not same_signer(CERT, ADHOC)
    assert same_signer(ADHOC, CERT)  # moving off a temporary signature is allowed
    assert same_signer(OTHER, CERT)  # e.g. a build signed before the project certificate existed
    assert same_signer(None, CERT)
    assert not same_signer(CERT, None)
