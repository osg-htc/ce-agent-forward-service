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
