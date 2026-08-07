"""farm_common 纯函数单测。跑法(仓库根): python3 -m pytest tests/ -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "modal_app"))
import farm_common as fc  # noqa: E402


def test_normalize_defaults():
    """最小 payload(demo 单帧)→ 全默认值。"""
    job, err = fc.normalize_job({})
    assert err is None
    assert job["task_type"] == "render" and job["blend_path"] is None
    assert job["frame_start"] == 1 == job["frame_end"] and job["frame_step"] == 1
    assert job["output"] == "video" and job["file_format"] == "PNG" and job["fps"] == 24


def test_normalize_task_type_gate():
    """MVP 只认 render;bake 等二期类型给明确报错而非静默当 render。"""
    _, err = fc.normalize_job({"task_type": "bake"})
    assert err and "bake" in err
    job, err = fc.normalize_job({"task_type": "render"})
    assert err is None


def test_normalize_blend_path_jail():
    """blend_path 只认 Volume 上 scenes/ 下的规范相对路径(upload 端点生成的形态)。"""
    for bad in ("models/x.blend", "scenes/../secrets", "/scenes/x.blend", 123):
        _, err = fc.normalize_job({"blend_path": bad})
        assert err, f"应拒绝 {bad!r}"
    job, err = fc.normalize_job({"blend_path": "scenes/ab12cd34_scene.blend"})
    assert err is None and job["blend_path"] == "scenes/ab12cd34_scene.blend"


def test_normalize_frames_and_limits():
    job, err = fc.normalize_job({"render": {"frame_start": 10, "frame_end": 20, "frame_step": 5}})
    assert err is None and fc.frames_list(job) == [10, 15, 20]
    _, err = fc.normalize_job({"render": {"frame_start": 5, "frame_end": 1}})
    assert err
    _, err = fc.normalize_job({"render": {"frame_start": 1, "frame_end": fc.MAX_FRAMES + 1}})
    assert err and str(fc.MAX_FRAMES) in err
    _, err = fc.normalize_job({"render": {"frame_start": 1, "frame_end": 100000}})
    assert err  # 帧号 > 99999(%05d 字典序会乱)


def test_normalize_video_rejects_exr():
    _, err = fc.normalize_job({"render": {"output": "video", "file_format": "OPEN_EXR"}})
    assert err
    job, err = fc.normalize_job({"render": {"output": "frames", "file_format": "OPEN_EXR"}})
    assert err is None and job["file_format"] == "OPEN_EXR"


def test_normalize_overrides():
    job, err = fc.normalize_job({"render": {"samples": 64, "resolution_x": 1920,
                                            "resolution_y": 1080, "camera": " Cam.001 "}})
    assert err is None
    assert job["samples"] == 64 and job["resolution_x"] == 1920 and job["camera"] == "Cam.001"
    _, err = fc.normalize_job({"render": {"samples": 0}})
    assert err
    _, err = fc.normalize_job({"render": {"samples": "abc"}})
    assert err


def test_parse_frame_spec():
    assert fc.parse_frame_spec("1-250") == (1, 250, 1)
    assert fc.parse_frame_spec("1-250:2") == (1, 250, 2)
    assert fc.parse_frame_spec("7") == (7, 7, 1)
    for bad in ("", "a-b", "1-2:0x"):
        with pytest.raises(ValueError):
            fc.parse_frame_spec(bad)


def test_ffmpeg_cmd():
    cmd = fc.ffmpeg_cmd("/v/_outputs/j1/frames", "/v/_outputs/j1/render.mp4", 24)
    s = " ".join(cmd)
    assert "-framerate 24" in s and "yuv420p" in s and "pad=" in s  # 奇数分辨率兜底
    assert "/v/_outputs/j1/frames/*.png" in s
    assert "-crf 20" in s and "-g 18" in s  # 质量参数(对齐 Flamenco;x264 默认 crf23 偏低)


def test_expand_frame_spec():
    """复合帧范围(镜头返修补渲散帧):逗号分段,段=帧号|区间|区间:step;去重升序。"""
    assert fc.expand_frame_spec("3, 5-10, 47-50") == [3, 5, 6, 7, 8, 9, 10, 47, 48, 49, 50]
    assert fc.expand_frame_spec("1-10:3") == [1, 4, 7, 10]
    assert fc.expand_frame_spec("7") == [7]
    assert fc.expand_frame_spec("5, 3, 5-6") == [3, 5, 6]   # 去重 + 排序
    for bad in ("", "a", "1-2:0", "5-1", "1-3,x"):
        with pytest.raises(ValueError):
            fc.expand_frame_spec(bad)


def test_normalize_frames_spec():
    """render.frames(复合 spec 字符串)优先于 frame_start/end/step。"""
    job, err = fc.normalize_job({"render": {"frames": "1, 5-8"}})
    assert err is None and fc.frames_list(job) == [1, 5, 6, 7, 8]
    assert job["frames_spec"] == "1, 5-8"
    _, err = fc.normalize_job({"render": {"frames": "bad"}})
    assert err
    _, err = fc.normalize_job({"render": {"frames": f"1-{fc.MAX_FRAMES + 1}"}})
    assert err and str(fc.MAX_FRAMES) in err
    _, err = fc.normalize_job({"render": {"frames": "99998-100001"}})
    assert err  # 帧号上限仍生效


def test_bake_normalize_defaults():
    """bake 最小 payload:objects 必填,其余默认。"""
    job, err = fc.normalize_job({"task_type": "bake", "bake": {"objects": ["Cube"]}})
    assert err is None
    assert job["task_type"] == "bake" and job["objects"] == ["Cube"]
    assert job["passes"] == ["NORMAL", "AO"] and job["resolution"] == 2048
    assert job["margin"] == 16 and job["selected_to_active"] is False
    assert job["file_format"] == "PNG"
    _, err = fc.normalize_job({"task_type": "bake", "bake": {}})
    assert err  # objects 必填
    _, err = fc.normalize_job({"task_type": "bake", "bake": {"objects": []}})
    assert err


def test_bake_pass_whitelist_and_units():
    job, err = fc.normalize_job({"task_type": "bake", "bake": {
        "objects": ["A", "B"], "passes": ["NORMAL", "DIFFUSE", "NORMAL"]}})
    assert err is None and job["passes"] == ["NORMAL", "DIFFUSE"]   # 去重保序
    assert fc.bake_units(job) == [("A", "NORMAL"), ("A", "DIFFUSE"),
                                  ("B", "NORMAL"), ("B", "DIFFUSE")]
    _, err = fc.normalize_job({"task_type": "bake", "bake": {
        "objects": ["A"], "passes": ["GLOSSY_WRONG"]}})
    assert err
    many = [f"o{i}" for i in range(60)]   # 60×6=360 > 256
    _, err = fc.normalize_job({"task_type": "bake", "bake": {
        "objects": many, "passes": list(fc.BAKE_PASSES)}})
    assert err and str(fc.MAX_BAKE_UNITS) in err


def test_bake_param_validation():
    _, err = fc.normalize_job({"task_type": "bake", "bake": {
        "objects": ["A"], "resolution": 32}})
    assert err   # 分辨率 64..8192
    _, err = fc.normalize_job({"task_type": "bake", "bake": {
        "objects": ["A"], "margin": 0}})
    assert err
    job, err = fc.normalize_job({"task_type": "bake", "bake": {
        "objects": ["A"], "selected_to_active": True, "cage_extrusion": 0.05}})
    assert err is None and job["cage_extrusion"] == 0.05
    _, err = fc.normalize_job({"task_type": "bake", "bake": {
        "objects": ["A"], "cage_extrusion": -1}})
    assert err


def test_high_name():
    assert fc.high_name("Cube_low") == "Cube_high"
    assert fc.high_name("Cube") is None
    assert fc.high_name("x_low_low") == "x_low_high"   # 只换最后一个后缀


def test_safe_scene_name():
    assert fc.safe_scene_name("My Scene (v2).blend") == "My_Scene__v2_.blend"
    assert fc.safe_scene_name("../../../etc/passwd") == "passwd"
    assert fc.safe_scene_name("") == "scene.blend"
