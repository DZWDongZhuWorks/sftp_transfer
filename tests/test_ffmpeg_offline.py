"""deploy/install_ffmpeg_offline.sh —— 靜態 ffmpeg / ffprobe 離線安裝到 /usr/local/bin。

全部裝進暫存 prefix(NSSMS_FFMPEG_PREFIX,只在 NSSMS_TEST_OVERRIDES=1 時收),不碰真的 /usr/local。
用真離線包的幾項只在 aarch64 上跑(CI 是 ubuntu-22.04-arm;船隊全是 Jetson)。
"""
import hashlib
import io
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEPLOY_DIR = PROJECT_DIR / "deploy"
INSTALLER = DEPLOY_DIR / "install_ffmpeg_offline.sh"
BUNDLE_DIR = DEPLOY_DIR / "static" / "ffmpeg"
DEPLOY_SH = DEPLOY_DIR / "deploy_offline.sh"
COMMON_SH = DEPLOY_DIR / "lib" / "offline_common.sh"

ON_ARM64 = platform.machine() in ("aarch64", "arm64")


def run_installer(args=(), env=None):
    merged = os.environ.copy()
    for k in ("NSSMS_TEST_OVERRIDES", "NSSMS_FFMPEG_BUNDLE_DIR", "NSSMS_FFMPEG_PREFIX",
              "NSSMS_ARCH_OVERRIDE"):
        merged.pop(k, None)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(INSTALLER)] + list(args),
        cwd=str(PROJECT_DIR),
        env=merged,
        stdin=subprocess.DEVNULL,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nssms-ffmpeg-test."))
        self.prefix = self.tmp / "prefix"
        self.prefix.mkdir()

    def tearDown(self):
        shutil.rmtree(str(self.tmp))

    def env(self, bundle=None, **extra):
        e = {
            "NSSMS_TEST_OVERRIDES": "1",
            "NSSMS_FFMPEG_PREFIX": str(self.prefix),
            "NSSMS_FFMPEG_BUNDLE_DIR": str(bundle or BUNDLE_DIR),
        }
        e.update(extra)
        return e


class BundleTests(Base):
    """隨附的真離線包。"""

    def test_bundle_matches_manifest_and_has_exactly_one_tarball(self):
        tarballs = sorted(BUNDLE_DIR.glob("*.tar.xz"))
        self.assertEqual(len(tarballs), 1, tarballs)
        proc = subprocess.run(
            ["bash", "-c", '. "{}"; nssms_verify_flat_manifest "{}" "{}" "*.tar.xz" ffmpeg'.format(
                COMMON_SH, BUNDLE_DIR, BUNDLE_DIR / "MANIFEST.txt")],
            universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_tarball_layout_and_inner_checksums(self):
        tarball = next(BUNDLE_DIR.glob("*.tar.xz"))
        with tarfile.open(str(tarball), "r:xz") as tf:
            names = tf.getnames()
            top = {n.split("/", 1)[0] for n in names}
            self.assertEqual(len(top), 1, top)
            root = top.pop()
            for f in ("bin/ffmpeg", "bin/ffprobe", "BUILDINFO.txt", "COPYING.GPLv2", "SHA256SUMS"):
                self.assertIn(root + "/" + f, names)
            for m in tf.getmembers():
                self.assertFalse(m.name.startswith("/") or ".." in m.name.split("/"), m.name)
                self.assertTrue(m.isfile() or m.isdir(), m.name)   # 沒有連結 / 裝置檔
            sums = tf.extractfile(root + "/SHA256SUMS").read().decode()
            for line in sums.splitlines():
                digest, name = line.split("  ", 1)
                data = tf.extractfile(root + "/" + name).read()
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)
            info = tf.extractfile(root + "/BUILDINFO.txt").read().decode()
            self.assertRegex(info, r"source_sha256: [0-9a-f]{64}")
            self.assertIn("--enable-libx264", info)

    def test_build_recipe_is_versioned_next_to_the_bundle(self):
        for f in ("README.md", "fetch.sh", "build_in_container.sh", "package.sh"):
            self.assertTrue((BUNDLE_DIR / "build" / f).is_file(), f)


