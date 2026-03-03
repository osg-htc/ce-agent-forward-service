#!/usr/bin/env python3
"""
Main entrypoint for CE SSH agent-forwarder.
Uses the kubernetes watch API to monitor for new CE pods, then spins off worker subprocesses
to ssh to each new pod when they become ready.
"""

from kubernetes import watch
from multiprocessing import Pool, Lock
from multiprocessing.pool import Pool as PoolType
from multiprocessing.synchronize import Lock as LockType
import re
import os
import sys
import logging
from k8s_utils import v1_client, pod_is_ready, get_key_name_for_pod
from ssh_worker import try_ssh_to_pod, init_port_lock
from config import NAMESPACE, LABEL_SELECTOR, MAX_PROCS

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Extract the socket name from the stdout of ssh-agent
SSH_AUTH_SOCK_RE = re.compile(r'SSH_AUTH_SOCK=([^;]*);')
# Extract the agent PID from the stdout of ssh-agent
SSH_AGENT_PID_RE = re.compile(r'SSH_AGENT_PID=([^;]*);')

class SessionDeduplicator:
    """ Class to track active SSH sessions to avoid opening multiple sessions to the same pod. """
    active_sessions: set[str]
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
                logger.info(f"Active session to {pod_name} already exists.")
                return False
            else:
                self.active_sessions.add(pod_name)
                self.pool.apply_async(
                    try_ssh_to_pod, 
                    args=(pod_name, namespace, ssh_keys), 
                    callback=lambda _: self.end_session(pod_name),
                    error_callback=lambda e: logger.error(f"Error in SSH session for pod {pod_name}: {e}"))
                logger.info(f"Starting SSH session for {pod_name}. {self._active_session_count} of {MAX_PROCS} sessions used.")
                return True

    def end_session(self, pod_name: str):
        """ End the session to the given pod. """
        logger.info(f"trying to end session to {pod_name}")
        with self.sessionLock:
            self.active_sessions.discard(pod_name)
            logger.info(f"Closed SSH session to {pod_name}. {self._active_session_count} of {MAX_PROCS} sessions used.")

    @property
    def _active_session_count(self) -> int:
        """ Get the count of currently active sessions. """
        return len(self.active_sessions)

def main():
    """ Main event loop to watch for new CE pods and SSH to them when they are ready. """

    logger.info("Starting CE SSH watcher...")
    w = watch.Watch()
    portLock = Lock()
    # Open a subprocess pool, sharing the port-allocation lock among all processes
    with Pool(processes=MAX_PROCS, initializer=init_port_lock, initargs=(portLock,)) as pool:
        # Synchronized struct to track which pods currently have active sessions
        deduplicator = SessionDeduplicator(pool)

        # Indefinitely poll the k8s API for new pod events.
        for event in w.stream(v1_client.list_namespaced_pod, namespace=NAMESPACE, label_selector=LABEL_SELECTOR):
            obj = event['object']
            logger.info(f"Event: {event['type']} {obj.kind} {obj.metadata.name}")

            # We get all events by default, filter for events where the all the pod's containers are running.
            if not pod_is_ready(obj):
                continue
            
            key_name = get_key_name_for_pod(obj.metadata.name, NAMESPACE)
            if not key_name:
                logger.warning(f"No key mapping found for pod {obj.metadata.name}. Skipping.")
                continue

            deduplicator.start_session(obj.metadata.name, obj.metadata.namespace, key_name)


if __name__ == "__main__":
    main()
