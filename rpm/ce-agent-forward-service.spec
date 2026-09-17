%global service_user  ce-agent-forward
%global service_group ce-agent-forward
%global appdir        %{_libexecdir}/%{name}

Name:           ce-agent-forward-service
Version:        0.1.0
Release:        3%{?dist}
Summary:        Maintains SSH agent-forwarding sessions to OSG Compute Entrypoint pods

License:        TODO
URL:            https://github.com/osg-htc/ce-agent-forward-service
# Build with: git archive --prefix=%%{name}-%%{version}/ -o %%{name}-%%{version}.tar.gz HEAD
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  systemd-rpm-macros

Requires:       python3
Requires:       python3-kubernetes
Requires:       python3-pyyaml
Requires:       openssh-clients
Requires:       kubectl
Requires(pre):  shadow-utils
Requires(post): systemd-udev
%{?systemd_requires}

%description
ce-agent-forward-service watches the Kubernetes API for OSG Compute Entrypoint
(CE) pods and, once a pod is ready, forks a worker that port-forwards to the
pod's SSH port and opens a persistent SSH connection with agent forwarding
enabled, using the SSH key(s) named in the pod's annotations. It is intended
to run as a long-lived systemd service.

%prep
%setup -q

%build
# Nothing to build; this is a pure Python application.

%install
rm -rf %{buildroot}

install -d -m 0755 %{buildroot}%{appdir}
install -p -m 0644 main.py       %{buildroot}%{appdir}/main.py
install -p -m 0644 config.py     %{buildroot}%{appdir}/config.py
install -p -m 0644 k8s_utils.py  %{buildroot}%{appdir}/k8s_utils.py
install -p -m 0644 ssh_worker.py %{buildroot}%{appdir}/ssh_worker.py

install -d -m 0755 %{buildroot}%{_unitdir}
install -p -m 0644 systemd/%{name}.service %{buildroot}%{_unitdir}/%{name}.service

install -d -m 0755 %{buildroot}%{_sysconfdir}/sysconfig
install -p -m 0644 systemd/%{name}.sysconfig %{buildroot}%{_sysconfdir}/sysconfig/%{name}

install -d -m 0700 %{buildroot}%{_sharedstatedir}/%{name}
install -d -m 0700 %{buildroot}%{_sharedstatedir}/%{name}/.kube
install -d -m 0700 %{buildroot}%{_sharedstatedir}/%{name}/keys

install -d -m 0755 %{buildroot}%{_udevrulesdir}
install -p -m 0644 udev/71-%{service_group}-hidraw.rules %{buildroot}%{_udevrulesdir}/71-%{service_group}-hidraw.rules

%pre
getent group %{service_group} >/dev/null || groupadd -r %{service_group}
getent passwd %{service_user} >/dev/null || \
    useradd -r -g %{service_group} -d %{_sharedstatedir}/%{name} \
            -s /sbin/nologin -c "ce-agent-forward-service" %{service_user}
exit 0

%post
%systemd_post %{name}.service
udevadm control --reload-rules >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=hidraw >/dev/null 2>&1 || :

%preun
%systemd_preun %{name}.service

%postun
%systemd_postun_with_restart %{name}.service

%files
%dir %{appdir}
%{appdir}/main.py
%{appdir}/config.py
%{appdir}/k8s_utils.py
%{appdir}/ssh_worker.py
%{_unitdir}/%{name}.service
%{_udevrulesdir}/71-%{service_group}-hidraw.rules
%config(noreplace) %{_sysconfdir}/sysconfig/%{name}
%attr(0700,%{service_user},%{service_group}) %dir %{_sharedstatedir}/%{name}
%attr(0700,%{service_user},%{service_group}) %dir %{_sharedstatedir}/%{name}/.kube
%attr(0700,%{service_user},%{service_group}) %dir %{_sharedstatedir}/%{name}/keys

%changelog
* Thu Sep 17 2026 Matthew Westphall <westphall@wisc.edu> - 0.1.0-3
- Document HTTP(S)_PROXY/NO_PROXY overrides (commented, no site-specific
  values) in the sysconfig template, since systemd services don't inherit
  the login shell's proxy environment.

* Thu Sep 17 2026 Matthew Westphall <westphall@wisc.edu> - 0.1.0-2
- Ship a udev rule granting the service group access to FIDO/U2F hidraw
  devices, since EL's stock udev rules tag security keys but grant no
  access to them (unlike desktop distros' uaccess-based rules).

* Wed Sep 16 2026 Matthew Westphall <westphall@wisc.edu> - 0.1.0-1
- Initial packaging for EL10.