@unittest.skipUnless(ON_ARM64, "真離線包是 linux arm64 執行檔")
class RealInstallTests(Base):
    def test_check_only_reports_installable_and_writes_nothing(self):
        proc = run_installer(["--check-only"], self.env())
        self.assertEqual(proc.returncode, 5, proc.stdout)
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_install_then_idempotent(self):
        proc = run_installer([], self.env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for t in ("ffmpeg", "ffprobe"):
            p = self.prefix / "bin" / t
            self.assertTrue(p.is_file(), t)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o755, t)
        mark = self.prefix / "share" / "nssms-ffmpeg"
        for f in ("BUILDINFO.txt", "COPYING.GPLv2", "SHA256SUMS"):
            self.assertTrue((mark / f).is_file(), f)
        self.assertEqual([p.name for p in (self.prefix / "bin").iterdir() if p.name.startswith(".")], [])

        enc = subprocess.run([str(self.prefix / "bin" / "ffmpeg"), "-hide_banner", "-encoders"],
                             universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertIn(" libx264rgb ", enc.stdout)

        again = run_installer([], self.env())
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("已是本包這一版", again.stdout)
        chk = run_installer(["--check-only"], self.env())
        self.assertEqual(chk.returncode, 0, chk.stdout)

    def test_lossless_roundtrip_of_one_pixel_lines(self):
        """與 radar 長錄影同一組參數:1 px 線條經 libx264rgb qp 0 逐位元無損。"""
        self.assertEqual(run_installer([], self.env()).returncode, 0)
        ff = str(self.prefix / "bin" / "ffmpeg")
        w, h = 64, 48
        frames = []
        for i in range(3):
            buf = bytearray(w * h * 3)
            for x in range(w):                       # 1 px 橫線,每幀換顏色與位置
                o = ((10 + i) * w + x) * 3
                buf[o:o + 3] = bytes(((37 * i) % 256, 200, (90 + 50 * i) % 256))
            for y in range(h):                       # 1 px 直線
                o = (y * w + 5 + 2 * i) * 3
                buf[o:o + 3] = b"\x00\xff\x01"
            frames.append(bytes(buf))
        mp4 = str(self.tmp / "t.mp4")
        enc = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                              "-pix_fmt", "bgr24", "-s", "%dx%d" % (w, h), "-framerate", "1/3",
                              "-i", "pipe:0", "-c:v", "libx264rgb", "-preset", "veryfast",
                              "-refs", "4", "-qp", "0", "-pix_fmt", "bgr24", mp4],
                             input=b"".join(frames), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(enc.returncode, 0, enc.stderr)
        dec = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", mp4, "-f", "rawvideo",
                              "-pix_fmt", "bgr24", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(dec.returncode, 0, dec.stderr)
        self.assertEqual(dec.stdout, b"".join(frames))
        probe = subprocess.run([str(self.prefix / "bin" / "ffprobe"), "-v", "error", "-select_streams",
                                "v:0", "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", mp4],
                               universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(probe.stdout.strip(), "gbrp")

    def test_binaries_are_fully_static(self):
        """不依賴 glibc:Ubuntu 18.04(glibc 2.27)與 22.04 同一支。"""
        self.assertEqual(run_installer([], self.env()).returncode, 0)
        for t in ("ffmpeg", "ffprobe"):
            data = (self.prefix / "bin" / t).read_bytes()
            self.assertNotIn(b"ld-linux-aarch64.so", data, t)   # 沒有 PT_INTERP → 不需要動態載入器
            self.assertNotIn(b"libc.so.6", data, t)

    def test_foreign_ffmpeg_is_left_alone(self):
        (self.prefix / "bin").mkdir()
        foreign = self.prefix / "bin" / "ffmpeg"
        foreign.write_text("#!/bin/sh\necho someone else's\n")
        before = sha256(foreign)
        for args in (["--check-only"], []):
            proc = run_installer(args, self.env())
            self.assertEqual(proc.returncode, 8, proc.stdout)
            self.assertIn("不是本包裝的", proc.stdout)
        self.assertEqual(sha256(foreign), before)
        self.assertFalse((self.prefix / "bin" / "ffprobe").exists())
        self.assertFalse((self.prefix / "share" / "nssms-ffmpeg").exists())

    def test_interrupted_install_is_resumed_not_called_foreign(self):
        """換版途中斷:記號目錄在、SHA256SUMS 已刪、執行檔是舊的 → 要重裝,不是 exit 8。"""
        (self.prefix / "bin").mkdir()
        (self.prefix / "share" / "nssms-ffmpeg").mkdir(parents=True)
        (self.prefix / "bin" / "ffmpeg").write_text("#!/bin/sh\nexit 1\n")
        proc = run_installer(["--check-only"], self.env())
        self.assertEqual(proc.returncode, 5, proc.stdout)
        proc = run_installer([], self.env())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(" libx264rgb ", subprocess.run(
            [str(self.prefix / "bin" / "ffmpeg"), "-hide_banner", "-encoders"],
            universal_newlines=True, stdout=subprocess.PIPE).stdout)

    def test_tampered_bundle_is_rejected(self):
        bundle = self.tmp / "bundle"
        shutil.copytree(str(BUNDLE_DIR), str(bundle), ignore=shutil.ignore_patterns("build"))
        tarball = next(bundle.glob("*.tar.xz"))
        data = bytearray(tarball.read_bytes())
        data[len(data) // 2] ^= 0xFF
        tarball.write_bytes(bytes(data))
        proc = run_installer([], self.env(bundle=bundle))
        self.assertEqual(proc.returncode, 4, proc.stdout)
        self.assertEqual(list(self.prefix.iterdir()), [])


class FakeBundleTests(Base):
    """用 shell 腳本假扮的離線包測邏輯(任何架構都能跑)。"""

    def make_bundle(self, encoders="libx264rgb", encode_ok=True, drop=None):
        bundle = self.tmp / "fake-bundle"
        bundle.mkdir()
        root = "ffmpeg-0.0-test"
        ffmpeg = (
            "#!/bin/sh\n"
            'case " $* " in\n'
            '  *" -encoders "*) echo " V..... {enc} fake";;\n'
            '  *" -version "*) echo "ffmpeg version 0.0-test";;\n'
            '  *) cat >/dev/null; for a; do last="$a"; done; {enc_cmd};;\n'
            "esac\n"
        ).format(enc=encoders, enc_cmd='printf x > "$last"' if encode_ok else "exit 1")
        ffprobe = (
            "#!/bin/sh\n"
            'case " $* " in *" -version "*) echo "ffprobe version 0.0-test";; *) echo 2;; esac\n'
        )
        files = {"bin/ffmpeg": ffmpeg, "bin/ffprobe": ffprobe,
                 "BUILDINFO.txt": "version: ffmpeg 0.0-test\n", "COPYING.GPLv2": "GPL\n"}
        sums = "".join("{}  {}\n".format(hashlib.sha256(files[k].encode()).hexdigest(), k)
                       for k in ("bin/ffmpeg", "bin/ffprobe"))
        files["SHA256SUMS"] = sums
        if drop:
            files.pop(drop)
        tarball = bundle / (root + ".tar.xz")
        with tarfile.open(str(tarball), "w:xz") as tf:
            for name, text in sorted(files.items()):
                data = text.encode()
                ti = tarfile.TarInfo(root + "/" + name)
                ti.size = len(data)
                ti.mode = 0o755 if name.startswith("bin/") else 0o644
                tf.addfile(ti, io.BytesIO(data))
        (bundle / "MANIFEST.txt").write_text("{}  {}\n".format(sha256(tarball), tarball.name))
        return bundle

    def test_fake_bundle_installs(self):
        proc = run_installer([], self.env(bundle=self.make_bundle(), NSSMS_ARCH_OVERRIDE="aarch64"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue((self.prefix / "share" / "nssms-ffmpeg" / "SHA256SUMS").is_file())

    def test_missing_libx264rgb_is_rejected(self):
        proc = run_installer([], self.env(bundle=self.make_bundle(encoders="libx264"),
                                          NSSMS_ARCH_OVERRIDE="aarch64"))
        self.assertEqual(proc.returncode, 6, proc.stdout)
        self.assertIn("沒有 libx264rgb", proc.stdout)
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_failed_trial_encode_is_rejected(self):
        proc = run_installer([], self.env(bundle=self.make_bundle(encode_ok=False),
                                          NSSMS_ARCH_OVERRIDE="aarch64"))
        self.assertEqual(proc.returncode, 6, proc.stdout)
        self.assertIn("試編失敗", proc.stdout)

    def test_missing_member_is_rejected(self):
        proc = run_installer([], self.env(bundle=self.make_bundle(drop="COPYING.GPLv2"),
                                          NSSMS_ARCH_OVERRIDE="aarch64"))
        self.assertEqual(proc.returncode, 4, proc.stdout)

    def test_wrong_arch_is_rejected(self):
        proc = run_installer([], self.env(bundle=self.make_bundle(), NSSMS_ARCH_OVERRIDE="x86_64"))
        self.assertEqual(proc.returncode, 6, proc.stdout)
        self.assertIn("arm64", proc.stdout)

    def test_overrides_require_test_mode(self):
        env = self.env(bundle=self.make_bundle())
        env.pop("NSSMS_TEST_OVERRIDES")
        proc = run_installer(["--check-only"], env)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("只允許測試使用", proc.stdout)

    def test_unknown_argument(self):
        self.assertEqual(run_installer(["--bogus"]).returncode, 2)


class DeployWiringTests(unittest.TestCase):
    """deploy_offline.sh 有接上這一步、兩份總結都有這一欄。"""

    def setUp(self):
        self.src = DEPLOY_SH.read_text(encoding="utf-8")

    def test_status_initialised_at_file_scope(self):
        self.assertRegex(self.src, r'(?m)^FFMPEG_STATUS="未執行"$')

    def test_main_calls_stage_after_tracker(self):
        main = self.src[self.src.index("\nmain() {"):]
        self.assertIn("stage_ffmpeg", main)
        self.assertLess(main.index("stage_gnome_tracker"), main.index("stage_ffmpeg"))
        self.assertLess(main.index("stage_ffmpeg"), main.index("stage_launch_decision"))

    def test_both_summaries_show_ffmpeg(self):
        self.assertIn('"$FFMPEG_STATUS"', self.src)
        self.assertIn('"${FFMPEG_STATUS:-未執行}"', self.src)

    def test_stage_never_exits(self):
        body = re.search(r"\nstage_ffmpeg\(\) \{\n(.*?)\n\}\n", self.src, re.S).group(1)
        # exit 指令(行首或接在 ; && || 之後),不是訊息裡的「exit=$RC」
        self.assertNotRegex(body, r"(?m)(^|[;&|])\s*exit(\s|;|$)")
        self.assertIn('mutating "', body)
        self.assertIn("sudo_obtainable", body)


if __name__ == "__main__":
    unittest.main()
