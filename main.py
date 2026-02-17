#!/usr/bin/env python3

from kubernetes import client, config, watch
from contextlib import contextmanager
from subprocess import Popen, PIPE
from multiprocessing import Pool, Lock
from multiprocessing.pool import Pool as PoolType
from multiprocessing.synchronize import Lock as LockType
import signal 
import re
import os
from pathlib import Path
import tempfile 
import sys
import time
import socket

# Constants for the script, configured via env

# Namespace in which to whatch for new CE pods.
NAMESPACE = os.environ.get("CE_NAMESPACE", "osg")
# Label selector to filter for CE pods.
LABEL_SELECTOR = os.environ.get("CE_LABEL_SELECTOR", "app.kubernetes.io/part-of=osg-hosted-ce")
# Username to use when SSHing to the CE pods.
CE_USER = os.environ.get("CE_SSH_USER", "sshd-user")
# SSH key root path
SSH_KEY_ROOT = os.environ.get("CE_SSH_KEY_ROOT", str(Path.home() / 'scratch' / 'yubikey'))
# Maximum number of concurrent SSH sessions to allow. This should be O(CE count)
MAX_PROCS = int(os.environ.get("CE_MAX_PROCS", "5"))

# Extract the socket name from the stdout of ssh-agent
SSH_AUTH_SOCK_RE = re.compile(r'SSH_AUTH_SOCK=([^;]*);')
# Extract the agent PID from the stdout of ssh-agent
SSH_AGENT_PID_RE = re.compile(r'SSH_AGENT_PID=([^;]*);')
# Extract the "instance" from a pod name ("osg-hosted-ce-<instance-name>-<replicaset-id>-<pod-id>")
POD_NAME_CE_INSTANCE_RE = re.compile(r'osg-hosted-ce-(.*)-[a-z0-9]*-[a-z0-9]*')

# Lock for allocating ports shared among processes, in accordance with the peculiarities of
# the Python multiprocessing library.
# see https://stackoverflow.com/a/25558333
portLock: LockType
def init_port_lock(lock: LockType):
    global portLock
    portLock = lock

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

def allocate_port() -> int:
    """ Allocate and return a free local port. """
    # Ask the OS to allocate a free port by binding to port 0, then close the socket and return the allocated port.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

@contextmanager
def port_forward_pod(pod_name: str, namespace: str, remote_port: int):
    """ Port-forward the given pod's remote port to a local port within a context. """
    # TODO : Port-forwarding is non-trivial in the native Python API, for now shell out to kubectl.
    with portLock:
        local_port = allocate_port()
        cmd = ["kubectl", "-n", namespace, "port-forward", f"pod/{pod_name}", f"{local_port}:{remote_port}"]
        proc = Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
        # Wait a moment for the port-forward to be established. 
        # TODO read the proc's stdout to confirm this rather than just waiting.
        time.sleep(2)
    try:
        yield local_port
    finally:        
        proc.terminate()

@contextmanager
def ssh_agent_session(ssh_key_paths: str):
    """ Run the context within an eval $(ssh-agent), closing the agent at the end """
    ssh_agent_out, ssh_agent_err = Popen('ssh-agent', stdout=PIPE).communicate()
    socket_line, pid_line, *_ = [l.decode() for l in ssh_agent_out.splitlines()]

    socket_match = SSH_AUTH_SOCK_RE.search(socket_line)
    pid_match = SSH_AGENT_PID_RE.search(pid_line)
    if not socket_match or not pid_match:
        raise RuntimeError(f"Unexpected ssh-agent output: {ssh_agent_out.decode()}")

    os.environ['SSH_AUTH_SOCK'] = socket_match[1]
    os.environ['SSH_AGENT_PID'] = pid_match[1]

    try:
        for ssh_key_path in ssh_key_paths.split(','):
            Popen(['ssh-add', SSH_KEY_ROOT / ssh_key_path]).wait()
        yield
    finally:
        os.kill(int(os.environ['SSH_AGENT_PID']), signal.SIGTERM)

@contextmanager
def temporary_known_hosts(host_key: str):
    """ Create a temporary known_hosts file containing the given host key, and set the environment variable to point to it. """
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b'localhost ' + host_key.encode())
        f.close()
        yield f.name


def get_ssh_host_key(pod_name: str, namespace: str) -> str:
    """ Get the SSH host key for the given pod. """
    instance_name = POD_NAME_CE_INSTANCE_RE.match(pod_name).group(1)
    configmap_name = f"{instance_name}-ssh-host-pubkey"
    configmap = v1.read_namespaced_config_map(configmap_name, namespace)
    return configmap.data['ssh_host_rsa_key.pub']

