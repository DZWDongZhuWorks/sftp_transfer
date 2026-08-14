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
        # 版本查詢一律回答 3.6，其餘（例如 lib/wheel_compat.py 這種真的要執行的腳本）
        # 轉交給真實的 python3。preflight 現在會拿 wheelhouse 去比對直譯器版本，
        # 只會回答版本的殼子已經不夠用了 —— 但「假裝是 3.6」這個測試意圖不變：
        # 目標版本是由 --py 明確傳給 checker 的，不是由執行它的直譯器決定。
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"${2:-}\" in\n"
            "  *join*) printf '3.6.15\\n'; exit 0 ;;\n"
            "  *cp\\%d\\%d*) printf 'cp36\\n'; exit 0 ;;\n"
            "  *version_info\\[:2\\]*) printf '3.6\\n'; exit 0 ;;\n"
            "esac\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in *.py) exec /usr/bin/env python3 \"$@\" ;; esac\n"
            "done\n"
            "exit 1\n",
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


class WheelCompatTests(unittest.TestCase):
    """wheel_compat.py 的守門契約。

    這支守門存在的理由是一次真實事故的重現：Bionic（py3.6 / glibc 2.27）對著 cp310 +
    manylinux_2_34 的 wheelhouse 跑 --check-only 會回 0，宣稱全部通過，然後在階段 B 的
    pip 才失敗 —— 而階段 A 已經改完 systemd / sudoers / tmux。更糟的是沒裝完的 venv 會讓
    兩支 OTA 腳本的 `[ -x $VENV_PY ]` 守門失效（venv 在、paramiko 不在），那條船就失去
    唯一的下載路徑。所以下面每一項都在測「不相容時必須非 0」，而不是只測正向。
    """

    RUNTIME = ["paramiko", "bcrypt", "cryptography", "pynacl", "cffi", "pycparser"]

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nssms-wheelcompat-test."))
        self.wh = self.temp_dir / "wheelhouse"
        self.wh.mkdir()

    def tearDown(self):
        shutil.rmtree(str(self.temp_dir))

    def touch(self, *filenames):
        for name in filenames:
            (self.wh / name).write_bytes(b"not-a-real-wheel")

    def check(self, glibc, py_ver, arch="aarch64", required=None, wheelhouse=None):
        """直接呼叫 check()，才能對任意 (python, glibc, arch) 組合斷言。

        走 subprocess 只能測到「跑測試的那個直譯器」，而這支守門的重點正是「別的平台」。
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "wheel_compat", str(DEPLOY_DIR / "lib" / "wheel_compat.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = str(self.wh if wheelhouse is None else wheelhouse)
        rc, problems, _found = module.check(
            target, glibc,
            self.RUNTIME if required is None else required,
            py_ver, arch,
        )
        return rc, "\n".join(problems)

    # --- 正向：兩個 profile 各自的真實 wheelhouse 都要過 ---------------------

    def test_real_jammy_wheelhouse_matches_cp310(self):
        wh = DEPLOY_DIR / "platforms" / "ubuntu-22.04-arm64" / "wheelhouse"
        if not any(wh.glob("*.whl")):
            self.skipTest("Jammy wheelhouse 未派送到本機（*.whl 不納入版控）")
        rc, out = self.check("2.35", (3, 10), wheelhouse=wh)
        self.assertEqual(rc, 0, out)

    def test_real_bionic_wheelhouse_matches_cp36(self):
        wh = DEPLOY_DIR / "platforms" / "ubuntu-18.04-arm64" / "wheelhouse"
        if not any(wh.glob("*.whl")):
            self.skipTest("Bionic wheelhouse 未派送到本機（*.whl 不納入版控）")
        rc, out = self.check("2.27", (3, 6), wheelhouse=wh)
        self.assertEqual(rc, 0, out)

    def test_pure_python_wheels_are_always_compatible(self):
        self.touch(
            "paramiko-3.5.1-py3-none-any.whl",
            "pycparser-2.21-py2.py3-none-any.whl",
        )
        rc, out = self.check("2.27", (3, 6), required=["paramiko", "pycparser"])
        self.assertEqual(rc, 0, out)

    # --- 反向：這些都是實際踩過或差一步就會踩到的 ---------------------------

    def test_cp310_wheel_rejected_on_py36(self):
        self.touch("cffi-2.1.0-cp310-cp310-manylinux2014_aarch64.whl")
        rc, out = self.check("2.27", (3, 6), required=[])
        self.assertEqual(rc, 6, out)
        self.assertIn("只適用 Python 3.10", out)

    def test_newer_glibc_wheel_rejected_on_bionic(self):
        # 這正是 Jammy 那三個輪子（bcrypt / cryptography / pynacl）在 Bionic 上的下場。
        self.touch("cryptography-49.0.0-cp39-abi3-manylinux_2_34_aarch64.whl")
        rc, out = self.check("2.27", (3, 6), required=[])
        self.assertEqual(rc, 6, out)
        self.assertIn("glibc >= 2.34", out)

    def test_abi3_wheel_rejected_when_interpreter_too_old(self):
        self.touch("bcrypt-5.0.0-cp39-abi3-manylinux_2_17_aarch64.whl")
        rc, out = self.check("2.27", (3, 6), required=[])
        self.assertEqual(rc, 6, out)
        self.assertIn("abi3", out)

    def test_abi3_wheel_accepted_when_interpreter_newer(self):
        # abi3 的語意是「>= 這個版本」，不能當成必須相等，否則 Jammy 會誤擋自己的輪子。
        self.touch("bcrypt-5.0.0-cp39-abi3-manylinux_2_17_aarch64.whl")
        rc, out = self.check("2.35", (3, 10), required=["bcrypt"])
        self.assertEqual(rc, 0, out)

    def test_multiple_platform_tags_take_the_loosest(self):
        # 同一個 wheel 宣告多個平台標籤時，只要**任一個**滿足就裝得起來。
        self.touch(
            "coverage-7.15.2-cp36-cp36m-manylinux2014_aarch64."
            "manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl"
        )
        rc, out = self.check("2.27", (3, 6), required=["coverage"])
        self.assertEqual(rc, 0, out)

    def test_wrong_architecture_rejected(self):
        self.touch("cffi-1.15.1-cp36-cp36m-manylinux2014_x86_64.whl")
        rc, out = self.check("2.27", (3, 6), required=[], arch="aarch64")
        self.assertEqual(rc, 6, out)
        self.assertIn("架構是", out)

    def test_debian_arch_naming_is_not_used_for_wheel_tags(self):
        # dpkg 說 arm64、wheel 標籤說 aarch64。拿前者來比會把每個輪子都誤判成不相容，
        # 這個 bug 在開發時真的發生過（Jammy 自己的 17 個輪子全被擋）。
        self.touch("cffi-1.15.1-cp36-cp36m-manylinux2014_aarch64.whl")
        rc, out = self.check("2.27", (3, 6), required=["cffi"], arch="arm64")
        self.assertEqual(rc, 6, out)
        source = (DEPLOY_DIR / "lib" / "wheel_compat.py").read_text(encoding="utf-8")
        self.assertIn("platform.machine()", source)
        self.assertNotIn("print-architecture", source)

    def test_empty_wheelhouse_is_rejected(self):
        rc, out = self.check("2.27", (3, 6))
        self.assertEqual(rc, 4, out)
        self.assertIn("沒有任何 .whl", out)

    def test_missing_wheelhouse_is_rejected(self):
        rc, out = self.check("2.27", (3, 6), wheelhouse=self.temp_dir / "nope")
        self.assertEqual(rc, 4, out)

    def test_missing_required_runtime_package_is_rejected(self):
        # 輪子全都相容，但少了 paramiko —— pip 會失敗，而失敗點在階段 B。
        self.touch("bcrypt-4.0.1-cp36-abi3-manylinux_2_17_aarch64.whl")
        rc, out = self.check("2.27", (3, 6))
        self.assertEqual(rc, 4, out)
        self.assertIn("paramiko", out)

    def test_package_name_normalisation(self):
        # 檔名是 typing_extensions，需求寫 typing-extensions，必須視為同一個（PEP 503）。
        self.touch("typing_extensions-4.1.1-py3-none-any.whl")
        rc, out = self.check("2.27", (3, 6), required=["typing-extensions"])
        self.assertEqual(rc, 0, out)

    def test_guard_itself_runs_on_python36(self):
        """守門不能用它要守的那個平台跑不動的語法寫。

        只看**程式碼**，不看註解 —— 檔頭刻意把這些寫法列成「不要用」的清單，
        整檔搜字串會被自己的說明文件誤判。
        """
        raw = (DEPLOY_DIR / "lib" / "wheel_compat.py").read_text(encoding="utf-8")
        code = "\n".join(
            line for line in raw.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("from __future__ import annotations", code)
        self.assertNotIn("dataclass", code)
        for token in ("list[", "dict[", "tuple[", "set["):
            self.assertNotIn(token, code, "PEP 585 泛型在 3.6 求值會 TypeError")


class ShipInterpreterCompatTests(unittest.TestCase):
    """跑在船端直譯器上的程式必須維持 Python 3.6 相容。

    Bionic 的船端只有 python3.6.9（沒有任何 3.7+），而下面這些檔案**不是**用專案 venv
    跑就是用系統 python3 跑，兩者在 Bionic 上都是 3.6：

      * deploy/automation_health_check.py — 系統 python3（README 明示「使用系統 python3
        即可」），所以連 backport 都補不了，只能用標準庫。
      * deploy/health_check.py — venv 的 python，而 Bionic 的 venv 是 3.6。
      * deploy/lib/wheel_compat.py — preflight 守門，用船端直譯器執行。

    這一項用靜態掃描而不是真的跑 3.6：CI 與開發機是 3.10，跑不出 3.6 的行為。掃描抓不到
    全部（例如某些只在特定分支才執行的新 API），但 `from __future__ import annotations`、
    `dataclasses`、PEP 585/604 這幾類是實際踩過的，值得釘死。
    """

    MUST_RUN_ON_36 = (
        "deploy/automation_health_check.py",
        "deploy/health_check.py",
        "deploy/lib/wheel_compat.py",
    )

    # (樣式, 說明, 需要的版本)
    FORBIDDEN = (
        ("from __future__ import annotations", "3.6 沒有這個 future，SyntaxError", "3.7"),
        ("from dataclasses import", "dataclasses 是 3.7 才進標準庫", "3.7"),
        ("import dataclasses", "dataclasses 是 3.7 才進標準庫", "3.7"),
        ("capture_output=", "subprocess 的 capture_output= 是 3.7", "3.7"),
        ("text=True", "subprocess 的 text= 是 3.7（用 universal_newlines=）", "3.7"),
        (".fromisoformat(", "datetime.fromisoformat 是 3.7", "3.7"),
        ("shlex.join(", "shlex.join 是 3.8", "3.8"),
        ("cached_property", "functools.cached_property 是 3.8", "3.8"),
        (".removeprefix(", "str.removeprefix 是 3.9", "3.9"),
        (".removesuffix(", "str.removesuffix 是 3.9", "3.9"),
    )

    PEP585_BUILTINS = {"list", "dict", "tuple", "set", "frozenset", "type"}

    def _code_lines(self, path):
        """去掉整行註解 —— 檔頭常把這些寫法列成「不要用」的清單。"""
        raw = (PROJECT_DIR / path).read_text(encoding="utf-8")
        return "\n".join(
            line for line in raw.splitlines() if not line.lstrip().startswith("#")
        )

    def _annotations(self, path):
        """走訪所有註解節點。

        用 AST 而不是字串比對，有兩個具體理由（開發這一批時兩者都踩到了）：

        1. `Tuple[str | None, str]` 這種**嵌在下標裡**的 PEP 604 union，用 regex 比對
           「註解開頭」抓不到，`compile()` 也會過（語法合法），只有在 3.6 上求值時才
           `TypeError: unsupported operand type(s) for |`。
        2. 反過來，用 `"list["` 當子字串會誤判 —— `mylist[0]` 也含這串。
        """
        import ast

        tree = ast.parse((PROJECT_DIR / path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                every = list(args.args) + list(args.kwonlyargs)
                every += list(getattr(args, "posonlyargs", []))
                for arg in every:
                    if arg.annotation is not None:
                        yield ast, arg.annotation, "參數 {}".format(arg.arg)
                for extra in (args.vararg, args.kwarg):
                    if extra is not None and extra.annotation is not None:
                        yield ast, extra.annotation, extra.arg
                if node.returns is not None:
                    yield ast, node.returns, "回傳值"
            elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
                yield ast, node.annotation, "變數註解"

    def test_no_post36_constructs(self):
        for path in self.MUST_RUN_ON_36:
            code = self._code_lines(path)
            for token, why, ver in self.FORBIDDEN:
                self.assertNotIn(
                    token, code,
                    "{}：{}（需要 {}，Bionic 船端只有 3.6）".format(path, why, ver),
                )

    def test_no_pep585_builtin_generics_in_annotations(self):
        for path in self.MUST_RUN_ON_36:
            for ast, ann, where in self._annotations(path):
                for sub in ast.walk(ann):
                    if not isinstance(sub, ast.Subscript):
                        continue
                    base = sub.value
                    if isinstance(base, ast.Name) and base.id in self.PEP585_BUILTINS:
                        self.fail(
                            "{}:{} [{}] 用了 PEP 585 的 {}[...]；3.6 求值會 "
                            "TypeError，改用 typing.{}".format(
                                path, sub.lineno, where, base.id,
                                base.id.capitalize()),
                        )

    def test_no_pep604_unions_in_annotations(self):
        for path in self.MUST_RUN_ON_36:
            for ast, ann, where in self._annotations(path):
                for sub in ast.walk(ann):
                    if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                        self.fail(
                            "{}:{} [{}] 用了 PEP 604 union（3.10+）；改用 "
                            "typing.Optional / Union".format(path, sub.lineno, where),
                        )

    def test_files_actually_parse(self):
        """順手確認掃描對象真的還在、而且語法沒壞。"""
        for path in self.MUST_RUN_ON_36:
            full = PROJECT_DIR / path
            self.assertTrue(full.is_file(), "{} 不見了".format(path))
            compile(full.read_text(encoding="utf-8"), str(full), "exec")


class WheelhouseLayoutTests(unittest.TestCase):
    """per-profile wheelhouse 的佈局契約。"""

    def test_deploy_resolves_wheelhouse_from_profile(self):
        source = (DEPLOY_DIR / "deploy_offline.sh").read_text(encoding="utf-8")
        self.assertIn("resolve_wheelhouse", source)
        self.assertIn('WHEELHOUSE="$prof_wh"', source)
        # manifest 要跟著 wheelhouse 走，不能再指回共用的 deploy/MANIFEST.txt。
        self.assertIn('MANIFEST="${prof_wh}/MANIFEST.txt"', source)

    def test_guard_runs_before_any_mutation(self):
        """相容性檢查必須在階段 A 之前 —— 這是整個守門的意義所在。"""
        source = (DEPLOY_DIR / "deploy_offline.sh").read_text(encoding="utf-8")
        guard = source.index("wheel_compat.py")
        preflight_end = source.index("stage_vessel_info")
        self.assertLess(
            guard, preflight_end,
            "wheel_compat 的呼叫跑到階段 A 之後了；那樣擋不住「部署到一半失去 OTA」"
        )

    def test_every_shipped_profile_wheelhouse_has_a_manifest(self):
        platforms = DEPLOY_DIR / "platforms"
        for profile in sorted(p for p in platforms.iterdir() if p.is_dir()):
            wh = profile / "wheelhouse"
            if not wh.is_dir():
                continue
            if not any(wh.glob("*.whl")):
                continue
            manifest = wh / "MANIFEST.txt"
            self.assertTrue(
                manifest.is_file(),
                "{} 有輪子卻沒有 MANIFEST.txt".format(wh),
            )
            listed = set()
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                listed.add(line.split(None, 1)[1].strip())
            actual = {p.name for p in wh.glob("*.whl")}
            self.assertEqual(
                listed, actual,
                "{} 的 MANIFEST.txt 與實際檔案不一致".format(wh),
            )

    def test_test_stack_is_filtered_by_availability(self):
        """測試堆疊要按 wheelhouse 實際有什麼裝什麼。

        Bionic 的 py3.6 沒有任何真的 exceptiongroup（PyPI 上只有 0.0.0a0 佔位套件），
        不該為此讓整個部署失敗 —— 測試堆疊不是船上跑服務的必要條件。
        """
        source = (DEPLOY_DIR / "deploy_offline.sh").read_text(encoding="utf-8")
        self.assertIn("skipped+=", source)
        self.assertIn("RUNTIME_PKGS", source)


if __name__ == "__main__":
    unittest.main()
