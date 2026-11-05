#!/bin/sh
# Runs only inside the builder's new user/mount/network namespace and ARM64 fakeroot.
set -eu
umask 022
ss07_kernel_release=$1
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C.UTF-8

apt-get -o Acquire::Languages=none update
set --
while IFS= read -r ss07_package; do
    set -- "$@" "$ss07_package"
done < /build-inputs/install-args.txt
apt-get -y --no-download --no-install-recommends --no-remove \
    -o Dpkg::Options::=--force-confnew install "$@"
test -z "$(dpkg --audit)"

cp -a /build-app/opt /build-app/etc /build-app/usr /
python3 /build-scripts/register-release.py
cp -a /build-modules/lib/modules /lib/
depmod -a "$ss07_kernel_release"
install -m 644 /build-inputs/kernel.config "/boot/config-$ss07_kernel_release"
mkdir -p /etc/initramfs-tools/conf.d
printf 'MODULES=most\nCOMPRESS=gzip\n' > /etc/initramfs-tools/conf.d/ss07
printf 'RESUME=none\n' > /etc/initramfs-tools/conf.d/resume
mkinitramfs -o "/build-state/initrd-$ss07_kernel_release" "$ss07_kernel_release"

printf 'ss07-rk3588\n' > /etc/hostname
printf '127.0.0.1 localhost\n127.0.1.1 ss07-rk3588\n::1 localhost ip6-localhost ip6-loopback\n' > /etc/hosts
printf 'LANG=C.UTF-8\n' > /etc/default/locale
printf 'UTC\n' > /etc/timezone
ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime
install -m 644 /build-inputs/fstab /etc/fstab
mkdir -p /etc/systemd/network /etc/systemd/journald.conf.d
install -m 644 /build-scripts/20-ss07-wired.network /etc/systemd/network/
install -m 644 /build-scripts/journald.conf /etc/systemd/journald.conf.d/ss07.conf
ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
systemctl enable systemd-networkd.service systemd-resolved.service systemd-timesyncd.service
systemctl enable serial-getty@ttyFIQ0.service

# An SSH public key is a separate explicit provisioning input. There is no default password/key.
useradd -m -U -s /bin/bash -G sudo,video,dialout ss07-admin
passwd -l root
passwd -l ss07-admin
mkdir -p /etc/ssh/sshd_config.d /etc/systemd/system/ssh.service.d
install -m 644 /build-scripts/ssh.conf /etc/ssh/sshd_config.d/ss07.conf
install -m 644 /build-scripts/ssh-service.conf /etc/systemd/system/ssh.service.d/ss07.conf
printf 'ss07-admin ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/ss07-admin
chmod 440 /etc/sudoers.d/ss07-admin
visudo -c
install -d -m 755 -o root -g root /run/sshd
sshd -t
rm -f /etc/ssh/ssh_host_*
if test -f /build-inputs/admin-authorized-key; then
    install -d -m 700 -o ss07-admin -g ss07-admin /home/ss07-admin/.ssh
    install -m 600 -o ss07-admin -g ss07-admin /build-inputs/admin-authorized-key /home/ss07-admin/.ssh/authorized_keys
fi
systemctl enable ssh.service

systemd-sysusers /usr/lib/sysusers.d/ss07.conf
# Focal fakeroot 1.24 cannot emulate tmpfiles' fchownat(AT_EMPTY_PATH).
# The builder applies this same file with host tmpfiles and the saved ownership state.
install -m 644 /build-scripts/ss07.target /usr/lib/systemd/system/ss07.target
for ss07_service in ss07-api ss07-camera@ ss07-gateway ss07-maintenance ss07-media ss07-storage ss07-timecode ss07-sonar ss07-application ss07-config-recover; do
    mkdir -p "/etc/systemd/system/$ss07_service.service.d"
    install -m 644 /build-scripts/provisioning.conf "/etc/systemd/system/$ss07_service.service.d/10-provisioning.conf"
done
systemctl enable ss07.target
systemctl set-default multi-user.target

mkdir -p /usr/share/ss07-image /etc/apt/preferences.d
install -m 644 /build-inputs/image.lock.json /usr/share/ss07-image/inputs.lock.json
install -m 644 /build-scripts/gstreamer.pref /etc/apt/preferences.d/ss07-gstreamer
dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n' > /usr/share/ss07-image/packages.tsv
cp /etc/passwd /etc/group /build-state/
cp /usr/share/ss07-image/packages.tsv /build-state/
cat /etc/os-release > /build-state/os-release

/opt/ss07/current/tools/ss07 validate --config /etc/ss07/system.json
/opt/ss07/current/bin/ss07-camera --version
for ss07_feature in v4l2src h264parse queue capsfilter rtph264pay udpsink; do
    gst-inspect-1.0 "$ss07_feature" > "/build-state/plugin-$ss07_feature.txt"
done
systemd-analyze verify /usr/lib/systemd/system/ss07*.service /usr/lib/systemd/system/ss07.target

# Remove build-only inputs and machine-specific identifiers. Do not create a recording directory.
apt-get clean
rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb
rm -f /usr/sbin/policy-rc.d /etc/apt/apt.conf.d/99-ss07-build
install -m 644 /build-scripts/sources.list /etc/apt/sources.list
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id /var/lib/systemd/random-seed
mkdir -p /var/lib/dbus
ln -sf /etc/machine-id /var/lib/dbus/machine-id
find /var/log -type f -exec truncate -s 0 {} +
rm -rf /root/.cache
rm -f "/boot/config-$ss07_kernel_release"
printf 'unprovisioned\n' > /etc/ss07-image-state
