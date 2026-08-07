# 二期:烘焙贴图(bake)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Blender 面板里选中对象一键提交云端烘焙:对象 × pass 并行(L40S+OPTIX),产物 textures.zip 自动取回。支持程序化材质烘 PBR 贴图集与高模→低模(`_low`/`_high` 命名配对)烘焙。

**Architecture:** 复用 MVP 全部基建(上传/分块/进度/取消/下载)。协议走既有 `task_type` 分支;云端把滑动窗口调度从 render_job 抽成共享 `_sliding_schedule`,新增 `bake_unit`(GPU,单对象×单 pass)与 `bake_job`(coordinator);addon 面板加 Render/Bake 模式切换。**前置冒烟已通过**(2026-08-08:无头 bake 在 L40S+OPTIX 上 DIFFUSE 2.7s/NORMAL 0.5s/AO 0.5s;坑:多材质槽对象每个槽都要挂目标 image node,否则该槽的面被静默跳过)。

**Tech Stack:** 既有栈;`bpy.ops.object.bake`(Cycles bake)。

## Global Constraints

- 沿用 MVP 全部约束(bpy 5.2/py3.13、farm_key、产物走 Volume+fetch、部署经 farm_deploy.py)
- bake 单元 = (object, pass);单 job ≤ **256 单元**(费用护栏)
- pass 白名单:`NORMAL / AO / DIFFUSE / ROUGHNESS / EMIT / COMBINED`(常用 PBR 集;DIFFUSE 自动关 direct/indirect = albedo)
- **场景是真源**:UV 必须已展好(无 UV 报错);目标 image node 由 worker 统一创建(`farm_bake_<pass>`,每个材质槽都挂——冒烟坑),分辨率参数控制(默认 2048)
- 高低模配对:`<name>_low` ↔ `<name>_high` 命名约定;`selected_to_active=true` 时低模找不到配对高模 → 明确报错
- 产物:`_outputs/<job_id>/textures/<obj>_<pass>.<png|exr>` → 打包 `textures.zip`
- 每 task 一 commit;云端改动重新 `python3 farm_deploy.py`;addon 改动热重载前查活跃任务

---

### Task 1: farm_common — bake 协议校验 + 单测

**Files:** Modify `modal_app/farm_common.py`、`tests/test_farm_common.py`

**Interfaces(Produces):**
- `normalize_job` 支持 `task_type="bake"` + `payload["bake"]`,返回扁平 job:`{task_type:"bake", blend_path, passes:[…], objects:[…], selected_to_active, cage_extrusion, max_ray_distance, margin, resolution, file_format}` + 可选 `samples`
- `BAKE_PASSES = ("NORMAL","AO","DIFFUSE","ROUGHNESS","EMIT","COMBINED")`、`MAX_BAKE_UNITS = 256`
- `bake_units(job) -> list[tuple[str, str]]`((object, pass) 展开,顺序稳定)
- `high_name(low: str) -> str | None`(`Cube_low`→`Cube_high`;无 `_low` 后缀返回 None)

**Steps:**
- [ ] 单测(先红):bake 默认值/objects 非空校验/pass 白名单/units 上限(objects×passes>256 拒)/cage 数值校验/`high_name("X_low")=="X_high"`、`high_name("X") is None`/render 路径回归不破
- [ ] 实现:normalize_job 里 task_type 分支(render 逻辑不动;bake 分支校验 objects(非空 str 列表,≤128 个)/passes(白名单去重,默认 ["NORMAL","AO"])/selected_to_active(bool)/cage_extrusion、max_ray_distance(float ≥0)/margin(int 1..64,默认 16)/resolution(int 64..8192,默认 2048)/file_format(PNG|OPEN_EXR)/samples 可选)
- [ ] `python3 -m pytest tests/ -q` 全绿 → commit

### Task 2: farm_app — 调度抽取 + bake_unit + bake_job + run 分流

**Files:** Modify `modal_app/farm_app.py`

