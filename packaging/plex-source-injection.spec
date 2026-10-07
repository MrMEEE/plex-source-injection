%global appdir %{_datadir}/plex-source-injection
%global libdir %{_libdir}/plex-source-injection/pythonlibs
%if 0%{?rhel} == 9
%global python_executable /usr/bin/python3.12
%global python_package python3.12
%global python_pip_package python3.12-pip
%else
%if 0%{?rhel} == 10
%global python_executable /usr/bin/python3
%global python_package python3
%global python_pip_package python3-pip
%else
%{error:This package supports only EL9 and EL10}
%endif
%endif
%global __python %{python_executable}
%global _build_id_links none
%global debug_package %{nil}

Name:           plex-source-injection
Version:        0.0.6
Release:        1%{?dist}
Summary:        On-demand music search and ingestion proxy for Plex
License:        GPL-3.0-only
URL:            https://github.com/MrMEEE/plex-source-injection
Source0:        %{name}-%{version}.tar.gz
Source1:        %{name}-wheelhouse-%{version}.tar.gz
BuildRequires:  %{python_package}
BuildRequires:  %{python_pip_package}
BuildRequires:  systemd-rpm-macros
Requires:       %{name}-pythonlibs%{?_isa} = %{version}-%{release}
Requires:       %{python_package}
Requires:       %{python_pip_package}
Requires:       python(abi) = 3.12
Requires:       systemd
Requires:       util-linux
Requires(pre):  shadow-utils
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

%description
Plex-compatible proxy with SQLite configuration, a separate administration
listener, plugin configuration, managed dependencies and activity logs.

%package pythonlibs
Summary:        Private Python dependencies for Plex Source Injection
# This private directory is not on the system Python path. Do not export its
# distributions as system python3dist providers.
AutoProv:       no
Requires:       %{python_package}
Requires:       python(abi) = 3.12

%description pythonlibs
Bundled Python dependencies for Plex Source Injection, isolated from system
Python packages. Includes upstream distribution metadata and licenses.

%prep
%setup -q
tar -xzf %{SOURCE1}

%build
# Python application; dependencies are prebuilt wheels for this EL/Python ABI.

%install
mkdir -p %{buildroot}%{appdir} %{buildroot}%{libdir}
cp -a *.py providers web requirements.txt %{buildroot}%{appdir}/
%{python_executable} -m pip install --no-index --no-compile \
    --find-links wheelhouse --target %{buildroot}%{libdir} -r requirements.txt
rm -rf %{buildroot}%{libdir}/bin
install -D -m 0755 packaging/plex-source-injection %{buildroot}%{_bindir}/plex-source-injection
install -D -m 0755 packaging/plex-inject-passwd %{buildroot}%{_bindir}/plex-inject-passwd
sed -i -e 's|@PYTHON@|%{python_executable}|g' -e 's|@LIBDIR@|%{libdir}|g' \
    %{buildroot}%{_bindir}/plex-source-injection
install -D -m 0644 packaging/plex-source-injection.service \
    %{buildroot}%{_unitdir}/plex-source-injection.service
install -d -m 0750 %{buildroot}%{_localstatedir}/lib/plex-source-injection
install -d -m 0750 %{buildroot}%{_localstatedir}/lib/plex-source-injection/bin
# Keep third-party license files and distribution metadata in the pythonlibs RPM.
find %{buildroot} -type d -name __pycache__ -prune -exec rm -rf {} +

%pre
getent group plex-source-injection >/dev/null || groupadd -r plex-source-injection
getent passwd plex-source-injection >/dev/null || \
    useradd -r -g plex-source-injection -d /var/lib/plex-source-injection \
    -s /sbin/nologin -c "Plex Source Injection" plex-source-injection

%post
%systemd_post plex-source-injection.service

%preun
%systemd_preun plex-source-injection.service

%postun
%systemd_postun_with_restart plex-source-injection.service

%files
%license LICENSE
%doc README.md .env.example
%{appdir}
%{_bindir}/plex-source-injection
%{_bindir}/plex-inject-passwd
%{_unitdir}/plex-source-injection.service
%attr(0750,plex-source-injection,plex-source-injection) %dir /var/lib/plex-source-injection
%attr(0750,plex-source-injection,plex-source-injection) %dir /var/lib/plex-source-injection/bin

%files pythonlibs
%{_libdir}/plex-source-injection

%changelog
* Thu Oct 08 2026 Martin Juhl <m@rtinjuhl.dk> - 0.0.6-1
- Release 0.0.6

* Thu Oct 08 2026 Martin Juhl <m@rtinjuhl.dk> - 0.0.5-1
- Release 0.0.5

* Thu Oct 08 2026 Martin Juhl <m@rtinjuhl.dk> - 0.0.4-1
- Release 0.0.4

* Thu Oct 08 2026 Martin Juhl <m@rtinjuhl.dk> - 0.0.3-1
- Release 0.0.3

* Thu Oct 08 2026 Martin Juhl <m@rtinjuhl.dk> - 0.0.2-1
- Release 0.0.2

* Thu Oct 08 2026 Martin Juhl <m@rtinjuhl.dk> - 0.0.1-1
- Release 0.0.1
