#!/usr/bin/env python3
"""
Main entrypoint for CE SSH agent-forwarder.
Uses the kubernetes watch API to monitor for new CE pods, then spins off worker subprocesses
to ssh to each new pod when they become ready.
"""

import logging
import os
import re
import signal
import socket
import sys
import tempfile
import time
from contextlib import contextmanager
from multiprocessing.synchronize import Lock as LockType
from subprocess import PIPE, Popen

from config import CE_USER, SSH_KEY_ROOT
from k8s_utils import get_ssh_host_key, pod_name_is_ready

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Extract the socket name from the stdout of ssh-agent
SSH_AUTH_SOCK_RE = re.compile(r"SSH_AUTH_SOCK=([^;]*);")
# Extract the agent PID from the stdout of ssh-agent
SSH_AGENT_PID_RE = re.compile(r"SSH_AGENT_PID=([^;]*);")

# Lock for allocating ports shared among processes, in accordance with the peculiarities of
# the Python multiprocessing library.
# see https://stackoverflow.com/a/25558333
portLock: LockType


def init_port_lock(lock: LockType):
    global portLock
    portLock = lock


def allocate_port() -> int:
    """Allocate and return a free local port. This should be called within a lock to avoid doubly-allocating the same port"""
    # Ask the OS to allocate a free port by binding to port 0, then close the socket and return the allocated port.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def port_forward_pod(pod_name: str, namespace: str, remote_port: int):
    """Port-forward the given pod's remote port to a local port within a context."""
    # TODO : Port-forwarding is non-trivial in the native Python API, for now shell out to kubectl.
    with portLock:
        local_port = allocate_port()
        cmd = [
            "kubectl",
            "-n",
            namespace,
            "port-forward",
            f"pod/{pod_name}",
            f"{local_port}:{remote_port}",
        ]
        proc = Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
        # Wait a moment for the port-forward to be established.
        # TODO read the proc's stdout to confirm this rather than just waiting.
        time.sleep(2)
    try:
        yield local_port
    finally:
        proc.terminate()


def stop_ssh_agent():
    """Stop the ssh-agent process if it's still running."""
    if "SSH_AGENT_PID" in os.environ:
        try:
            os.kill(int(os.environ["SSH_AGENT_PID"]), signal.SIGTERM)
        except ProcessLookupError:
            logger.info("SSH agent process already terminated.")

def handle_stop_ssh_agent(signal, frame):
    stop_ssh_agent()

@contextmanager
def ssh_agent_session(ssh_key_paths: str):
    """Run the context within an eval $(ssh-agent), closing the agent at the end"""
    ssh_agent_out, ssh_agent_err = Popen("ssh-agent", stdout=PIPE).communicate()
    socket_line, pid_line, *_ = [l.decode() for l in ssh_agent_out.splitlines()]

    socket_match = SSH_AUTH_SOCK_RE.search(socket_line)
    pid_match = SSH_AGENT_PID_RE.search(pid_line)
    if not socket_match or not pid_match:
        raise RuntimeError(f"Unexpected ssh-agent output: {ssh_agent_out.decode()}")

    os.environ["SSH_AUTH_SOCK"] = socket_match[1]
    os.environ["SSH_AGENT_PID"] = pid_match[1]

    try:
        for ssh_key_path in ssh_key_paths.split(","):
            Popen(["ssh-add", SSH_KEY_ROOT / ssh_key_path]).wait()
        yield
    finally:
        stop_ssh_agent()


@contextmanager
def temporary_known_hosts(host_key: str):
    """Create a temporary known_hosts file containing the given host key, and set the environment variable to point to it."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"localhost " + host_key.encode())
        f.close()
        yield f.name


def ssh_to_pod(pod_name: str, namespace: str, ssh_keys: str, remote_port=22):
    """
    SSH to the given pod using the provided SSH keys. Leave the connection open indefinitely.
    Each call to this method should be run in a separate subprocess.
    """
    # Port-forward the pod's SSH port to a local port.
    host_key = get_ssh_host_key(pod_name, namespace)

    # Create contexts for ssh'ing:
    # - A port-forward to the pod's SSH port
    # - An ssh-agent session with the provided keys loaded
    # - A temporary known_hosts file containing the pod's SSH host key
    logger.info(f"Starting SSH session for pod {pod_name}...")
    with (
        port_forward_pod(pod_name, namespace, remote_port=remote_port) as local_port,
        ssh_agent_session(ssh_keys),
        temporary_known_hosts(host_key) as known_hosts_path,
    ):
        logger.info("Port forwarding established, SSHing to pod...")
        # TODO encode as many of these flags as possible into an ssh config file?
        cmd = [
            "ssh",
            "-o",
            f"UserKnownHostsFile={known_hosts_path}",
            "-o",
            "ForwardAgent=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"IdentityFile={SSH_KEY_ROOT}/{ssh_keys.split(',')[0]}",
            "-p",
            f"{local_port}",
            f"{CE_USER}@localhost",
            "echo 'Logging from inside ssh connection to pod' $(hostname)'. Connection established.'; sleep infinity",
        ]
        Popen(cmd).wait()

    logger.info(f"SSH session to pod {pod_name} complete.")


def try_ssh_to_pod(pod_name: str, namespace: str, ssh_keys: str, remote_port: int = 22):
    """Wrapper around ssh_to_pod that catches and logs exceptions. Entrypoint for subprocesses."""
    logger.info(f"Try ssh to pod {pod_name} in {namespace} with keys {ssh_keys} to port {remote_port}")
    signal.signal(signal.SIGINT, handle_stop_ssh_agent)
    # Keep trying to ssh to the pod until it no longer exists, in the case of unexpected disconnects
    while pod_name_is_ready(pod_name, namespace):
        try:
            ssh_to_pod(pod_name, namespace, ssh_keys, remote_port)
        except Exception as e:
            logger.error(f"Error SSHing to pod {pod_name}: {e}")
        logger.info(
            f"SSH session to pod {pod_name} ended, checking if pod is still ready..."
        )
        time.sleep(5)