**Interfaces(Produces):**
- `_sliding_schedule(spawn_fn, units, job_id, t0) -> (results, warnings, device)`:从 render_job 抽出的滑动窗口(spawn/进度/:subcalls/FIFO get);render_job 改为调用它
- `bake_unit(job, obj_name, pass_type, job_id) -> dict`(GPU 函数):场景缓存 key=job_id;context= select(+高模)+active 低模;**每个材质槽**确保 `farm_bake_<pass>` image node 并设 active(分辨率 job["resolution"]);无 UV → 报错列出对象名;DIFFUSE 关 direct/indirect;bake 后 image.save_render 到 /tmp → copy Volume `_outputs/<job_id>/textures/<obj>_<pass>.<ext>` → commit;返回 {"unit","path","size","secs","warnings","device"}
- `bake_job(job, job_id)`(coordinator):units=farm_common.bake_units → _sliding_schedule(bake_unit.spawn 包装) → 全完成后打 `textures.zip`(帧目录规则同 render:/tmp 打包再拷、散图删)→ 终态 outputs
- `run_endpoint`:按 `job["task_type"]` spawn `render_job` / `bake_job`

**Steps:**
- [ ] 抽 `_sliding_schedule`(render_job 行为不变——现有 demo 冒烟兜回归)
- [ ] 实现 bake_unit / bake_job / run 分流;语法 + 单测全绿 → commit

### Task 3: 部署 + demo bake 冒烟

**Files:** Modify `smoke_test.py`(`--task bake`:demo 场景 blend_path=None,objects=["Cube"],passes NORMAL+AO)

**Steps:**
- [ ] `python3 farm_deploy.py` 部署
- [ ] `python3 smoke_test.py --task bake` → textures.zip 下载解包出 `Cube_NORMAL.png`+`Cube_AO.png`,非空图;render 回归:`--frames 1-2` 仍通 → commit

### Task 4: addon — Render/Bake 模式切换 + Bake 参数区 + 提交分支

**Files:** Modify `addon/blender_modal_bridge/ui.py`、`ops.py`、`client.py`

**Interfaces:**
- Scene 属性:`farm_task`(RENDER|BAKE)、`farm_bake_normal/ao/diffuse/roughness/emit/combined`(Bool)、`farm_bake_resolution`(2048)、`farm_bake_margin`(16)、`farm_bake_s2a`(Bool)、`farm_bake_cage`(0.05)、`farm_bake_format`(PNG|OPEN_EXR)
- BAKE 模式提交:对象 = **当前选中的 MESH 对象**(无选中报错);`client.run(task_payload: dict, blend_path)` 泛化(body 直接并入 task_type/render/bake 键)
- 面板 BAKE 区:选中对象计数提示、pass 勾选行、分辨率/margin、s2a+cage;job 卡片 label 显示 `bake N obj × M pass`

**Steps:**
- [ ] ui/ops/client 实现;语法检查 → 热重载(查活跃任务)→ commit

### Task 5: 真机验收 + 收尾

- [ ] Mac Blender:选中一个有 UV 的 mesh(或 demo cube 场景)→ Bake 模式勾 NORMAL+AO → 提交 → textures.zip 自动下载解包验图
- [ ] (可选,用户有高低模资产时)`_low/_high` s2a 烘 normal 验收
- [ ] README bake 用法一节;memory 更新(bake 上线+实测数据);commit + push

## Self-Review

- 冒烟坑(多材质槽)在 bake_unit 显式处理 ✓;UV 缺失显式报错 ✓;s2a 配对失败显式报错 ✓
- render 回归由 Task 3 的 `--frames 1-2` 冒烟兜底 ✓;调度抽取不改行为 ✓
- 风险:image.save_render 的色彩管理(NORMAL/ROUGHNESS 应存 Non-Color/raw)——bake_unit 里对非颜色 pass 设 `image.colorspace_settings.name="Non-Color"` 再 save,Task 3 冒烟看图验证
