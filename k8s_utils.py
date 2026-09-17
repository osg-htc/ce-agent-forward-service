import logging
import re
import sys

import urllib3
import urllib3.util.connection as urllib3_connection
import yaml
from kubernetes import client, config
from typing import Any

from config import K8S_CONNECT_TIMEOUT, K8S_READ_TIMEOUT, KEY_TYPE

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Some hosts have a route to the Kubernetes API server over IPv4 but not IPv6, despite
# the API server's hostname resolving to both. Unlike kubectl/curl, urllib3's
# create_connection() tries getaddrinfo() results serially with a blocking connect()
# and no happy-eyeballs fallback, so a black-holed IPv6 route hangs forever instead of
# falling back to IPv4. Force IPv4-only resolution to avoid that.
urllib3_connection.HAS_IPV6 = False

# Extract the "instance" from a pod name ("osg-hosted-ce-<instance-name>-<replicaset-id>-<pod-id>")
POD_NAME_CE_INSTANCE_RE = re.compile(r"osg-hosted-ce-(.*)-[a-z0-9]*-[a-z0-9]*")


def get_v1_client() -> client.CoreV1Api:
    """Get an API client for the K8s API pointed at Tiger."""
    k8s_config = client.Configuration()
    config.load_kube_config(client_configuration=k8s_config)

    # TODO why doesn't load_kube_config honor the CA cert in the kubeconfig file? For now, just disable SSL verification.
    k8s_config.verify_ssl = False
    api_client = client.ApiClient(configuration=k8s_config)
    v1 = client.CoreV1Api(api_client=api_client)
    return v1


v1_client = get_v1_client()


def get_ssh_host_key(pod_name: str, namespace: str) -> str:
    """Get the SSH host key for the given pod by reading its associated configmap."""
    instance_name = POD_NAME_CE_INSTANCE_RE.match(pod_name).group(1)
    configmap_name = f"{instance_name}-ssh-host-pubkey"
    configmap = v1_client.read_namespaced_config_map(
        configmap_name, namespace,
        _request_timeout=(K8S_CONNECT_TIMEOUT, K8S_READ_TIMEOUT),
    )
    return configmap.data["ssh_host_rsa_key.pub"]


def get_key_name_for_pod(pod: Any) -> str | None:
    """Get the SSH key that should be forwarded to the pod with the given name.
    Key mapping is expected to be stored in the pod's labels.
    """
    return pod.metadata.labels.get(f"osg-htc.org/{KEY_TYPE}-key", None)


def pod_is_ready(pod) -> bool:
    """Return whether all containers in a pod are 'running', and the pod is not marked for deletion."""
    # We get all events by default, filter for events where the all the pod's containers are running.
    if pod and pod.status and pod.status.container_statuses:
        deleted = pod.metadata.deletion_timestamp is not None
        running_states = [k.state.running for k in pod.status.container_statuses]
        return all(running_states) and not deleted
    return False


def pod_name_is_ready(name, namespace) -> bool:
    """Return whether all containers in a pod with the given name are 'running', and the pod is not marked for deletion."""
    try:
        pod = v1_client.read_namespaced_pod(
            name, namespace,
            _request_timeout=(K8S_CONNECT_TIMEOUT, K8S_READ_TIMEOUT),
        )
        return pod_is_ready(pod)
    except (client.exceptions.ApiException, urllib3.exceptions.HTTPError) as e:
        logger.error(f"Error checking if pod {name} in {namespace} is ready: {e}")
        return False