def ssh_to_pod(pod_name: str, namespace: str, ssh_keys: str):
    """ SSH to the given pod using the provided SSH keys. """
    # Port-forward the pod's SSH port to a local port.
    host_key = get_ssh_host_key(pod_name, namespace)

    # Create contexts for ssh'ing:
    # - A port-forward to the pod's SSH port
    # - An ssh-agent session with the provided keys loaded
    # - A temporary known_hosts file containing the pod's SSH host key
    print(f"Starting SSH session for pod {pod_name}...")
    with port_forward_pod(pod_name, namespace, remote_port=22) as local_port, \
         ssh_agent_session(ssh_keys), \
         temporary_known_hosts(host_key) as known_hosts_path:
        print("Port forwarding established, SSHing to pod...")
        # TODO encode as many of these flags as possible into an ssh config file?
        cmd = ['ssh', 
               '-o', f'UserKnownHostsFile={known_hosts_path}', 
               '-o', 'ForwardAgent=yes',
               '-o', 'IdentitiesOnly=yes',
               '-o', f'IdentityFile={SSH_KEY_ROOT}/{ssh_keys.split(",")[0]}',
               '-p', f'{local_port}', f'{CE_USER}@localhost',
               "echo 'Logging from inside ssh connection to pod' $(hostname)'. Connection established.'; sleep infinity"]
        Popen(cmd).wait()
    
    print(f"SSH session to pod {pod_name} complete.")


def try_ssh_to_pod(pod_name: str, namespace: str, ssh_keys: str):
    """ Wrapper around ssh_to_pod that catches and logs exceptions. """
    print(f"Try ssh to pod {pod_name} in {namespace} with keys {ssh_keys}")
    try:
        ssh_to_pod(pod_name, namespace, ssh_keys)
    except Exception as e:
        print(f"Error SSHing to pod {pod_name}: {e}")

class SessionDeduplicator:
    """ Class to track active SSH sessions to avoid opening multiple sessions to the same pod. """
    active_sessions: set[str]
    # Lock for access to active_sessions
    sessionLock: LockType
    pool: PoolType

    def __init__(self, pool: PoolType):
        self.active_sessions = set()
        self.sessionLock = Lock()
        self.pool = pool

    def start_session(self, pod_name: str, namespace: str, ssh_keys: str) -> bool:
        """ Attempt to start a session to the given pod. Returns True if a session was started, False if a session is already active. """
        with self.sessionLock:
            if pod_name in self.active_sessions:
                print(f"Active session to {pod_name} already exists.")
                return False
            else:
                self.active_sessions.add(pod_name)
                self.pool.apply_async(
                    try_ssh_to_pod, 
                    args=(pod_name, namespace, ssh_keys), 
                    callback=lambda _: self.end_session(pod_name),
                    error_callback=lambda e: print(f"Error in SSH session for pod {pod_name}: {e}"))
                print(f"Starting SSH session for {pod_name}. {self._active_session_count} of {MAX_PROCS} sessions used.")
                return True

    def end_session(self, pod_name: str):
        """ End the session to the given pod. """
        print(f"trying to end session to {pod_name}")
        with self.sessionLock:
            self.active_sessions.discard(pod_name)
            print(f"Closed SSH session to {pod_name}. {self._active_session_count} of {MAX_PROCS} sessions used.")

    @property
    def _active_session_count(self) -> int:
        """ Get the count of currently active sessions. """
        return len(self.active_sessions)

def main():
    """ Main event loop to watch for new CE pods and SSH to them when they are ready. """

    print("Listing pods in the default namespace:")
    w = watch.Watch()
    portLock = Lock()
    with Pool(processes=MAX_PROCS, initializer=init_port_lock, initargs=(portLock,)) as pool:
        deduplicator = SessionDeduplicator(pool)
        for event in w.stream(v1.list_namespaced_pod, namespace="osg", label_selector="app.kubernetes.io/part-of=osg-hosted-ce"):
            obj = event['object']
            print("Event: %s %s %s" % (
                event['type'],
                obj.kind,
                obj.metadata.name)
            )
            if obj.status and obj.status.container_statuses:
                deleted = obj.metadata.deletion_timestamp is not None
                running_states = [k.state.running for k in obj.status.container_statuses]
                if all(running_states) and not deleted:
                    deduplicator.start_session(obj.metadata.name, obj.metadata.namespace, obj.metadata.annotations.get('osg-htc.org/ssh-keys'))
                elif deleted:
                    print(f"Pod {obj.metadata.name} is marked for deletion. Not SSHing.")


if __name__ == "__main__":
    main()
