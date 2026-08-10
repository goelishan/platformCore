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


def core_v1() -> client.CoreV1Api:
    "Pods, events, nodes, namespaces"
    return client.CoreV1Api()


def apps_v1() -> client.AppsV1Api:
    """ReplicaSets, Deployments, StatefulSets, DaemonSets."""
    return client.AppsV1Api()

def batch_v1() -> client.BatchV1Api:
    """Jobs — the other intermediate link in an ownerReferences chain."""
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

