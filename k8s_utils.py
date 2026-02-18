import re
from kubernetes import client, config, watch
import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Extract the "instance" from a pod name ("osg-hosted-ce-<instance-name>-<replicaset-id>-<pod-id>")
POD_NAME_CE_INSTANCE_RE = re.compile(r'osg-hosted-ce-(.*)-[a-z0-9]*-[a-z0-9]*')

def get_v1_client() -> client.CoreV1Api:
    """ Get an API client for the K8s API pointed at Tiger. """
    k8s_config = client.Configuration()
    config.load_kube_config(client_configuration=k8s_config)

    # TODO why doesn't load_kube_config honor the CA cert in the kubeconfig file? For now, just disable SSL verification.
    k8s_config.verify_ssl = False
    api_client = client.ApiClient(configuration=k8s_config)
    v1 = client.CoreV1Api(api_client=api_client)
    return v1

v1 = get_v1_client()

def get_ssh_host_key(pod_name: str, namespace: str) -> str:
    """ Get the SSH host key for the given pod by reading its associated configmap. """
    instance_name = POD_NAME_CE_INSTANCE_RE.match(pod_name).group(1)
    configmap_name = f"{instance_name}-ssh-host-pubkey"
    configmap = v1.read_namespaced_config_map(configmap_name, namespace)
    return configmap.data['ssh_host_rsa_key.pub']

