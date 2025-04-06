#!/usr/bin/python3
import logging
import threading
import os
import subprocess
import shutil
import shlex
import pwd
import re
from datetime import datetime
from pathlib import Path

import gi  # type: ignore[import]
from packaging.version import parse as parse_version

gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("Flatpak", "1.0")

#from yumex.constants import BACKEND  # type: ignore[import]
# still need to repair DNF5, use DNF4 for now
BACKEND = "DNF4"

if BACKEND == "DNF5":
    from nobara_updater.dnf5 import PackageUpdater, updatechecker  # type: ignore[import]
else:
    from nobara_updater.dnf4 import PackageUpdater, updatechecker  # type: ignore[import]


class QuirkFixup:
    def __init__(self, logger=None):
        self.logger = logger if logger else logging.getLogger("nobara-updater.quirks")

    def system_quirk_fixup(self):
        package_names = updatechecker()
        action = "upgrade"
        perform_kernel_actions = 0
        perform_reboot_request = 0
        perform_refresh = 0
        # START QUIRKS LIST

        # QUIRK 0: Make sure to refresh the repositories and gpg-keys before anything
        self.logger.info("QUIRK: Make sure to refresh the repositories and gpg-keys before anything.")
        critical_packages = [
            "fedora-gpg-keys",
            "nobara-gpg-keys",
            "fedora-repos",
            "nobara-repos",
        ]
        if any(pkg in package_names for pkg in critical_packages):
            critical_updates = [
                pkg for pkg in package_names if pkg in critical_packages
            ]
            log_message = "Updates for repository packages detected: {}. Updating these first...\n".format(
                ", ".join(critical_updates)
            )
            subprocess.run("dnf update -y --refresh fedora-repos fedora-gpg-keys nobara-repos nobara-gpg-keys --nogpgcheck", shell=True, capture_output=True, text=True, check=True)
            if "fedora-repos" in package_names:
                package_names = [pkg for pkg in package_names if pkg != "fedora-repos"]
            if "fedora-gpg-keys" in package_names:
                package_names = [pkg for pkg in package_names if pkg != "fedora-gpg-keys"]
            if "nobara-repos" in package_names:
                package_names = [pkg for pkg in package_names if pkg != "nobara-repos"]
            if "nobara-gpg-keys" in package_names:
                package_names = [pkg for pkg in package_names if pkg != "nobara-gpg-keys"]
            perform_refresh = 1
            return (
                0,
                0,
                0,
                perform_refresh,
            )
            self.logger.info(log_message)
        # QUIRK 1: Make sure to update the updater itself and refresh before anything
        self.logger.info("QUIRK: Make sure to update the updater itself and refresh before anything.")
        if "nobara-updater" in package_names:
            log_message = "An update for the Update System app has been detected, updating self...\n"
            subprocess.run("dnf update -y --refresh nobara-updater --nogpgcheck", shell=True, capture_output=True, text=True, check=True)
            perform_refresh = 1
            self.logger.info(perform_refresh)
            return (
                0,
                0,
                0,
                perform_refresh,
            )
            self.logger.info(log_message)
        # QUIRK 2: Cleanup outdated kernel modules
        self.logger.info("QUIRK: Cleanup outdated kernel modules.")
        try:
            # Run the command and capture the output
            result = subprocess.run("ls /boot/ | grep vmlinuz | grep -v rescue", shell=True, capture_output=True, text=True, check=True)

            # Split the output into lines
            lines = result.stdout.strip().split('\n')

            # Extract version numbers by removing 'vmlinuz-' prefix
            versions = [line.replace('vmlinuz-', '') for line in lines if line.startswith('vmlinuz-')]

            # Run the ls command and capture the output
            result = subprocess.run(['ls', '/lib/modules'], capture_output=True, text=True, check=True)

            # Split the output into entries
            modules = result.stdout.strip().split()

            # Filter modules that do not match the kernel versions
            filtered_modules = [module for module in modules if module not in versions]

            # Remove filtered modules
            for directory in filtered_modules:
                if directory:  # Check if directory is not None or empty
                    dir_path = os.path.join('/lib/modules', directory)
                    if os.path.exists(dir_path):  # Check if the path exists
                        shutil.rmtree(dir_path)

        except subprocess.CalledProcessError as e:
            print(f"An error occurred: {e}")

        # QUIRK 3: Make sure to reinstall rpmfusion repos if they do not exist
        self.logger.info("QUIRK: Make sure to reinstall rpmfusion repos if they do not exist.")
        if (
            self.check_and_install_rpmfusion(
                "/etc/yum.repos.d/rpmfusion-free.repo", "rpmfusion-free-release"
            )
            == 1
        ):
            perform_refresh = 1
        if (
            self.check_and_install_rpmfusion(
                "/etc/yum.repos.d/rpmfusion-free-updates.repo", "rpmfusion-free-release"
            )
            == 1
        ):
            perform_refresh = 1
        if (
            self.check_and_install_rpmfusion(
                "/etc/yum.repos.d/rpmfusion-nonfree.repo", "rpmfusion-nonfree-release"
            )
            == 1
        ):
            perform_refresh = 1
        if (
            self.check_and_install_rpmfusion(
                "/etc/yum.repos.d/rpmfusion-nonfree-updates.repo",
                "rpmfusion-nonfree-release",
            )
            == 1
        ):
            perform_refresh = 1

        # QUIRK 5: Make sure to run both dracut and akmods if any kmods  or kernel packages were updated.
        self.logger.info("QUIRK: Make sure to run both dracut and akmods if any kmods  or kernel packages were updated.")
        # Check if any packages contain "kernel" or "akmod"
        kernel_kmod_packages = [
            pkg for pkg in package_names if "kernel" in pkg or "akmod" in pkg
        ]
        if kernel_kmod_packages:
            perform_kernel_actions = 1
            perform_reboot_request = 1

        # QUIRK 6: If kwin or mutter are being updated, ask for a reboot.
        self.logger.info("QUIRK: If kwin or mutter are being updated, ask for a reboot.")
        de_update_packages = [
            pkg for pkg in package_names if "kwin" in pkg or "mutter" in pkg
        ]
        if de_update_packages:
            perform_reboot_request = 1

        # QUIRK 7: Install InputPlumber for Controller input, install steam firmware for steamdecks. Cleanup old packages.
        remove_names = []
        updatelist  = []
        controller_update_config_path = '/etc/nobara/handheld_packages/autoupdate.conf'
        controller_update_config = True

        # Check if the file exists
        if os.path.exists(controller_update_config_path):
            try:
                # Open the file and read its contents
                with open(controller_update_config_path, 'r') as file:
                    content = file.read().strip()
                    if content == "disabled":
                        controller_update_config = False
            except Exception as e:
                self.logger.info(f"An error occurred while reading the file: {e}")

        if controller_update_config == True:
            self.logger.info("QUIRK: Install InputPlumber for Controller input, install steam firmware for steamdecks. Cleanup old packages.")

            # Remove any deprecated controller input handlers
            check_handygccs = subprocess.run(
                ["rpm", "-q", "HandyGCCS"], capture_output=True, text=True
            )
            if check_handygccs.returncode == 0:
                subprocess.run(
                    ["systemctl", "disable", "--now", "handycon"],
                    capture_output=True,
                    text=True,
                )
                remove_names.append("HandyGCCS")

            check_lgcd = subprocess.run(
                ["rpm", "-q", "lgcd"], capture_output=True, text=True
            )
            if check_lgcd.returncode == 0:
                remove_names.append("lgcd")

            check_rogue_enemy = subprocess.run(
                ["rpm", "-q", "rogue-enemy"], capture_output=True, text=True
            )
            if check_rogue_enemy.returncode == 0:
                remove_names.append("rogue-enemy")

            check_hhd = subprocess.run(
                ["rpm", "-q", "hhd"], capture_output=True, text=True
            )
            if check_hhd.returncode == 0:
                remove_names.append("hhd")

            check_hhd_ui = subprocess.run(
                ["rpm", "-q", "hhd-ui"], capture_output=True, text=True
            )
            if check_hhd_ui.returncode == 0:
                remove_names.append("hhd-ui")

            check_hhd_adjustor = subprocess.run(
                ["rpm", "-q", "adjustor"], capture_output=True, text=True
            )
            if check_hhd_adjustor.returncode == 0:
                remove_names.append("adjustor")

            # Install InputPlumber
            check_ip = subprocess.run(
                ["rpm", "-q", "inputplumber"], capture_output=True, text=True
            )
            if check_ip.returncode != 0:
                updatelist.append("inputplumber")

            # Install ROG Ally/X firmware if needed
            check_ally = subprocess.run(
                "dmesg | grep 'ROG Ally'", capture_output=True, text=True, shell=True
            )
            ally_detected = check_ally.returncode == 0
            if ally_detected:
                self.logger.info(
                    "Found ROG Ally, installing firmware"
                )

                rogfw_name = "rogally-firmware"
                check_rogfw = subprocess.run(
                    ["rpm", "-q", rogfw_name], capture_output=True, text=True
                )
                rogfw_installed = check_rogfw.returncode == 0
                # Remove it, it's upstreamed now'
                if rogfw_installed:
                    PackageUpdater([rogfw_name], "remove", None)

        check_gamescope_htpc = subprocess.run(
            ["rpm", "-q", "gamescope-htpc-common"], capture_output=True, text=True
        )
        gamescope_htpc_installed = check_gamescope_htpc.returncode == 0

        check_gamescope_session_common = subprocess.run(
            ["rpm", "-q", "gamescope-session-common"], capture_output=True, text=True
        )
        gamescope_session_common_installed = check_gamescope_session_common.returncode == 0
        if gamescope_htpc_installed:
            if not gamescope_session_common_installed:
                # Return to normal grub + plymouth first.
                plymouth_scripts_name = "plymouth-plugin-script"
                check_plymouth_scripts = subprocess.run(
                    ["rpm", "-q", plymouth_scripts_name], capture_output=True, text=True
                )
                plymouth_scripts_notinstalled = check_plymouth_scripts.returncode != 0
                if plymouth_scripts_notinstalled:
                    PackageUpdater(["plymouth-plugin-script"], "install", None)

                # Run the 'plymouth-set-default-theme' command and capture its output
                check_theme = subprocess.run(
                    ["plymouth-set-default-theme"],
                    capture_output=True,
                    text=True
                )
                if 'steamos' in check_theme.stdout:
                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["plymouth-set-default-theme", "bgrt"],
                        capture_output=True,
                        text=True,
                    )

                    subprocess.run(["dracut", "-f", "--regenerate-all"], check=True)

                    # Path to the grub configuration file
                    grub_file_path = "/etc/default/grub"  # Use the test file path

                    # Function to calculate SHA256 checksum
                    def calculate_sha256(file_path):
                        result = subprocess.run(
                            ["sha256sum", file_path], capture_output=True, text=True
                        )
                        return result.stdout.split()[0]  # Extract the checksum from the output

                    # Calculate SHA256 checksum before changes
                    sha256_before = calculate_sha256(grub_file_path)

                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["sed", "-i", "s/GRUB_TIMEOUT='0'/GRUB_TIMEOUT='5'/g", "/etc/default/grub"],
                        capture_output=True,
                        text=True,
                    )

                    # Lines to remove from the file
                    lines_to_remove = [
                        "GRUB_TIMEOUT_STYLE='hidden'",
                        "GRUB_HIDDEN_TIMEOUT='0'",
                        "GRUB_HIDDEN_TIMEOUT_QUIET='true'"
                    ]

                    # Read the current contents of the file
                    with open(grub_file_path, "r") as file:
                        current_contents = file.readlines()

                    # Filter out the lines to remove
                    updated_contents = [
                        line for line in current_contents if line.strip() not in lines_to_remove
                    ]

                    # Write the updated contents back to the file
                    with open(grub_file_path, "w") as file:
                        file.writelines(updated_contents)

                    # Calculate SHA256 checksum after changes
                    sha256_after = calculate_sha256(grub_file_path)

                    # Compare checksums
                    if sha256_before != sha256_after:
                        subprocess.run(
                            ["/usr/sbin/grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"],
                            capture_output=True,
                            text=True,
                        )
            else:
                # Fixup plymouth so it's more steamos-like
                plymouth_scripts_name = "plymouth-plugin-script"
                check_plymouth_scripts = subprocess.run(
                    ["rpm", "-q", plymouth_scripts_name], capture_output=True, text=True
                )
                plymouth_scripts_notinstalled = check_plymouth_scripts.returncode != 0
                if plymouth_scripts_notinstalled:
                    PackageUpdater(["plymouth-plugin-script"], "install", None)

                # Run the 'plymouth-set-default-theme' command and capture its output
                check_theme = subprocess.run(
                    ["plymouth-set-default-theme"],
                    capture_output=True,
                    text=True
                )
                if not 'steamos' in check_theme.stdout:
                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["plymouth-set-default-theme", "steamos"],
                        capture_output=True,
                        text=True,
                    )

                    subprocess.run(["dracut", "-f", "--regenerate-all"], check=True)

                    # Path to the grub configuration file
                    grub_file_path = "/etc/default/grub"  # Use the test file path

                    # Function to calculate SHA256 checksum
                    def calculate_sha256(file_path):
                        result = subprocess.run(
                            ["sha256sum", file_path], capture_output=True, text=True
                        )
                        return result.stdout.split()[0]  # Extract the checksum from the output

                    # Calculate SHA256 checksum before changes
                    sha256_before = calculate_sha256(grub_file_path)

                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["sed", "-i", "s/GRUB_TIMEOUT='5'/GRUB_TIMEOUT='0'/g", "/etc/default/grub"],
                        capture_output=True,
                        text=True,
                    )

                    # Lines to add to the file
                    lines_to_add = [
                        "GRUB_TIMEOUT_STYLE='hidden'",
                        "GRUB_HIDDEN_TIMEOUT='0'",
                        "GRUB_HIDDEN_TIMEOUT_QUIET='true'"
                    ]

                    # Read the current contents of the file
                    with open(grub_file_path, "r") as file:
                        current_contents = file.readlines()

                    # Function to check if a line exists and append it if not
                    def add_line_if_missing(line):
                        if line + "\n" not in current_contents:  # Ensure newline is considered
                            with open(grub_file_path, "a") as file:  # Open file in append mode
                                file.write(line + "\n")  # Append the line with a newline

                    # Check and add each line
                    for line in lines_to_add:
                        add_line_if_missing(line)

                    # Calculate SHA256 checksum after changes
                    sha256_after = calculate_sha256(grub_file_path)

                    # Compare checksums
                    if sha256_before != sha256_after:
                        subprocess.run(
                            ["/usr/sbin/grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"],
                            capture_output=True,
                            text=True,
                        )
        if gamescope_session_common_installed:
            if not gamescope_htpc_installed:
                # Return to normal grub + plymouth first.
                plymouth_scripts_name = "plymouth-plugin-script"
                check_plymouth_scripts = subprocess.run(
                    ["rpm", "-q", plymouth_scripts_name], capture_output=True, text=True
                )
                plymouth_scripts_notinstalled = check_plymouth_scripts.returncode != 0
                if plymouth_scripts_notinstalled:
                    PackageUpdater(["plymouth-plugin-script"], "install", None)

                # Run the 'plymouth-set-default-theme' command and capture its output
                check_theme = subprocess.run(
                    ["plymouth-set-default-theme"],
                    capture_output=True,
                    text=True
                )
                if 'steamos' in check_theme.stdout:
                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["plymouth-set-default-theme", "bgrt"],
                        capture_output=True,
                        text=True,
                    )

                    subprocess.run(["dracut", "-f", "--regenerate-all"], check=True)

                    # Path to the grub configuration file
                    grub_file_path = "/etc/default/grub"  # Use the test file path

                    # Function to calculate SHA256 checksum
                    def calculate_sha256(file_path):
                        result = subprocess.run(
                            ["sha256sum", file_path], capture_output=True, text=True
                        )
                        return result.stdout.split()[0]  # Extract the checksum from the output

                    # Calculate SHA256 checksum before changes
                    sha256_before = calculate_sha256(grub_file_path)

                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["sed", "-i", "s/GRUB_TIMEOUT='0'/GRUB_TIMEOUT='5'/g", "/etc/default/grub"],
                        capture_output=True,
                        text=True,
                    )

                    # Lines to remove from the file
                    lines_to_remove = [
                        "GRUB_TIMEOUT_STYLE='hidden'",
                        "GRUB_HIDDEN_TIMEOUT='0'",
                        "GRUB_HIDDEN_TIMEOUT_QUIET='true'"
                    ]

                    # Read the current contents of the file
                    with open(grub_file_path, "r") as file:
                        current_contents = file.readlines()

                    # Filter out the lines to remove
                    updated_contents = [
                        line for line in current_contents if line.strip() not in lines_to_remove
                    ]

                    # Write the updated contents back to the file
                    with open(grub_file_path, "w") as file:
                        file.writelines(updated_contents)

                    # Calculate SHA256 checksum after changes
                    sha256_after = calculate_sha256(grub_file_path)

                    # Compare checksums
                    if sha256_before != sha256_after:
                        subprocess.run(
                            ["/usr/sbin/grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"],
                            capture_output=True,
                            text=True,
                        )

        if len(remove_names) > 0:
            PackageUpdater(remove_names, "remove", None)

        if len(updatelist) > 0:
            PackageUpdater(updatelist, "install", None)

        # Also check if device is steamdeck, if so install jupiter packages
        check_galileo = subprocess.run(
            "dmesg | grep 'Galileo'", capture_output=True, text=True, shell=True
        )
        galileo_detected = check_galileo.returncode == 0

        check_jupiter = subprocess.run(
            "dmesg | grep 'Jupiter'", capture_output=True, text=True, shell=True
        )
        jupiter_detected = check_jupiter.returncode == 0

        if (galileo_detected or jupiter_detected):
            steamdeck_install = []

            jupiter_hw = "jupiter-hw-support"
            check_jupiter_hw = subprocess.run(
                ["rpm", "-q", jupiter_hw], capture_output=True, text=True
            )
            jupiter_hw_installed = check_jupiter_hw.returncode != 0
            if jupiter_hw_installed:
                steamdeck_install.append(jupiter_hw)

            jupiter_fan = "jupiter-fan-control"
            check_jupiter_fan = subprocess.run(
                ["rpm", "-q", jupiter_fan], capture_output=True, text=True
            )
            jupiter_fan_installed = check_jupiter_fan.returncode != 0
            if jupiter_fan_installed:
                steamdeck_install.append(jupiter_fan)

            steamdeck_dsp = "steamdeck-dsp"
            check_steamdeck_dsp = subprocess.run(
                ["rpm", "-q", steamdeck_dsp], capture_output=True, text=True
            )
            steamdeck_dsp_installed = check_steamdeck_dsp.returncode != 0
            if steamdeck_dsp_installed:
                steamdeck_install.append(steamdeck_dsp)

            steamdeck_firmware = "steamdeck-firmware"
            check_steamdeck_firmware = subprocess.run(
                ["rpm", "-q", steamdeck_firmware], capture_output=True, text=True
            )
            steamdeck_firmware_installed = check_steamdeck_firmware.returncode != 0
            if steamdeck_firmware_installed:
                steamdeck_install.append(steamdeck_firmware)

            if len(steamdeck_install) > 0:
                PackageUpdater(steamdeck_install, "install", None)

        # QUIRK 10: Problematic package cleanup
        self.logger.info("QUIRK: Problematic package cleanup.")
        problematic = [
            "unrar",
            "qt5-qtwebengine-freeworld",
            "qt6-qtwebengine-freeworld",
            "qgnomeplatform-qt6",
            "qgnomeplatform-qt5",
            "musescore",
            "okular5-libs",
            "fedora-workstation-repositories",
            "mesa-demos"
            "libheif-freeworld.x86_64",
            "libheif-freeworld.i686",
        ]
        problematic_names = []
        for package in problematic:
            problematic_check = subprocess.run(
                ["rpm", "-q", package], capture_output=True, text=True
            )
            if problematic_check.returncode == 0:
                problematic_names.append(package)
        if len(problematic_names) > 0:
            self.logger.info("Found problematic packages, removing...")
            PackageUpdater(problematic_names, "remove", None)

        problematic_2025 = [
            "plasma-workspace-geolocation",
            "plasma-workspace-geolocation-libs"
        ]
        for package in problematic_2025:
            problematic_check_2025 = subprocess.run(
                ["rpm", "-q", package], capture_output=True, text=True
            )
            if problematic_check_2025.returncode == 0:
                subprocess.run(["rpm", "-e", "--nodeps", package], capture_output=True, text=True)


        # QUIRK 13: Clear plasmashell cache if a plasma-workspace update is available
        self.logger.info("QUIRK: Clear plasmashell cache if a plasma-workspace update is available.")
        # Function to run the rpm command and get the output
        def check_update():
            try:
                # Run the dnf check-update command and filter for plasma-workspace
                result = subprocess.run(['dnf', 'check-update'], capture_output=True, text=True, check=True)
                # Use Python's string filtering instead of piping in the shell
                if 'plasma-workspace' in result.stdout:
                    return True
                return False
            except subprocess.CalledProcessError as e:
                print(f"Failed to run dnf command: {e}")
                return False

        # Function to get the list of all user home directories
        def get_all_user_home_directories():
            home_directories = []
            for user in pwd.getpwall():
                if user.pw_uid >= 1000:  # Filter out system users
                    home_directories.append(user.pw_dir)
            return home_directories

        # Function to check and delete qmlcache folder if older than install date
        def delete_qmlcache(home_dir):
            qmlcache_dir = os.path.join(home_dir, ".cache", "plasmashell", "qmlcache")
            if os.path.exists(qmlcache_dir):
                try:
                    shutil.rmtree(qmlcache_dir)
                    print(f"Deleted '{qmlcache_dir}' directory successfully")
                except Exception as e:
                    print(f"Failed to delete '{qmlcache_dir}': {e}")
            else:
                print(f"Directory '{qmlcache_dir}' does not exist")

        # Main script execution
        if check_update:
            for home_dir in get_all_user_home_directories():
                delete_qmlcache(home_dir)

        # QUIRK 14: Fix Nvidia epoch so it matches that of negativo17 for cross compatibility
        self.logger.info("QUIRK: Fix Nvidia epoch so it matches that of negativo17 for cross compatibility.")

        # Run the dnf list installed command and capture the output
        check_nvidia_wrong_epoch = subprocess.run(
            ["dnf4", "list", "installed"], capture_output=True, text=True
        )

        # Check if the command was successful
        if check_nvidia_wrong_epoch.returncode == 0:
            # Filter the output for lines containing 'nvidia' and '4:'
            output_lines = check_nvidia_wrong_epoch.stdout.splitlines()
            nvidia_wrong_epoch = any("nvidia" in line and "4:" in line for line in output_lines)

            # Print the result
            print("nvidia_wrong_epoch:", nvidia_wrong_epoch)

            # Proceed if nvidia_wrong_epoch is True
            if nvidia_wrong_epoch:
                check_chromium = subprocess.run(
                    ["rpm", "-q", "chromium"], capture_output=True, text=True
                )
                reinstall_chromium = 0
                if check_chromium == 0:
                    reinstall_chromium = 1

                # Remove old
                commands = [
                    ["dnf", "remove", "-y", "nvidia*"],
                    ["dnf", "remove", "-y", "kmod-nvidia*"],
                    ["dnf", "remove", "-y", "akmod-nvidia"],
                    ["dnf", "remove", "-y", "dkms-nvidia"],
                    ["rm", "-rf", "/var/lib/dkms/nvidia*"]
                ]

                for command in commands:
                    subprocess.run(command, capture_output=False, text=True)

                # Add new
                packages = [
                    "akmod-nvidia",
                    "nvidia-driver",
                    "libnvidia-ml",
                    "libnvidia-ml.i686",
                    "libnvidia-fbc",
                    "nvidia-driver-cuda",
                    "nvidia-driver-cuda-libs",
                    "nvidia-driver-cuda-libs.i686",
                    "nvidia-driver-libs",
                    "nvidia-driver-libs.i686",
                    "nvidia-kmod-common",
                    "nvidia-libXNVCtrl",
                    "nvidia-modprobe",
                    "nvidia-persistenced",
                    "nvidia-settings",
                    "nvidia-xconfig",
                    "nvidia-vaapi-driver",
                    "nvidia-gpu-firmware",
                    "libnvidia-cfg"
                ]

                if reinstall_chromium == 1:
                    packages.append("chromium")

                # Add the '--refresh' option at the end
                command = ["dnf", "install", "-y"] + packages + ["--refresh"]

                # Run the command
                subprocess.run(command)

                perform_kernel_actions = 1
                perform_reboot_request = 1

        # QUIRK 15: Swap old AMD ROCm packages with upstream Fedora ROCm versions.
        self.logger.info("QUIRK: Swap old AMD ROCm packages with upstream Fedora ROCm versions.")

        try:
            result = subprocess.run(
                "dnf4 list installed | grep @nobara-rocm-official",
                shell=True,
                capture_output=True,
                text=True
            )

            # Check if there is any output
            if result.stdout.strip():
                # Remove old ROCm packages
                old_rocm_removal = [
                    "comgr.x86_64",
                    "hip-devel.x86_64",
                    "hip-runtime-amd.x86_64",
                    "hipcc.x86_64",
                    "hsa-rocr.x86_64",
                    "hsa-rocr-devel.x86_64",
                    "hsakmt-roct-devel.x86_64",
                    "openmp-extras-runtime.x86_64",
                    "rocm-core.x86_64",
                    "rocm-device-libs.x86_64",
                    "rocm-hip-runtime.x86_64",
                    "rocm-language-runtime.x86_64",
                    "rocm-llvm.x86_64",
                    "rocm-opencl.x86_64",
                    "rocm-opencl-icd-loader.x86_64",
                    "rocm-opencl-runtime.x86_64",
                    "rocm-smi-lib.x86_64",
                    "rocminfo.x86_64",
                    "rocprofiler-register.x86_64",
                    "rocm-meta",
                ]
                PackageUpdater(old_rocm_removal, "remove", None)
                # Now reinstall new rocm-meta
                PackageUpdater(["rocm-meta"], "install", None)

        except Exception as e:
            print(f"An error occurred: {e}")

        # QUIRK 16: mesa-vulkan-drivers fixup
        self.logger.info("QUIRK: mesa-vulkan-drivers fixup.")
        try:
            result = subprocess.run(
                "rpm -q mesa-vulkan-drivers | grep git",
                shell=True,
                capture_output=True,
                text=True
            )

            # Check if there is any output
            if result.stdout.strip():
                self.logger.info("mesa-vulkan-drivers fixup.")
                subprocess.run(
                    ["rpm", "-e", "--nodeps", "mesa-vulkan-drivers.x86_64"], capture_output=True, text=True
                )
                subprocess.run(
                    ["rpm", "-e", "--nodeps", "mesa-vulkan-drivers.i686"], capture_output=True, text=True
                )
                subprocess.run(
                    ["dnf4", "install", "-y", "mesa-vulkan-drivers.x86_64", "mesa-vulkan-drivers.i686"], capture_output=True, text=True
                )
        except Exception as e:
            print(f"An error occurred: {e}")

        # QUIRK 18: vaapi fixup
        self.logger.info("QUIRK: vaapi fixup.")
        mesa_fixup_check = subprocess.run(
            ["rpm", "-q", "mesa-libgallium-freeworld.x86_64"], capture_output=True, text=True
        )
        mesa_fixup_check1 = subprocess.run(
            ["rpm", "-q", "mesa-libgallium-freeworld.i686"], capture_output=True, text=True
        )
        mesa_fixup_check2 = subprocess.run(
            ["rpm", "-q", "mesa-va-drivers-freeworld.x86_64"], capture_output=True, text=True
        )
        mesa_fixup_check3 = subprocess.run(
            ["rpm", "-q", "mesa-va-drivers-freeworld.i686"], capture_output=True, text=True
        )
        mesa_fixup_check4 = subprocess.run(
            ["rpm", "-q", "mesa-va-drivers-freeworld.x86_64"], capture_output=True, text=True
        )
        mesa_fixup_check5 = subprocess.run(
            ["rpm", "-q", "mesa-va-drivers-freeworld.i686"], capture_output=True, text=True
        )

        mesa_fixup_check6 = subprocess.run(
            ["rpm", "-q", "mesa-libgallium.x86_64"], capture_output=True, text=True
        )
        mesa_fixup_check7 = subprocess.run(
            ["rpm", "-q", "mesa-libgallium.i686"], capture_output=True, text=True
        )
        mesa_fixup_check8 = subprocess.run(
            ["rpm", "-q", "mesa-va-drivers.x86_64"], capture_output=True, text=True
        )
        mesa_fixup_check9 = subprocess.run(
            ["rpm", "-q", "mesa-va-drivers.i686"], capture_output=True, text=True
        )
        mesa_fixup_check10 = subprocess.run(
            ["rpm", "-q", "mesa-va-drivers.x86_64"], capture_output=True, text=True
        )
        mesa_fixup_check11 = subprocess.run(
            ["rpm", "-q", "mesa-va-drivers.i686"], capture_output=True, text=True
        )
        # they should all either end in -freeworld or not, no mixing.
        if not (
            mesa_fixup_check.returncode == 0
            and mesa_fixup_check1.returncode == 0
            and mesa_fixup_check2.returncode == 0
            and mesa_fixup_check3.returncode == 0
            and mesa_fixup_check4.returncode == 0
            and mesa_fixup_check5.returncode == 0
        ):
            # If all of them are not freeworld, check if they are all standard:
            if not (
                mesa_fixup_check6.returncode == 0
                and mesa_fixup_check7.returncode == 0
                and mesa_fixup_check8.returncode == 0
                and mesa_fixup_check9.returncode == 0
                and mesa_fixup_check10.returncode == 0
                and mesa_fixup_check11.returncode == 0
            ):

                # looks like we have a mix of both, let's check if -any- of them are freeworld:
                if not (
                    # If at least one of them is freeworld, correct all to freeworld
                    mesa_fixup_check.returncode == 0
                    or mesa_fixup_check1.returncode == 0
                    or mesa_fixup_check2.returncode == 0
                    or mesa_fixup_check3.returncode == 0
                    or mesa_fixup_check4.returncode == 0
                    or mesa_fixup_check5.returncode == 0
                ):
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-libgallium.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-libgallium.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-libgallium-freeworld.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-libgallium-freeworld.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-va-drivers.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-va-drivers.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-va-drivers-freeworld.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-va-drivers-freeworld.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-vdpau-drivers.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-vdpau-drivers.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-vdpau-drivers-freeworld.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-vdpau-drivers-freeworld.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["dnf", "install", "-y", "mesa-libgallium-freeworld.x86_64", "mesa-libgallium-freeworld.i686", "mesa-va-drivers-freeworld.x86_64", "mesa-va-drivers-freeworld.i686", "mesa-vdpau-drivers-freeworld.x86_64", "mesa-vdpau-drivers-freeworld.i686", "--refresh"],
                        capture_output=True, text=True
                    )
                # Otherwise correct to original
                else:
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-libgallium.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-libgallium.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-libgallium-freeworld.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-libgallium-freeworld.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-va-drivers.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-va-drivers.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-va-drivers-freeworld.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-va-drivers-freeworld.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-vdpau-drivers.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-vdpau-drivers.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-vdpau-drivers-freeworld.x86_64"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["rpm", "-e", "--nodeps", "mesa-vdpau-drivers-freeworld.i686"], capture_output=True, text=True
                    )
                    subprocess.run(
                        ["dnf", "install", "-y", "mesa-libgallium.x86_64", "mesa-libgallium.i686", "mesa-va-drivers.x86_64", "mesa-va-drivers.i686", "mesa-vdpau-drivers.x86_64", "mesa-vdpau-drivers.i686", "--refresh"],
                        capture_output=True, text=True
                    )

        # QUIRK 18: Kernel 6.12.9 fixup
        self.logger.info("QUIRK: Kernel fsync->nobara conversion update.")
        try:
            # Get the full kernel version
            full_version = subprocess.run(['uname', '-r'], capture_output=True, text=True, check=True)

            # Access the captured output
            version_output = full_version.stdout.strip()

            if "fsync" in version_output:
                subprocess.run(['dnf', 'remove', 'kernel-uki-virt*', '-y'], capture_output=True, text=True, check=True)
                subprocess.run(['dnf', 'update', 'kernel', '-y'], capture_output=True, text=True, check=True)
                subprocess.run(['dnf', 'update', 'kernel-devel', '-y'], capture_output=True, text=True, check=True)
                perform_kernel_actions = 1
                perform_reboot_request = 1

            target_version = "6.12.11-204.nobara.fc41.x86_64"
            if "nobara" in version_output:
                if version_output < target_version:
                    checkpending = subprocess.run(['rpm', '-q', f'kernel-{target_version}'], capture_output=True, text=True, check=True)
                    checkpending_output = checkpending.stdout.strip()
                    if "not installed" in checkpending_output:
                        try:
                            subprocess.run(['dnf', 'install', "-y", f'kernel-{target_version}'], check=True)
                            subprocess.run(['dnf', 'install', "-y", f'kernel-devel-{target_version}'], check=True)
                            perform_kernel_actions = 1
                            perform_reboot_request = 1
                        except subprocess.CalledProcessError as e:
                            self.logger.info(f"Error installing new kernel: {e}")
                else:
                    self.logger.info(f"Current kernel version ({version_output}) is already up to date.")

        except subprocess.CalledProcessError as e:
            self.logger.info(f"An error occurred: {e}")

        # QUIRK 18: Media fixup
        media_fixup = 0
        if "gamescope" not in os.environ.get('XDG_CURRENT_DESKTOP', '').lower():
            self.logger.info("QUIRK: Media fixup.")
            media = [
                "ffmpeg-libs.x86_64",
                "ffmpeg-libs.i686",
                "x264.x86_64",
                "x265.x86_64",
                "noopenh264.x86_64",
                "noopenh264.i686",
                "libavcodec-free.x86_64",
                "libavcodec-free.i686",
                "mesa-va-drivers.x86_64",
                "mesa-vdpau-drivers.x86_64",
                "x264-libs.x86_64",
                "x264-libs.i686",
                "x265-libs.x86_64",
                "x265-libs.i686",
                "libavcodec-freeworld.x86_64",
                "libavcodec-freeworld.i686",
                "openh264.x86_64",
                "openh264.i686",
                "mesa-va-drivers-freeworld.x86_64",
                "mesa-vdpau-drivers-freeworld.x86_64",
                "gstreamer1-plugins-bad-free-extras.x86_64",
                "gstreamer1-plugins-bad-free-extras.i686",
                "mozilla-openh264.x86_64",
                "mesa-demos",
            ]

            for package in media:
                media_fixup_check = subprocess.run(
                    ["rpm", "-q", package], capture_output=True, text=True
                )
                if package in [
                    "x264-libs.x86_64",
                    "x264-libs.i686",
                    "x265-libs.x86_64",
                    "x265-libs.i686",
                    "libavcodec-freeworld.x86_64",
                    "libavcodec-freeworld.i686",
                    "openh264.x86_64",
                    "openh264.i686",
                    "mesa-va-drivers-freeworld.x86_64",
                    "mesa-vdpau-drivers-freeworld.x86_64",
                    "gstreamer1-plugins-bad-free-extras.x86_64",
                    "gstreamer1-plugins-bad-free-extras.i686",
                    "mozilla-openh264.x86_64",
                ]:
                    if media_fixup_check.returncode != 0:
                        media_fixup = 1
                        break
                else:
                    if package in [
                        "ffmpeg-libs.x86_64",
                        "ffmpeg-libs.i686",
                        "x264.x86_64",
                        "x265.x86_64",
                        "noopenh264.x86_64",
                        "noopenh264.i686",
                        "libavcodec-free.x86_64",
                        "libavcodec-free.i686",
                        "mesa-va-drivers.x86_64",
                        "mesa-vdpau-drivers.x86_64",
                    ]:
                        if media_fixup_check.returncode == 0:
                            media_fixup = 1
                            break

        # END QUIRKS LIST
        # Check if any packages contain "kernel" or "akmod"
        if "gamescope" in os.environ.get('XDG_CURRENT_DESKTOP', '').lower():
            gamescope_packages = [
                pkg for pkg in package_names if "gamescope" in pkg
            ]
            if gamescope_packages:
                perform_reboot_request = 1

        # Remove newinstall needs-update tracker
        if Path.exists(Path("/etc/nobara/newinstall")):
            try:
                # Remove the file
                Path("/etc/nobara/newinstall").unlink()
            except OSError as e:
                logger.error("Error: %s", e.strerror)

        return (
            perform_kernel_actions,
            perform_reboot_request,
            media_fixup,
            perform_refresh,
        )

    def update_core_packages(
        self, package_list: list[str], action: str, log_message: str
    ) -> None:
        self.logger.info(log_message)

        # Run the updater in a separate thread and wait for it to finish
        updater_thread = threading.Thread(
            target=self.run_package_updater, args=(package_list, action)
        )
        updater_thread.start()
        updater_thread.join()  # Wait for the updater thread to finish

    def check_and_install_rpmfusion(self, filepath: str, packagename: str) -> int:
        # Check if the specified file exists
        if not Path(filepath).exists():
            self.logger.info(
                "%s not found. Checking for %s package...\n", filepath, packagename
            )

            # Check if the specified package is installed
            result = subprocess.run(
                ["rpm", "-q", packagename], capture_output=True, text=True
            )
            if result.returncode == 0:
                self.logger.info("%s is installed. Reinstalling...\n", packagename)
                updater_thread = threading.Thread(
                    target=self.run_package_updater, args=([packagename], "remove")
                )
                updater_thread.start()
                updater_thread.join()
                updater_thread = threading.Thread(
                    target=self.run_package_updater, args=([packagename], "install")
                )
                updater_thread.start()
                updater_thread.join()
            else:
                self.logger.info("%s is not installed. Installing...\n", packagename)
                updater_thread = threading.Thread(
                    target=self.run_package_updater, args=([packagename], "install")
                )
                updater_thread.start()
                updater_thread.join()
            return 1
        return 0

    def run_package_updater(self, package_names: list[str], action: str) -> None:
        # Initialize the PackageUpdater
        PackageUpdater(package_names, action, None)
