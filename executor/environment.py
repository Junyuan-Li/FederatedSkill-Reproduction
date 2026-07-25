"""
environment.py — WorkspaceManager：真实 agent workspace 环境管理

[ENGINEERING] 本模块标签：工程实现细节，用于实现论文 Agent Harness 架构中
抽象描述的 Environment 层，本身不对应任何论文公式。

对应论文 Agent Harness 架构中的 Environment 层：

    Model -> Agent Framework -> Skill Retrieval -> Tool Calling -> Environment -> Test

职责：

  1. 创建隔离的临时工作区目录
  2. 把任务输入文件（Task.files，对应真实 SkillFlow 任务的 environment/ 目录）
     写入工作区
  3. 通过"执行前 / 执行后"文件路径快照 diff，追踪 agent 在工作区内新生成的文件
     （generated_files，供 Trajectory 使用）
  4. 清理工作区（无论成功/失败，支持上下文管理器用法）

用法::

    with WorkspaceManager(prefix="task_") as ws:
        ws.write_input_files(task.files)
        ws.snapshot()                       # 记录初始文件集合
        ws.write_file("solution.py", code)  # agent 的一次 write_file 动作
        new_files = ws.diff_generated_files()
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# 官方 SkillFlow 任务的 environment/Dockerfile 里 `COPY <src> <dst>` 指令
# （构建期把 environment/ 目录下的输入文件铺进容器的 /root）。不同任务的
# COPY 写法并不统一（有的直接 `COPY data/ /root/` 把 data/ 子目录内容拍平，
# 有的 `COPY x.xlsx /root/data/x.xlsx` 反而是主动加前缀，还有的
# `COPY data /root/environment/data` 加了更深的前缀），如果只是把整个
# environment/ 目录原样镜像进工作区（旧实现），会导致 agent 真实看到的路径
# 与 Dockerfile 声明的运行时路径不一致，进而在 `/root/xxx` 找不到文件、
# 转而去翻 `data/xxx`——这正是本次真实实验发现 reward=0 的根因之一。
_DOCKERFILE_COPY_PATTERN = re.compile(
    r"^\s*COPY\s+(?!--from)(\S+)\s+(\S+)\s*$", re.MULTILINE
)


def _parse_dockerfile_copy_directives(dockerfile: Path) -> list[tuple[str, str]]:
    """解析 Dockerfile 里的 `COPY <src> <dst>` 指令（忽略多阶段构建的 --from）。"""
    text = dockerfile.read_text(encoding="utf-8")
    return [
        (src, dst)
        for src, dst in _DOCKERFILE_COPY_PATTERN.findall(text)
    ]


@dataclass(frozen=True)
class TrialIsolationSpec:
    """一次 task trial 的隔离与归档配置。"""

    artifact_dir: Path
    task_id: str
    worker_id: str
    round_idx: int
    attempt: int
    instruction: str
    runtime_metadata: dict[str, object]


_TRIAL_ISOLATION: contextvars.ContextVar[TrialIsolationSpec | None] = (
    contextvars.ContextVar("trial_isolation", default=None)
)
_SUBST_LOCK = threading.Lock()
_SUBST_DRIVES: set[str] = set()


@contextmanager
def isolated_task_trial(spec: TrialIsolationSpec) -> Iterator[None]:
    """让当前调用链中新建的 WorkspaceManager 使用 task 级隔离。"""
    token = _TRIAL_ISOLATION.set(spec)
    try:
        yield
    finally:
        _TRIAL_ISOLATION.reset(token)


def _allocate_subst_drive(target: Path) -> str:
    """把独立 sandbox 映射为虚拟盘，使 `/root` 解析到 trial 内。"""
    with _SUBST_LOCK:
        for letter in reversed("DEFGHIJKLMNOPQRSTUVWXYZ"):
            drive = f"{letter}:"
            if drive in _SUBST_DRIVES or Path(f"{drive}\\").exists():
                continue
            result = subprocess.run(
                ["subst", drive, str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                _SUBST_DRIVES.add(drive)
                return drive
        raise RuntimeError("没有可用盘符，无法为 SkillFlow trial 建立 /root 隔离映射")


def _release_subst_drive(drive: str) -> None:
    with _SUBST_LOCK:
        subprocess.run(
            ["subst", drive, "/D"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        _SUBST_DRIVES.discard(drive)


class WorkspaceManager:
    """
    管理单个任务的隔离工作区（临时目录），支持文件写入、生成文件追踪、自动清理。
    """

    def __init__(self, prefix: str = "workspace_", root: Path | str | None = None) -> None:
        self._isolation = _TRIAL_ISOLATION.get()
        self._sandbox: Path | None = None
        self._subst_drive: str | None = None
        if self._isolation is None:
            self._workspace = Path(
                tempfile.mkdtemp(prefix=prefix, dir=str(root) if root else None)
            )
        else:
            artifact_dir = self._isolation.artifact_dir
            artifact_dir.mkdir(parents=True, exist_ok=True)
            self._sandbox = artifact_dir / "workspace"
            if self._sandbox.exists():
                shutil.rmtree(self._sandbox)
            self._sandbox.mkdir(parents=True)
            if os.name != "nt":
                raise RuntimeError(
                    "无 Docker 模式下仅实现了 Windows subst /root 隔离；"
                    "拒绝在非 Windows 主机静默使用共享 /root"
                )
            self._subst_drive = _allocate_subst_drive(self._sandbox)
            self._workspace = Path(f"{self._subst_drive}\\root")
            self._workspace.mkdir(parents=True, exist_ok=True)
            (self._workspace / "task_instruction.md").write_text(
                self._isolation.instruction, encoding="utf-8"
            )
        self._before: set[str] = set()

    @property
    def path(self) -> Path:
        return self._workspace

    def write_input_files(self, files: dict[str, str]) -> None:
        """把 task.files（相对路径 -> 内容）写入工作区，对应真实任务的 environment/ 输入。"""
        for rel_path, content in files.items():
            self.write_file(rel_path, content)

    def copy_input_tree(self, source_dir: Path | str) -> None:
        """把官方 environment/ 的输入文件复制到隔离工作区。

        Workspace Materialization Fix（真实实验发现，2026-07-23）：优先按
        `environment/Dockerfile` 里真实的 `COPY <src> <dst>` 指令决定目标
        路径（`dst` 必须落在 `/root` 之下——本沙箱只把 `/root` 映射到工作区，
        `/app` 等其它前缀直接 fail-loud，不静默错放）；`Dockerfile` 本身
        **不会**被复制进工作区（它只是构建期文件，不是运行期文件）。仅当
        environment/ 下不存在 Dockerfile 时，才回退到旧行为（原样镜像整个
        目录树，同样排除 Dockerfile）。
        """
        source = Path(source_dir)
        if not source.is_dir():
            raise FileNotFoundError(f"任务 environment 源目录不存在: {source}")
        dockerfile = source / "Dockerfile"
        directives = (
            _parse_dockerfile_copy_directives(dockerfile) if dockerfile.is_file() else []
        )
        if directives:
            self._copy_via_dockerfile_directives(source, directives)
            return
        for source_path in source.rglob("*"):
            if not source_path.is_file() or source_path.name == "Dockerfile":
                continue
            destination = self._workspace / source_path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)

    def _copy_via_dockerfile_directives(
        self, source: Path, directives: list[tuple[str, str]]
    ) -> None:
        for src_pattern, dst in directives:
            if not dst.startswith("/root"):
                raise ValueError(
                    f"不支持的 Dockerfile COPY 目标前缀（仅支持 /root/*）: "
                    f"COPY {src_pattern} {dst}（environment 源目录: {source}）"
                )
            dst_is_dir_hint = dst.endswith("/")
            rel_dst = dst[len("/root") :].strip("/")
            matches = (
                sorted(source.glob(src_pattern))
                if any(ch in src_pattern for ch in "*?[")
                else [source / src_pattern]
            )
            for src_path in matches:
                if not src_path.exists():
                    raise FileNotFoundError(
                        f"Dockerfile 声明的 COPY 源不存在: {src_path}"
                    )
                if src_path.is_dir():
                    for file_path in src_path.rglob("*"):
                        if not file_path.is_file():
                            continue
                        destination = (
                            self._workspace / rel_dst / file_path.relative_to(src_path)
                        )
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file_path, destination)
                else:
                    destination = (
                        self._workspace / rel_dst / src_path.name
                        if dst_is_dir_hint or len(matches) > 1
                        else self._workspace / rel_dst
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, destination)

    def write_file(self, rel_path: str, content: str) -> Path:
        """在工作区内写入/覆盖单个文件（agent 的一次 write_file 工具调用）。"""
        dest = self._workspace / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return dest

    def read_file(self, rel_path: str) -> str:
        """读取工作区内文件内容（供 verifier 检查产出物）。"""
        return (self._workspace / rel_path).read_text(encoding="utf-8")

    def read_generated_file(self, rel_path: str, max_bytes: int = 1_000_000) -> str:
        """读取轨迹所需的生成文件文本；二进制或大文件只返回审计标记。"""
        path = self._workspace / rel_path
        size = path.stat().st_size
        if size > max_bytes:
            return f"<non-inline file: {size} bytes>"
        data = path.read_bytes()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary file: {size} bytes>"

    def exists(self, rel_path: str) -> bool:
        return (self._workspace / rel_path).exists()

    def snapshot(self) -> None:
        """记录当前工作区内所有文件的相对路径集合，作为"生成文件"判定的基线。"""
        self._before = self._list_files()

    def diff_generated_files(self) -> list[str]:
        """返回自 snapshot() 之后新出现的文件相对路径列表（供 Trajectory.generated_files 使用）。"""
        after = self._list_files()
        return sorted(after - self._before)

    def _list_files(self) -> set[str]:
        if not self._workspace.exists():
            return set()
        return {
            str(p.relative_to(self._workspace)).replace("\\", "/")
            for p in self._workspace.rglob("*")
            if p.is_file()
        }

    def cleanup(self) -> None:
        """删除整个工作区目录（幂等，重复调用安全）。"""
        if self._isolation is not None and self._workspace.exists():
            self._archive_isolated_trial()
        if self._subst_drive is not None:
            _release_subst_drive(self._subst_drive)
            self._subst_drive = None
        target = self._sandbox if self._sandbox is not None else self._workspace
        shutil.rmtree(target, ignore_errors=True)

    def _archive_isolated_trial(self) -> None:
        """在删除执行目录前保存 verifier 所见状态和 agent 生成文件。"""
        assert self._isolation is not None
        artifact_dir = self._isolation.artifact_dir
        snapshot_dir = artifact_dir / "workspace_snapshot"
        generated_dir = artifact_dir / "generated_files"
        for destination in (snapshot_dir, generated_dir):
            if destination.exists():
                shutil.rmtree(destination)

        shutil.copytree(self._workspace, snapshot_dir)
        after = self._list_files()
        generated = sorted(
            path for path in after - self._before
            if path != "_verify_test_script.py"
        )
        generated_dir.mkdir(parents=True, exist_ok=True)
        for rel_path in generated:
            source = self._workspace / rel_path
            destination = generated_dir / rel_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        files = []
        for rel_path in sorted(after):
            path = self._workspace / rel_path
            files.append({
                "path": rel_path,
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source": (
                    "verifier_internal" if rel_path == "_verify_test_script.py"
                    else "instruction" if rel_path == "task_instruction.md"
                    else "generated" if rel_path in generated
                    else "environment"
                ),
            })
        manifest = {
            "task_id": self._isolation.task_id,
            "worker_id": self._isolation.worker_id,
            "round_idx": self._isolation.round_idx,
            "attempt": self._isolation.attempt,
            "instruction_file": "task_instruction.md",
            "workspace_was_fresh": True,
            "temporary_workspace_deleted_after_collection": True,
            "runtime_metadata": self._isolation.runtime_metadata,
            "generated_files": generated,
            "files": files,
        }
        (artifact_dir / "task_instruction.md").write_text(
            self._isolation.instruction, encoding="utf-8"
        )
        (artifact_dir / "workspace_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def __enter__(self) -> "WorkspaceManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()
