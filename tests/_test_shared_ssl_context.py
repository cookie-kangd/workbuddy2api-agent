"""Outbound TLS must reuse one process-wide SSLContext.

http.client builds a fresh default context on every HTTPSConnection when
context=None: it calls ssl._create_default_https_context() ->
create_default_context(), which re-parses the system CA bundle. Measured in the
v1.7.2 container:

    create_default_context() 平均 20.4 ms  (CA 憑證 121 張)

And because OpenSSL's client-side session cache lives on the SSLContext, a fresh
context per connection also gives up TLS session resumption, so every handshake
degrades into a full handshake. With 45 accounts each routed through its own
proxy that is 45 pool keys, and the cost is paid again every time a connection
expires and is rebuilt.

These tests pin two things: the context is built once and shared, and its
verification settings are the ones create_default_context() ships - an
optimisation here must not relax a single check.
"""
import os
import ssl
import sys

CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIBVTCB/KADAgECAhRjBYVVqXLLq9XPGJLkh4vjllA88jAKBggqhkjOPQQDAjAX
MRUwEwYDVQQDDAx3Yi1wb29sLXRlc3QwIBcNMjYwMTAxMDAwMDAwWhgPMjEyNTEy
MDgwMDAwMDBaMBcxFTATBgNVBAMMDHdiLXBvb2wtdGVzdDBZMBMGByqGSM49AgEG
CCqGSM49AwEHA0IABO9/UQG2Y7AHaVYraezrSQRqTF7CNRvyB92HN0Z1UcZihDrJ
gdfNQ7roarcMLQp+iF/4Mptz9aK6JVpxHrhBDCajJDAiMCAGA1UdEQQZMBeCCWxv
Y2FsaG9zdIcEfwAAAYcEZGznVDAKBggqhkjOPQQDAgNIADBFAiEA3pmrNdsS5rsF
g61sIWJjuMTnhITTEpfEirLsDswzTJsCIHtKSt51ZnqleBoee2sC4uAZmSNogyzW
un5auKxKku42
-----END CERTIFICATE-----
"""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402  (its own data root)
# Its own directories, removed when this process exits.
_TMP = isolated_data_dirs("wb-shared-ssl-")

# A host-level WB_CA_BUNDLE would otherwise change the "system CA" baseline
# this suite compares against.
os.environ.pop("WB_CA_BUNDLE", None)
import wb_upstream_pool as U  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(detail) if detail else ""))


print("-- one context, built once --")

# Lazy on purpose: a process that never makes an outbound call (the test
# suites, a panel-only run) must not pay the 20ms CA parse at import time.
check("importing the module did not build the context yet",
      U._SSL_CONTEXT is None, "already built: %r" % (U._SSL_CONTEXT,))

first = U.shared_ssl_context()
check("returns an SSLContext", isinstance(first, ssl.SSLContext), type(first))
check("second call returns the identical object",
      U.shared_ssl_context() is first)
check("a third call too", U.shared_ssl_context() is first)
check("the module-level slot is the same object", U._SSL_CONTEXT is first)

print("-- verification is not relaxed --")

check("check_hostname is on", first.check_hostname is True,
      "check_hostname=%r" % (first.check_hostname,))
check("verify_mode is CERT_REQUIRED",
      first.verify_mode == ssl.CERT_REQUIRED,
      "verify_mode=%r" % (first.verify_mode,))
check("the system CA bundle is loaded", len(first.get_ca_certs()) > 10,
      "%d certs" % len(first.get_ca_certs()))

# The settings must match a freshly built default context exactly, so a future
# edit that "just shares a context" cannot quietly widen what is accepted.
fresh = ssl.create_default_context()
check("check_hostname matches a fresh default context",
      first.check_hostname == fresh.check_hostname)
check("verify_mode matches a fresh default context",
      first.verify_mode == fresh.verify_mode)
check("minimum_version matches a fresh default context",
      first.minimum_version == fresh.minimum_version)
check("the same CA set as a fresh default context",
      {c["subject"] for c in first.get_ca_certs()}
      == {c["subject"] for c in fresh.get_ca_certs()})

print("-- _new_connection hands the shared context to HTTPS --")

PROXY = {"host": "127.0.0.1", "port": "3128", "user": "", "password": ""}

conn = U._new_connection("https", "example.com", 443, None, 10)
check("direct https uses the shared context",
      conn._context is first, "context=%r" % (conn._context,))

proxied = U._new_connection("https", "example.com", 443, PROXY, 10)
check("https through a proxy uses the shared context",
      proxied._context is first, "context=%r" % (proxied._context,))
check("the CONNECT tunnel target is still set",
      getattr(proxied, "_tunnel_host", None) == "example.com",
      "tunnel_host=%r" % (getattr(proxied, "_tunnel_host", None),))
check("the proxy port is honoured for the tunnel",
      proxied.port == 3128, "port=%r" % (proxied.port,))

plain = U._new_connection("http", "example.com", 80, None, 10)
check("plain http gets no TLS context at all",
      not isinstance(plain, ssl.SSLContext)
      and getattr(plain, "_context", None) is None)

print("-- the pooled class is what the factory actually builds --")
import http.client  # noqa: E402

check("_PooledHTTPSConnection subclasses HTTPSConnection",
      issubclass(U._PooledHTTPSConnection, http.client.HTTPSConnection))
check("the object built for direct https is a pooled one",
      isinstance(conn, U._PooledHTTPSConnection))
check("the object built for the proxy path is a pooled one",
      isinstance(proxied, U._PooledHTTPSConnection))

print("-- the stdlib seam still works: a swapped factory invalidates the cache --")

before = U.shared_ssl_context()

custom = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
custom.check_hostname = False          # must precede verify_mode: CERT_NONE
custom.verify_mode = ssl.CERT_NONE     # is rejected while check_hostname is on

# Patch both spellings, the way the pool suite does, so this is version-agnostic:
# 3.13+ goes through http.client._create_https_context, 3.9-3.12 through
# ssl._create_default_https_context.
saved = {}
for module, name in ((http.client, "_create_https_context"),
                     (ssl, "_create_default_https_context")):
    if hasattr(module, name):
        saved[(module, name)] = getattr(module, name)
        setattr(module, name, lambda *a, **k: custom)

try:
    swapped = U.shared_ssl_context()
    check("swapping the default factory invalidates the cache",
          swapped is custom, "got %r" % (swapped,))
    check("the new context is not the previously cached one",
          swapped is not before)
finally:
    for (module, name), fn in saved.items():
        setattr(module, name, fn)

# Restoring the factory invalidates the cache again, so the next call rebuilds
# - a fresh object, not the one from before the swap. What matters is that it
# verifies again and is cached once more.
restored = U.shared_ssl_context()
check("restoring the factory brings back a verifying context",
      restored is not custom and restored.check_hostname is True
      and restored.verify_mode == ssl.CERT_REQUIRED,
      "check_hostname=%r verify_mode=%r" % (restored.check_hostname,
                                            restored.verify_mode))
check("and that rebuilt context is cached again",
      U.shared_ssl_context() is restored)
check("the relaxed context is gone from the cache",
      U._SSL_CONTEXT is not custom)

print("-- WB_CA_BUNDLE is trusted on top of the system roots --")

ca_file = os.path.join(_TMP.name, "private-ca.pem")
with open(ca_file, "w", encoding="ascii", newline="\n") as fh:
    fh.write(CERT_PEM)

try:
    os.environ["WB_CA_BUNDLE"] = os.path.join(_TMP.name, "does-not-exist.pem")
    try:
        U.shared_ssl_context()
    except RuntimeError as exc:
        check("a missing WB_CA_BUNDLE is a clear error",
              "WB_CA_BUNDLE" in str(exc) and "does-not-exist" in str(exc), exc)
    else:
        check("a missing WB_CA_BUNDLE is a clear error", False)

    os.environ["WB_CA_BUNDLE"] = ca_file
    bundled = U.shared_ssl_context()
    check("setting WB_CA_BUNDLE invalidates the cached context",
          bundled is not restored, "same object as before the bundle")
    base_certs = ssl.create_default_context().cert_store_stats()["x509"]
    bundled_certs = bundled.cert_store_stats()["x509"]
    check("the private certificate is loaded",
          bundled_certs == base_certs + 1, (base_certs, bundled_certs))
    check("verification is still required with a bundle",
          bundled.check_hostname is True
          and bundled.verify_mode == ssl.CERT_REQUIRED,
          "check_hostname=%r verify_mode=%r" % (bundled.check_hostname,
                                                bundled.verify_mode))
finally:
    os.environ.pop("WB_CA_BUNDLE", None)
    U.shared_ssl_context()  # restore the system-only cache

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
