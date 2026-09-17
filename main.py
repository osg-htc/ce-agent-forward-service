#!/usr/bin/env python3
"""
Main entrypoint for CE SSH agent-forwarder.
Uses the kubernetes watch API to monitor for new CE pods, then spins off worker subprocesses
to ssh to each new pod when they become ready.
"""

import logging
import re
import sys
from multiprocessing import Lock, Pool
from multiprocessing.pool import Pool as PoolType
from multiprocessing.synchronize import Lock as LockType
from typing import Any

from kubernetes import watch

from config import K8S_CONNECT_TIMEOUT, K8S_WATCH_TIMEOUT, LABEL_SELECTOR, MAX_PROCS, NAMESPACE, KEY_TYPE
from k8s_utils import get_key_name_for_pod, pod_is_ready, v1_client
from ssh_worker import init_port_lock, try_ssh_to_pod

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Extract the socket name from the stdout of ssh-agent
SSH_AUTH_SOCK_RE = re.compile(r"SSH_AUTH_SOCK=([^;]*);")
# Extract the agent PID from the stdout of ssh-agent
SSH_AGENT_PID_RE = re.compile(r"SSH_AGENT_PID=([^;]*);")


class SessionDeduplicator:
    """Class to track active SSH sessions to avoid opening multiple sessions to the same pod."""

    active_sessions: set[str]
    sessionLock: LockType
    pool: PoolType

    def __init__(self, pool: PoolType):
        self.active_sessions = set()
        self.sessionLock = Lock()
        self.pool = pool

    def start_session(self, pod_name: str, namespace: str, ssh_keys: str, remote_port: int) -> bool:
        """Attempt to start a session to the given pod. Returns True if a session was started, False if a session is already active."""
        with self.sessionLock:
            if pod_name in self.active_sessions:
                logger.info(f"Active session to {pod_name} already exists.")
                return False
            else:
                self.active_sessions.add(pod_name)
                self.pool.apply_async(
                    try_ssh_to_pod,
                    args=(pod_name, namespace, ssh_keys, remote_port),
                    callback=lambda _: self.end_session(pod_name),
                    error_callback=lambda e: logger.error(
                        f"Error in SSH session for pod {pod_name}: {e}"
                    ),
                )
                logger.info(
                    f"Starting SSH session for {pod_name}. {self._active_session_count} of {MAX_PROCS} sessions used."
                )
                return True

    def end_session(self, pod_name: str):
        """End the session to the given pod."""
        logger.info(f"trying to end session to {pod_name}")
        with self.sessionLock:
            self.active_sessions.discard(pod_name)
            logger.info(
                f"Closed SSH session to {pod_name}. {self._active_session_count} of {MAX_PROCS} sessions used."
            )

    @property
    def _active_session_count(self) -> int:
        """Get the count of currently active sessions."""
        return len(self.active_sessions)


def main():
    """Main event loop to watch for new CE pods and SSH to them when they are ready."""

    logger.info("Starting CE SSH watcher...")
    w = watch.Watch()
    portLock = Lock()
    # Open a subprocess pool, sharing the port-allocation lock among all processes
    with Pool(
        processes=MAX_PROCS, initializer=init_port_lock, initargs=(portLock,)
    ) as pool:
        # Synchronized struct to track which pods currently have active sessions
        deduplicator = SessionDeduplicator(pool)

        # Indefinitely poll the k8s API for new pod events. If the connection hangs or
        # goes quiet for longer than the watch timeout, this raises and the process exits;
        # systemd (Restart=on-failure) is responsible for restarting it, not this loop.
        for event in w.stream(
            v1_client.list_namespaced_pod,
            namespace=NAMESPACE,
            label_selector=LABEL_SELECTOR,
            _request_timeout=(K8S_CONNECT_TIMEOUT, K8S_WATCH_TIMEOUT),
        ):
            obj : Any = event["object"]
            logger.info(f"Event: {event['type']} {obj.kind} {obj.metadata.name}")

            # We get all events by default, filter for events where the all the pod's containers are running.
            if not pod_is_ready(obj):
                continue

            key_name = get_key_name_for_pod(obj)
            if not key_name:
                logger.warning(
                    f"No key mapping found for pod {obj.metadata.name}. Skipping."
                )
                continue

            
            remote_port = 22 if KEY_TYPE == "primary" else 23
            deduplicator.start_session(obj.metadata.name, obj.metadata.namespace, key_name, remote_port)


if __name__ == "__main__":
    main()
