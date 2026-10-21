"""script/force_redownload_batch.py 與回填批次檔 docs/force_redownload_b1.json 的測試。

批次檔的樣式是拿岸端實際清單一棵一棵挑出來的（見 docs/hash-baseline-backfill.md），
最容易出錯的地方是 `!` 排除：_force_matches 逐層檢查祖先資料夾，祖先一旦命中就整棵算進
去，所以 `"*"` 配 `"!rt_enc_ias/assets/"` 這種**第二層**的排除完全無效 —— 600 MB 的
venv 包會跟著小檔一起被強制重抓。這裡用真的 _force_matches 把每棵大樹釘住。
"""

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "script"))

import force_redownload_batch as B
from downloader import SFTPDownloader
from gitignore import GitIgnoreSpec

BATCH = ROOT / "docs" / "force_redownload_b1.json"


def matcher(patterns):
    ns = types.SimpleNamespace(_force_spec=GitIgnoreSpec.from_lines(patterns))
    return lambda rel: SFTPDownloader._force_matches(ns, rel)


@pytest.fixture(scope="module")
def b1():
    return B.load_batch(BATCH)[1]


@pytest.mark.parametrize("name, rel", [
    ("radar", "YOLOv7_MODEL/best.onnx"),
    ("radar", "OCR_MODEL/det_inference_opset11.onnx"),
    ("radar", "wheels/onnxruntime_gpu-1.23.0-cp310-cp310-linux_aarch64.whl"),
    ("radar", "virtualenv_wheels/virtualenv-20.17.1-py3-none-any.whl"),
    ("ecdis", "rt_enc_ias/rt-enc-ias-venv_packages/rt-enc-ias-venv_0.4.6.zip"),
    ("ecdis", "rt_enc_ias/assets/models/obj_segment/yolov8s-seg_best.onnx"),
    ("ecdis", "rt_enc_ias/assets/fonts/msjh.ttc"),
    ("shm", "wheels/numpy-1.26.3-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl"),
    ("sftp", "deploy/platforms/ubuntu-22.04-arm64/wheelhouse/cryptography-49.0.0-cp39-abi3-manylinux_2_34_aarch64.whl"),
    ("sftp", "deploy/virtualenv_wheels/virtualenv-20.17.1-py3-none-any.whl"),
])
def test_b1_never_forces_the_large_trees(b1, name, rel):
    assert not matcher(b1[name])(rel)


@pytest.mark.parametrize("name, rel", [
    ("radar", "headless_processing.py"),
    ("radar", "config/CVPI/CVPI-100/TYPE-A-RADAR-HW.json"),
    ("ecdis", "VERSION.json"),
    ("ecdis", "rt_enc_ias/install_env.sh"),
    ("ecdis", "rt_enc_ias/src/common/OCR_Tag_Extractor/CVPI.py"),
    ("ecdis", "rt_enc_ias/assets/fds_checkbox_template/checked_1.png"),
    ("shm", "STREAM_manager.py"),
    ("sftp", "downloader.py"),
    ("sftp", "deploy/deploy_offline.sh"),
    ("sftp", "deploy/lib/wheel_compat.py"),
    ("scheduler", "reboot_launcher.sh"),
    ("IPC1_share", "alarm_controller/music/alarm.wav"),
])
def test_b1_covers_the_small_files(b1, name, rel):
    assert matcher(b1[name])(rel)


def write_config(directory, name, **extra):
    data = {"host": "h", "password": "secret", "remote_path": "/r", "local_path": "../x"}
    data.update(extra)
    path = Path(directory) / (name + B.SUFFIX)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_batch(directory, configs, batch_id="b-test"):
    path = Path(directory) / "batch.json"
    path.write_text(json.dumps({"force_redownload_id": batch_id, "configs": configs}), encoding="utf-8")
    return str(path)


def test_apply_then_clear_round_trips_and_keeps_other_keys(tmp_path, capsys):
    original = write_config(tmp_path, "radar").read_text(encoding="utf-8")
    batch = write_batch(tmp_path, {"radar": ["*"]})

    assert B.main([batch, "--config-dir", str(tmp_path), "--apply"]) == 0
    applied = json.loads((tmp_path / "radar_download_settings.json").read_text(encoding="utf-8"))
    assert applied["force_redownload"] == ["*"]
    assert applied["force_redownload_id"] == "b-test"
    assert applied["password"] == "secret"

    assert B.main([batch, "--config-dir", str(tmp_path), "--clear", "--apply"]) == 0
    assert (tmp_path / "radar_download_settings.json").read_text(encoding="utf-8") == original
    # 差異會印到終端機，密碼那一行不能出現
    assert "secret" not in capsys.readouterr().out


def test_preview_does_not_write(tmp_path):
    path = write_config(tmp_path, "radar")
    before = path.read_text(encoding="utf-8")
    assert B.main([write_batch(tmp_path, {"radar": ["*"]}), "--config-dir", str(tmp_path)]) == 0
    assert path.read_text(encoding="utf-8") == before


def test_missing_config_aborts_the_whole_batch(tmp_path):
    path = write_config(tmp_path, "radar")
    before = path.read_text(encoding="utf-8")
    batch = write_batch(tmp_path, {"radar": ["*"], "ecdis": ["*"]})
    assert B.main([batch, "--config-dir", str(tmp_path), "--apply"]) == 2
    assert path.read_text(encoding="utf-8") == before


def test_batch_without_id_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        B.load_batch(write_batch(tmp_path, {"radar": ["*"]}, batch_id=""))
