"""
harbor_adapter.py — Harbor/Docker 容器验证执行器

对应官方 SkillFlow 执行链路：
    Agent -> SkillFlow environment -> Harbor 容器 -> test

本仓库默认用 subprocess + 临时工作区代替容器隔离
（见 executor/skillflow_executor.py::SkillFlowScriptVerifier），
但真实 SkillFlow benchmark 中部分任务的验证脚本假设运行在特定 Docker 镜像内
（预装 Excel/PDF/OCR 处理库等），无法用纯 subprocess 忠实复现。
本模块提供一个可选的 Docker 执行通道：VerificationSpec.type == "docker" 时使用，
由 benchmark/verifier.py::DockerScriptVerifier 委托调用。

"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from benchmark.verifier import VerificationResult

if TYPE_CHECKING:
    from benchmark.task import Task

logger = logging.getLogger(__name__)

DOCKER_BUILD_TIMEOUT_SECONDS = 300
DOCKER_RUN_TIMEOUT_FALLBACK_SECONDS = 120


class HarborExecutor:
    """
    真实 Docker 容器验证执行器（Harbor 的轻量替代——只用原生
    `docker build` / `docker run`，不依赖 Harbor 私有编排层）。
    """

    @staticmethod
    def is_docker_available() -> bool:
        """检查本机是否安装并可调用 docker CLI（不实际启动容器）。"""
        return shutil.which("docker") is not None

    def run_verification(self, task: "Task", workspace_dir: Path) -> VerificationResult:
        """
        在 workspace_dir 内构建镜像并运行验证脚本。

        约定：
          - workspace_dir 下必须已有 Dockerfile（由调用方 / task.files 写入）；
          - task.verification.docker_image 是构建后使用的本地镜像 tag；
          - task.verification.test_script 是容器内通过 `sh -c` 执行的验证命令
            （如 "pytest tests/ -q"）。
        """
        spec = task.verification
        t_start = time.monotonic()

        if not self.is_docker_available():
            return VerificationResult(
                reward=0.0, success=False,
                stderr=(
                    "本机未安装 docker（`docker` 不在 PATH 中），"
                    "无法执行 verification.type='docker' 的验证。"
                    "这是运行环境限制，不代表生成代码本身错误。"
                ),
                exception_info={"type": "DockerUnavailable"},
                runtime_seconds=time.monotonic() - t_start,
            )

        workspace_dir = Path(workspace_dir)
        dockerfile = workspace_dir / "Dockerfile"
        if not dockerfile.exists():
            return VerificationResult(
                reward=0.0, success=False,
                stderr=f"工作区 {workspace_dir} 下缺少 Dockerfile，无法构建镜像。",
                exception_info={"type": "DockerfileMissing"},
                runtime_seconds=time.monotonic() - t_start,
            )

        image_tag = spec.docker_image or f"skillflow-{task.task_id}:local"

        try:
            build_proc = subprocess.run(
                ["docker", "build", "-t", image_tag, str(workspace_dir)],
                capture_output=True, text=True, timeout=DOCKER_BUILD_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return VerificationResult(
                reward=0.0, success=False,
                stderr=f"docker build 超时（>{DOCKER_BUILD_TIMEOUT_SECONDS}s）",
                exception_info={"type": "DockerBuildTimeout"},
                runtime_seconds=time.monotonic() - t_start,
            )
        except Exception as exc:  # noqa: BLE001 — 需要把任意 docker CLI 异常转为明确结果
            return VerificationResult(
                reward=0.0, success=False,
                stderr=str(exc),
                exception_info={"type": type(exc).__name__, "message": str(exc)},
                runtime_seconds=time.monotonic() - t_start,
            )

        if build_proc.returncode != 0:
            return VerificationResult(
                reward=0.0, success=False,
                stdout=build_proc.stdout[:2000], stderr=build_proc.stderr[:2000],
                exception_info={"type": "DockerBuildFailed"},
                runtime_seconds=time.monotonic() - t_start,
            )

        run_timeout = spec.timeout_seconds or DOCKER_RUN_TIMEOUT_FALLBACK_SECONDS
        try:
            run_proc = subprocess.run(
                ["docker", "run", "--rm", image_tag, "sh", "-c", spec.test_script or ""],
                capture_output=True, text=True, timeout=run_timeout,
            )
        except subprocess.TimeoutExpired:
            self._cleanup_image(image_tag)
            return VerificationResult(
                reward=0.0, success=False,
                stderr=f"docker run 超时（>{run_timeout}s）",
                exception_info={"type": "DockerRunTimeout"},
                runtime_seconds=time.monotonic() - t_start,
            )
        except Exception as exc:  # noqa: BLE001
            self._cleanup_image(image_tag)
            return VerificationResult(
                reward=0.0, success=False,
                stderr=str(exc),
                exception_info={"type": type(exc).__name__, "message": str(exc)},
                runtime_seconds=time.monotonic() - t_start,
            )

        self._cleanup_image(image_tag)

        elapsed = time.monotonic() - t_start
        success = run_proc.returncode == 0
        return VerificationResult(
            reward=1.0 if success else 0.0,
            success=success,
            stdout=run_proc.stdout[:2000],
            stderr=run_proc.stderr[:2000],
            runtime_seconds=elapsed,
        )

    @staticmethod
    def _cleanup_image(image_tag: str) -> None:
        """尽力清理本地构建的镜像，失败不影响验证结果（避免磁盘堆积）。"""
        try:
            subprocess.run(
                ["docker", "rmi", "-f", image_tag],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:  # noqa: BLE001 — 清理失败不应影响验证结果
            logger.warning("清理 docker 镜像 %s 失败（不影响验证结果）", image_tag)
