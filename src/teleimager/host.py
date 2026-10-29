# Copyright 2025-2026 YuShu TECHNOLOGY CO.,LTD ("Unitree Robotics")
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Configure Linux camera permissions, UVC driver reloads, and CPU affinity."""

import grp
import os
import pwd
import shutil
import subprocess
import tempfile

import logging_mp

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


_UDEV_RULE = ('SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", '
              'ENV{ID_USB_INTERFACES}=="*:0e01??:*", '
              'GROUP="video", MODE="0660", TAG+="uaccess"\n')


_UDEV_RULE_PATH = "/etc/udev/rules.d/70-teleimager.rules"


_SUDOERS_PATH = "/etc/sudoers.d/uvc_modprobe"


def reload_uvcvideo_module():
    """Reload the Linux UVC driver and wait for pending udev events.

    A nonzero unload exit status is tolerated; loading requires success.
    Non-root calls use non-interactive sudo. Command errors and timeouts abort
    the remaining steps and are logged without propagating to the caller.
    """
    # Root needs no escalation and may not have a matching sudoers entry.
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    try:
        # Non-interactive sudo fails immediately if permissions are missing.
        subprocess.run([*sudo, "modprobe", "-r", "uvcvideo"], timeout=30)
        subprocess.run([*sudo, "modprobe", "uvcvideo"], check=True, timeout=30)
        # Waiting for udev does not need the modprobe privilege grant.
        subprocess.run(["udevadm", "settle"], timeout=30)
        logger_mp.info("[Teleimager] UVC driver reloaded.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        logger_mp.error(f"[Teleimager] Failed to reload driver: {e}")
        logger_mp.error('[Teleimager] Grant it once with:\n'
                        '    sudo "$(which teleimager-server)" --setup-permissions')


def _run(argv):
    """Run a host command with a 30-second timeout and return whether it succeeded."""
    try:
        subprocess.run(argv, check=True, timeout=30)
        return True
    except FileNotFoundError:
        logger_mp.error(f"[Teleimager] {argv[0]} not found; skipping: {' '.join(argv)}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger_mp.error(f"[Teleimager] {' '.join(argv)} failed: {e}")
    return False


def setup_permissions(requested_user=None):
    """Install camera access rules for a normal user; requires root.

    Installs the udev rule, adds the user to the video group, and grants the
    passwordless modprobe command used by reload_uvcvideo_module().

    Args:
        requested_user: Account to configure; defaults to SUDO_USER.

    Returns:
        Zero on completion, or one when a reported error aborts setup.
    """
    if os.geteuid() != 0:
        logger_mp.error("[Teleimager] This writes to /etc/udev and /etc/sudoers.d, "
                        "so it needs root:\n"
                        '    sudo "$(which teleimager-server)" --setup-permissions')
        return 1

    # Limit the passwordless modprobe grant to the selected account.
    user = requested_user or os.environ.get("SUDO_USER") or None
    if user == "root":
        user = None
    if requested_user:
        try:
            pwd.getpwnam(requested_user)
        except KeyError:
            logger_mp.error(f"[Teleimager] No such user: {requested_user}")
            return 1

    try:
        with open(_UDEV_RULE_PATH, "w") as f:
            f.write(_UDEV_RULE)
        os.chmod(_UDEV_RULE_PATH, 0o644)
        logger_mp.info(f"[Teleimager] Installed {_UDEV_RULE_PATH}")
    except OSError as e:
        logger_mp.error(f"[Teleimager] Cannot write {_UDEV_RULE_PATH}: {e}")
        return 1

    # Rules only apply to new events, so already-plugged cameras need a
    # synthetic 'change' to pick up the ACL without a replug.
    _run(["udevadm", "control", "--reload-rules"])
    _run(["udevadm", "trigger", "--subsystem-match=usb", "--action=change"])

    needs_relogin = False
    if user:
        try:
            grp.getgrnam("video")
        except KeyError:
            # uaccess still covers a desktop session; only the SSH/headless
            # fallback is lost, so this is a warning rather than a failure.
            logger_mp.warning("[Teleimager] No 'video' group on this system; "
                              "desktop sessions still work via uaccess.")
        else:
            if user in grp.getgrnam("video").gr_mem:
                logger_mp.info(f"[Teleimager] {user} is already in the 'video' group.")
            elif _run(["usermod", "-aG", "video", user]):
                needs_relogin = True

    # Root needs no grant of its own; the steps left are all per-account.
    if not user:
        logger_mp.info("[Teleimager] Running as root; the udev rule is in place. "
                       "Pass --user <name> to grant a normal account.")
        return 0

    # Match the reload command exactly and validate the rule before installing.
    modprobe = shutil.which("modprobe") or "/usr/sbin/modprobe"
    rule = ("# Teleimager: reload the UVC driver without a password.\n"
            "%s ALL=(ALL) NOPASSWD: %s -r uvcvideo, %s uvcvideo\n"
            % (user, modprobe, modprobe))
    # Stage the sudoers rule outside configuration directories until validated.
    try:
        fd, tmp = tempfile.mkstemp(prefix="teleimager-sudoers-")
    except OSError as e:
        logger_mp.error(f"[Teleimager] Cannot create a temp file: {e}")
        return 1
    try:
        with os.fdopen(fd, "w") as f:
            f.write(rule)
        check = subprocess.run(["visudo", "-c", "-f", tmp],
                               capture_output=True, text=True)
        if check.returncode != 0:
            out = (check.stdout or check.stderr or "").strip()[:500]
            logger_mp.error("[Teleimager] Generated sudoers entry is invalid, "
                            "not installing it:\n" + out)
            return 1
        shutil.copyfile(tmp, _SUDOERS_PATH)
        os.chmod(_SUDOERS_PATH, 0o440)
        logger_mp.info(f"[Teleimager] Installed {_SUDOERS_PATH}")
    except OSError as e:
        logger_mp.error(f"[Teleimager] Cannot install the sudoers entry: {e}")
        return 1
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    logger_mp.info("[Teleimager] Done.")
    logger_mp.info("[Teleimager] Desktop sessions: UVC works now, nothing to restart.")
    if needs_relogin:
        # Existing sessions retain their group list until the user logs in again.
        logger_mp.info("[Teleimager] SSH: reconnect ('sshd' re-reads groups per "
                       "login).  Desktop terminal: log out and back in.")
    logger_mp.info("[Teleimager] Verify with:  teleimager-server --cf --uvc")
    return 0


def set_performance_mode(cores=[0, 1, 2]):
    """Request CPU affinity for the supplied cores and log any failure."""
    import psutil
    try:
        p = psutil.Process(os.getpid())

        # Request affinity through psutil for the current PID.
        p.cpu_affinity(cores)
        logger_mp.info(f"[Teleimager] CPU Affinity locked to: {cores}")

    except psutil.AccessDenied:
        logger_mp.warning("[Teleimager] Access Denied: Run as sudo for full optimization")
    except Exception as e:
        logger_mp.error(f"[Teleimager] Error: {e}")
