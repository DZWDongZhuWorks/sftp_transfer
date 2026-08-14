import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEPLOY_DIR = PROJECT_DIR / "deploy"
COMMON_SH = DEPLOY_DIR / "lib" / "offline_common.sh"


def run_bash(script, env=None):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(PROJECT_DIR),
        env=merged,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


class OfflineDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nssms-offline-test."))

    def tearDown(self):
        shutil.rmtree(str(self.temp_dir))

    def platform_env(self, version="18.04", glibc="2.27"):
        os_release = self.temp_dir / "os-release"
        os_release.write_text(
            'ID="ubuntu"\nVERSION_ID="{}"\n'.format(version), encoding="utf-8"
        )
        return {
            "NSSMS_TEST_OVERRIDES": "1",
            "NSSMS_OS_RELEASE_FILE": str(os_release),
            "NSSMS_ARCH_OVERRIDE": "arm64",
            "NSSMS_GLIBC_OVERRIDE": glibc,
        }

    def write_manifest(self, directory, filenames):
        lines = []
        for filename in filenames:
            path = directory / filename
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append("{}  {}\n".format(digest, filename))
        manifest = directory / "MANIFEST.txt"
        manifest.write_text("".join(lines), encoding="utf-8")
        return manifest

    def test_detects_bionic_profile(self):
        proc = run_bash(
            '. "{}"; nssms_detect_profile "{}"; '
            'printf "%s|%s" "$NSSMS_PROFILE_ID" "$NSSMS_GLIBC"'.format(
                COMMON_SH, DEPLOY_DIR / "platforms"
            ),
            env=self.platform_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue(proc.stdout.endswith("ubuntu-18.04-arm64|2.27"))

    def test_detects_jammy_profile(self):
        proc = run_bash(
            '. "{}"; nssms_detect_profile "{}"; printf "%s" "$NSSMS_PROFILE_ID"'.format(
                COMMON_SH, DEPLOY_DIR / "platforms"
            ),
            env=self.platform_env(version="22.04", glibc="2.35"),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue(proc.stdout.endswith("ubuntu-22.04-arm64"))

    def test_rejects_unknown_platform(self):
        proc = run_bash(
            '. "{}"; nssms_detect_profile "{}"'.format(
                COMMON_SH, DEPLOY_DIR / "platforms"
            ),
            env=self.platform_env(version="20.04", glibc="2.31"),
        )
        self.assertEqual(proc.returncode, 6)
        self.assertIn("不支援的平台", proc.stdout)

    def test_rejects_detection_override_outside_test_mode(self):
        env = self.platform_env()
        env.pop("NSSMS_TEST_OVERRIDES")
        proc = run_bash(
            '. "{}"; nssms_detect_profile "{}"'.format(
                COMMON_SH, DEPLOY_DIR / "platforms"
            ),
            env=env,
        )
        self.assertEqual(proc.returncode, 6)
        self.assertIn("override 只允許測試使用", proc.stdout)

    def test_manifest_works_with_bionic_mawk_and_requires_every_payload(self):
        (self.temp_dir / "one.deb").write_bytes(b"one")
        manifest = self.write_manifest(self.temp_dir, ["one.deb"])
        good = run_bash(
            '. "{}"; nssms_verify_flat_manifest "{}" "{}" "*.deb" debs'.format(
                COMMON_SH, self.temp_dir, manifest
            )
        )
        self.assertEqual(good.returncode, 0, good.stdout)

        (self.temp_dir / "unlisted.deb").write_bytes(b"two")
        bad = run_bash(
            '. "{}"; nssms_verify_flat_manifest "{}" "{}" "*.deb" debs'.format(
                COMMON_SH, self.temp_dir, manifest
            )
        )
        self.assertEqual(bad.returncode, 4)
        self.assertIn("數量不一致", bad.stdout)

    def test_manifest_accepts_relative_asset_paths(self):
        relative_dir = os.path.relpath(str(self.temp_dir), str(PROJECT_DIR))
        (self.temp_dir / "one.deb").write_bytes(b"one")
        self.write_manifest(self.temp_dir, ["one.deb"])
        proc = run_bash(
            '. "{}"; nssms_verify_flat_manifest "{}" "{}/MANIFEST.txt" "*.deb" debs'.format(
                COMMON_SH, relative_dir, relative_dir
            )
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_manifest_rejects_malformed_non_comment_line(self):
        (self.temp_dir / "one.deb").write_bytes(b"one")
        manifest = self.write_manifest(self.temp_dir, ["one.deb"])
        with manifest.open("a", encoding="utf-8") as stream:
            stream.write("this is not a checksum entry\n")
        proc = run_bash(
            '. "{}"; nssms_verify_flat_manifest "{}" "{}" "*.deb" debs'.format(
                COMMON_SH, self.temp_dir, manifest
            )
        )
        self.assertEqual(proc.returncode, 4)
        self.assertIn("格式錯誤", proc.stdout)

    def test_both_profile_payloads_match_their_manifests(self):
        for profile in ("ubuntu-18.04-arm64", "ubuntu-22.04-arm64"):
            debs = DEPLOY_DIR / "platforms" / profile / "debs"
            proc = run_bash(
                '. "{}"; nssms_verify_flat_manifest "{}" "{}" "*.deb" "{}"'.format(
                    COMMON_SH, debs, debs / "MANIFEST.txt", profile
                )
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_tmux_profile_override_cannot_cross_os(self):
        proc = subprocess.run(
            [
                "bash",
                str(DEPLOY_DIR / "install_tmux_offline.sh"),
                "--check-only",
                "--profile-dir",
                str(DEPLOY_DIR / "platforms" / "ubuntu-22.04-arm64"),
            ],
            cwd=str(PROJECT_DIR),
            env=dict(os.environ, **self.platform_env()),
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(proc.returncode, 6)
        self.assertIn("指定 profile 與本機不符", proc.stdout)

    def test_missing_tmux_returns_5_when_bionic_payload_is_installable(self):
        notmux = self.temp_dir / "notmux-bin"
        notmux.mkdir()
        for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            for source in Path(directory).glob("*"):
                if source.name == "tmux" or (not source.exists() and not source.is_symlink()):
                    continue
                target = notmux / source.name
                if not target.exists():
                    try:
                        target.symlink_to(source)
                    except OSError:
                        pass

        env = dict(os.environ, **self.platform_env())
        env["PATH"] = str(notmux)
        proc = subprocess.run(
            [
                "bash",
                str(DEPLOY_DIR / "install_tmux_offline.sh"),
                "--check-only",
            ],
            cwd=str(PROJECT_DIR),
            env=env,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(proc.returncode, 5, proc.stdout)
        self.assertIn("profile 與 deb 均可安裝", proc.stdout)

    def test_rootless_install_mode_is_rejected_without_writing_home(self):
        fake_home = self.temp_dir / "rootless-home"
        fake_home.mkdir()
        env = dict(os.environ, **self.platform_env())
        env["HOME"] = str(fake_home)
        proc = subprocess.run(
            [
                "bash",
                str(DEPLOY_DIR / "install_tmux_offline.sh"),
                "--user-local",
            ],
            cwd=str(PROJECT_DIR),
            env=env,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(list(fake_home.iterdir()), [])

    def test_deploy_check_only_accepts_preinstalled_python36_and_is_read_only(self):
        fake_home = self.temp_dir / "home"
        fake_home.mkdir()
        fake_python = self.temp_dir / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"${2:-}\" in\n"
            "  *join*) printf '3.6.15\\n' ;;\n"
            "  *cp\\%d\\%d*) printf 'cp36\\n' ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        env = dict(os.environ, **self.platform_env())
        env["HOME"] = str(fake_home)
        proc = subprocess.run(
            [
                "bash",
                str(DEPLOY_DIR / "deploy_offline.sh"),
                "--check-only",
                "--python",
                str(fake_python),
            ],
            cwd=str(PROJECT_DIR),
            env=env,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("cp36；船端預安裝", proc.stdout)
        self.assertNotIn("portable Python", proc.stdout)
        self.assertEqual(list(fake_home.iterdir()), [])

    def test_rejects_unsafe_venv_target_before_preflight(self):
        proc = subprocess.run(
            [
                "bash",
                str(DEPLOY_DIR / "deploy_offline.sh"),
                "--check-only",
                "--venv",
                "/",
            ],
            cwd=str(PROJECT_DIR),
            env=dict(os.environ, **self.platform_env()),
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("拒絕把重要目錄當成 venv", proc.stdout)

    def test_release_has_no_portable_python_payload_contract(self):
        source = (DEPLOY_DIR / "deploy_offline.sh").read_text(encoding="utf-8")
        self.assertNotIn("install_python_offline", source)
        self.assertNotIn("assets/common/python", source)
        self.assertFalse((DEPLOY_DIR / "install_python_offline.sh").exists())


if __name__ == "__main__":
    unittest.main()
