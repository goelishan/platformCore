from __future__ import annotations
import logging
from typing import Any

from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.config import ConfigException

from oncall import config as oncall_config

log=logging.getLogger(__name__)

REQUEST_TIMEOUT = 10      # seconds per HTTP request
PAGE_SIZE = 500           # objects per response
MAX_PAGES = 200           # refuse to loop forever rather than truncate quietly


def load_auth() -> str:
    try:
        kube_config.load_incluster_config()
        mode="in-cluster"
    except ConfigException:
        kube_config.load_kube_config()
        mode="kubeconfig"

    log.info("Kubernetes auth load via %s", mode)
    return mode


# ---- clients ---------------------------------------------------------------
# Auth loads lazily behind the accessors rather than being left to the entry point.
# An unconfigured client does not fail loudly: it defaults to localhost, so every
# request drains into a connection error and the collector reports unavailable
# forever. Silent blindness is the one failure this agent cannot afford, and it is
# invisible to the test suite because the mapping is pure and never builds a client.
#
# These three accessors are the only route to an API object, which makes them the
# single choke point where the guarantee can be enforced once.


_auth_loaded = False


def _ensure_auth() -> None:
    """Idempotent, and latched only after a successful load.

    Setting the flag before the call would let one bad startup — a kubeconfig not yet
    written, a service account not yet projected — blind the process for its whole
    lifetime. Raising is correct here: every accessor is called inside collect()'s
    try block, so an auth failure surfaces as unavailable with a real message rather
    than as an empty result that reads like a healthy cluster.
    """
    global _auth_loaded
    if _auth_loaded:
        return
    load_auth()
    _auth_loaded = True


def core_v1() -> client.CoreV1Api:
    "Pods, events, nodes, namespaces"
    _ensure_auth()
    return client.CoreV1Api()


def apps_v1() -> client.AppsV1Api:
    """ReplicaSets, Deployments, StatefulSets, DaemonSets."""
    _ensure_auth()
    return client.AppsV1Api()

def batch_v1() -> client.BatchV1Api:
    """Jobs — the other intermediate link in an ownerReferences chain."""
    _ensure_auth()
    return client.BatchV1Api()


# ---- scope -----------------------------------------------------------------
# Exclusions win over the allowlist. The agent's own namespace is always excluded:
# without it, the agent restarting emits a signal about itself, which triggers log
# collection about itself, which produces more signals.



def in_scope(namespace: str | None) -> bool:
    if namespace is None:
        return True
    if namespace in oncall_config.EXCLUDE_NAMESPACES:
        return False
    if oncall_config.NAMESPACES:
        return namespace in oncall_config.NAMESPACES
    return True

# ---- complete listing ------------------------------------------------------


def list_all(list_fn, **kwargs) -> list[Any]:

    items: list[Any] = []
    cont = None

    for _ in range(MAX_PAGES):
        page=list_fn(
            limit=PAGE_SIZE,
            _continue=cont,
            _request_timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        items.extend(page.items)

        cont=page.metadata._continue

        if not cont:
            return items

    raise RuntimeError(
        f"list did not terminate after {MAX_PAGES} pages; refusing to return a "
        f"partial result that would read as a complete one"
    )

