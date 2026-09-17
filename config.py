# Config for the script, handled via env.
import os
from pathlib import Path

# Namespace in which to whatch for new CE pods.
NAMESPACE = os.environ.get("CE_NAMESPACE", "osg")
# Label selector to filter for CE pods.
LABEL_SELECTOR = os.environ.get(
    "CE_LABEL_SELECTOR", "app.kubernetes.io/part-of=osg-hosted-ce"
)
# Maximum number of concurrent SSH sessions to allow. This should be O(CE count)
MAX_PROCS = int(os.environ.get("CE_MAX_PROCS", "5"))
# Which key from a pod's annotations to use. "primary" | "secondary"
KEY_TYPE = os.environ.get("CE_KEY_TYPE", "primary")
# Username to use when SSHing to the CE pods.
CE_USER = os.environ.get("CE_SSH_USER", "sshd-user")
# SSH key root path
SSH_KEY_ROOT = Path(
    os.environ.get("CE_SSH_KEY_ROOT", str(Path.home() / "scratch" / "yubikey"))
)
# Timeout (seconds) for establishing a TCP connection to the Kubernetes API server.
K8S_CONNECT_TIMEOUT = int(os.environ.get("CE_K8S_CONNECT_TIMEOUT", "10"))
# Timeout (seconds) to wait for a response to a single (non-streaming) Kubernetes API call.
K8S_READ_TIMEOUT = int(os.environ.get("CE_K8S_READ_TIMEOUT", "30"))
# Timeout (seconds) to wait for an event on the pod watch stream before reconnecting.
K8S_WATCH_TIMEOUT = int(os.environ.get("CE_K8S_WATCH_TIMEOUT", "300"))
