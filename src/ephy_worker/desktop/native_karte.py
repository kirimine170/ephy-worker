"""Fixed official Karte process, isolated from authoritative worklog storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DesktopConfig, application_data
from .inspection import InspectionError, _json
from .renderer import KarteRenderer

NATIVE_COMMIT = "68e245204fe2b2af5c21d271d5b8213691b8cbd7"
NATIVE_OFFER_TTL_SECONDS = 30
# Populated from the frozen official release inventory, not from user settings.
OFFICIAL_FILES = {'karte-mcp.exe': (5054976, '694afd42f4ce2c6872ddd6761379b42bea5e6cbb5254113108b586373d50d086'),
 'karte.exe': (25823744, '8cfda3d6cdac43a3ef962dfea7858baf308490640fafae327a743d32dd671a38'),
 'karte_data_template/content/README.md': (120,
                                           'f48e80b9f06e8ed6e0be994637c3e59f4ce5c502a8a6d948305a8fc5c6182b52'),
 'karte_data_template/data/asr/config.json': (767,
                                              'a59ec91dd815303f7b039a42e5d0c83c5fba0cd46cc5ccd35ee9e5eb6aa34ca3'),
 'karte_data_template/data/asr/sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10/LICENSE': (13788,
                                                                                                             '3f51ae494d838ad7a57aae7cb8a75b848d90b87d0ef43602589f7e38ecd655ac'),
 'karte_data_template/data/asr/sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10/README.md': (350,
                                                                                                               '79099e4a69018ecef2b7dbe4fb8d48c9f9ea864df540d629cd87fbc7988be8e2'),
 'karte_data_template/data/asr/sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10/bpe.model': (476049,
                                                                                                               '027731f33cff7266f2878c6fb7e478cf4af983962e311bb565112792794c13cd'),
 'karte_data_template/data/asr/sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10/decoder-epoch-75-avg-11-chunk-16-left-128.onnx': (33837085,
                                                                                                                                                    '7ebc63f34b21c8efb4a41a5a2eee7fe1448829ce0230ecc5369e67fc14d90d48'),
 'karte_data_template/data/asr/sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10/encoder-epoch-75-avg-11-chunk-16-left-128.int8.onnx': (296583597,
                                                                                                                                                         'f9001ed7a9e46d0294438c1a30cd7c72d1cc4bdd4e7880edbcda36f67081e32e'),
 'karte_data_template/data/asr/sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10/joiner-epoch-75-avg-11-chunk-16-left-128.int8.onnx': (8257421,
                                                                                                                                                        'db88e3172323551abaa99b91b18fb422a27ea4a834fd0db10389f9478816f917'),
 'karte_data_template/data/asr/sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10/tokens.txt': (211260,
                                                                                                                '806870b26576e70be9c6503f60a961d70fd2022b257d7f6b26dc390942a9325f'),
 'libgcc_s_seh-1.dll': (149364, 'd7e343dd266747177c3699bc3201ea36eceef158e98530e089263bc55bb2a6d7'),
 'libportaudio.dll': (256896, '6e958de29c24d083ea56772968d3dd7473a0787a589684242db7d134b18add8b'),
 'libstdc++-6.dll': (2659216, '494bd9045948198ef4d3bc838c7362d4cfd49c890b439c096b1817aa7c7fa6ff'),
 'libwinpthread-1.dll': (66333, '72bf8802de3a99d28f0bc2db9a11f281a9f9f3fa83dab2bc67e69fac3bd3d9c0'),
 'licenses/gordonklaus-portaudio-MIT.txt': (1069,
                                            '64fc7217ad9020446ba1cfef71d0b7d90a994f471012311af9de4c740eabfab7'),
 'licenses/sherpa-onnx-Apache-2.0.txt': (11358,
                                         'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30'),
 'onnxruntime.dll': (17363968, 'daa77083a45bf525da0dde9e87f85d8eb146f58f9c9aa7124ca84545e1c0f148'),
 'sherpa-onnx-c-api.dll': (4544512, '614878147c05121aeb1514ec4fb3e48b89751591532eca9208235b9ab868306a'),
 'sherpa-onnx-cxx-api.dll': (258048, '0da42dfb964b5625c0f2cdd31cd831c0da4712a254e27edfe6eec4184f8c0cf7')}
OFFICIAL_PROVENANCE = {'schema_version': 1,
 'repository': 'https://github.com/kirimine170/Karte',
 'commit': '68e245204fe2b2af5c21d271d5b8213691b8cbd7',
 'tag': 'latest-main-successful-build',
 'asset': 'Karte-windows-amd64.zip',
 'asset_sha256': '6f4431e9d18eb5908f1f950e1a81b74724fd9f0f72f23a72a7c6b9ac58108532',
 'executable_sha256': '8cfda3d6cdac43a3ef962dfea7858baf308490640fafae327a743d32dd671a38',
 'renderer_commit': '30e5dfbab03f92e2eb7c8a67e17936579075fe88',
 'release_url': 'https://github.com/kirimine170/Karte/releases/tag/latest-main-successful-build',
 'asset_url': 'https://github.com/kirimine170/Karte/releases/download/latest-main-successful-build/Karte-windows-amd64.zip',
 'asset_bytes': 273191777,
 'acquired_at_utc': '2026-10-11T09:57:45.787773+00:00',
 'ci': {'run_id': 37729877657,
        'url': 'https://github.com/kirimine170/Karte/actions/runs/37729877657',
        'head_sha': '68e245204fe2b2af5c21d271d5b8213691b8cbd7',
        'status': 'completed',
        'conclusion': 'success'},
 'notices': [{'path': 'karte_data_template/data/asr/sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10/LICENSE',
              'bytes': 13788,
              'sha256': '3f51ae494d838ad7a57aae7cb8a75b848d90b87d0ef43602589f7e38ecd655ac'},
             {'path': 'licenses/gordonklaus-portaudio-MIT.txt',
              'bytes': 1069,
              'sha256': '64fc7217ad9020446ba1cfef71d0b7d90a994f471012311af9de4c740eabfab7'},
             {'path': 'licenses/sherpa-onnx-Apache-2.0.txt',
              'bytes': 11358,
              'sha256': 'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30'}],
 'licensing': {'scope': 'Local user-requested bundle only; no new license grant or public redistribution '
                        'approval.',
               'root_license': 'No root LICENSE/COPYING grant found at the pinned repository revision; GitHub API '
                               'license is null.',
               'runtime_notice_gap': 'The official asset includes ONNX Runtime, upstream PortAudio and '
                                     'GCC/runtime DLLs without separate notices for those components. All three '
                                     'supplied notices are preserved; complete third-party license coverage is '
                                     'not asserted.'}}
_IDENTITY_FIELDS = ("task_id", "doc_id", "relative_path", "source_sha256", "markdown_sha256", "renderer_commit")


def native_error_text(code: str) -> str:
    explanations = {
        "native_karte_busy": "起動準備中または本体が起動中です．既存のウィンドウを確認してください．",
        "native_karte_expired": "起動準備の有効期限が切れました．もう一度操作してください．",
        "native_karte_changed": "記録が変わったためコピーしませんでした．選択し直してください．",
        "native_karte_assets": "同梱アプリと出所情報の検査に失敗しました．配布フォルダーを確認してください．",
        "native_karte_root": "専用保存先を安全に作成できません．設定の artifact_root を確認してください．",
        "native_karte_space": "専用保存先の空き容量が不足しています．別の artifact_root を設定してください．",
        "native_karte_launch": "本体を起動できませんでした．作成済みの専用コピーは保持します．",
    }
    return f"Karte 本体を開けません（{code}）．" + explanations.get(code, "現在の閲覧権限と配布物を確認してください．")


def _plain(path: Path, *, directory: bool) -> os.stat_result:
    value = path.lstat()
    if (stat.S_ISLNK(value.st_mode)
            or getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
            or (not directory and value.st_nlink != 1)):
        raise OSError("Nonordinary path")
    return value


def _chain(path: Path, *, must_exist=False) -> Path:
    """Check lexical ancestors before resolving; do not silently follow a junction."""
    if not path.is_absolute() or ".." in path.parts or path.drive.startswith("\\\\"):
        raise OSError("Nonlocal absolute path")
    nearest = None
    for part in (*reversed(path.parents), path):
        if os.path.lexists(part):
            _plain(part, directory=True)
            nearest = part
        elif must_exist:
            raise OSError("Missing directory")
    if nearest is None:
        raise OSError("Missing volume")
    return nearest


def _hash_file(path: Path, expected_size: int) -> str:
    before = _plain(path, directory=False)
    if before.st_size != expected_size:
        raise OSError("Wrong file size")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
        raise OSError("File changed")
    return digest


def _same_json(actual, expected) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(_same_json(actual[key], item) for key, item in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_json(a, b) for a, b in zip(actual, expected, strict=True))
    return actual == expected


def validate_official_app(app_directory: Path, provenance_path: Path) -> dict:
    """Validate the exact official payload; shared by runtime and build preflight."""
    try:
        app_directory, provenance_path = Path(app_directory), Path(provenance_path)
        _chain(app_directory, must_exist=True)
        _chain(provenance_path.parent, must_exist=True)
        info = _plain(provenance_path, directory=False)
        if not 0 < info.st_size <= 32768:
            raise OSError("Invalid provenance size")
        provenance = _json(provenance_path.read_bytes(), "native_karte_assets")
        if not _same_json(provenance, OFFICIAL_PROVENANCE):
            raise OSError("Unexpected provenance")
        expected_dirs = {str(parent).replace("\\", "/") for name in OFFICIAL_FILES
                         for parent in Path(name).parents if str(parent) != "."}
        seen = set()
        pending = [app_directory]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(app_directory).as_posix()
                    if relative in OFFICIAL_FILES:
                        size, digest = OFFICIAL_FILES[relative]
                        if _hash_file(path, size) != digest:
                            raise OSError("Asset hash mismatch")
                        seen.add(relative)
                    elif relative in expected_dirs:
                        _plain(path, directory=True)
                        pending.append(path)
                    else:
                        raise OSError("Unexpected asset")
        if seen != set(OFFICIAL_FILES):
            raise OSError("Missing asset")
        return provenance
    except (OSError, ValueError, TypeError):
        raise InspectionError("native_karte_assets") from None


def _bundle_paths() -> tuple[Path, Path]:
    bundle = Path(__file__).resolve().parent / "karte_native"
    return bundle / "app", bundle / "provenance.json"


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _destination_parent(config: DesktopConfig | None, bundle: Path) -> Path:
    base = config.artifact_root if config is not None else application_data() / "runs"
    parent = Path(base) / "karte-desktop"
    try:
        nearest = _chain(parent)
        resolved = parent.resolve()
        if _overlap(resolved, bundle.resolve()) or (
            config is not None and config.karte_root is not None
            and _overlap(resolved, config.karte_root.resolve())
        ):
            raise OSError("Overlapping root")
        # Official startup copies its model template even while ASR is disabled.
        required = sum(size for name, (size, _) in OFFICIAL_FILES.items()
                       if name.startswith("karte_data_template/")) + 64 * 1024 * 1024
        if shutil.disk_usage(nearest).free < required:
            raise InspectionError("native_karte_space")
        return parent
    except (OSError, ValueError):
        raise InspectionError("native_karte_root") from None


@dataclass(frozen=True)
class _Snapshot:
    identity: tuple[str, ...]
    markdown: bytes

    @property
    def relative_path(self) -> str:
        return self.identity[2]


def _snapshot(config: DesktopConfig, task_id: str) -> _Snapshot:
    value = KarteRenderer(config).snapshot(task_id)
    identity = tuple(value[key] for key in _IDENTITY_FIELDS)
    body = value["markdown"]
    # Defense in depth at the write boundary; the adapter also verifies these.
    if (identity[0] != task_id or not isinstance(body, bytes) or not body
            or hashlib.sha256(body).hexdigest() != value["markdown_sha256"]
            or not isinstance(value["relative_path"], str)
            or re.fullmatch(r"content/projects/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/note/worklog-"
                            + re.escape(task_id) + r"\.md", value["relative_path"]) is None):
        raise InspectionError("native_karte_changed")
    return _Snapshot(identity, body)


@dataclass(frozen=True)
class NativeLaunchOffer:
    task_id: str | None
    config: DesktopConfig | None
    app_directory: Path
    provenance_path: Path
    parent: Path
    snapshot: _Snapshot | None
    expires_at: float


@dataclass(frozen=True)
class NativeLaunchResult:
    data_root: Path
    relative_path: str | None
    pid: int
    session_root: Path


class NativeKarteLauncher:
    """One pending offer or live owned app, with no process teardown method."""

    def __init__(self):
        self._lock = threading.RLock()
        self._pending = None
        self._process = None

    @property
    def active(self) -> bool:
        with self._lock:
            if self._process is not None and self._process.poll() is not None:
                self._process = None
            return self._process is not None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._pending is not None

    def prepare(self, config: DesktopConfig | None, task_id: str | None = None) -> NativeLaunchOffer:
        token = object()
        with self._lock:
            if self._pending is not None or self.active:
                raise InspectionError("native_karte_busy")
            self._pending = token
        try:
            app, provenance = _bundle_paths()
            validate_official_app(app, provenance)
            parent = _destination_parent(config, app)
            if task_id is not None and config is None:
                raise InspectionError("karte_not_configured")
            snapshot = _snapshot(config, task_id) if task_id is not None else None
            offer = NativeLaunchOffer(task_id, config, app, provenance, parent, snapshot,
                                      time.monotonic() + NATIVE_OFFER_TTL_SECONDS)
            with self._lock:
                self._pending = offer
            return offer
        except Exception:
            with self._lock:
                if self._pending is token:
                    self._pending = None
            raise

    def discard(self, offer: NativeLaunchOffer):
        with self._lock:
            if self._pending is offer:
                self._pending = None

    def launch(self, offer: NativeLaunchOffer) -> NativeLaunchResult:
        # Callers adopt only current request generations on Tk. Keep the final
        # reauthorization/copy/spawn serialized; no background process launch.
        with self._lock:
            if self._pending is not offer or self.active:
                raise InspectionError("native_karte_busy")
            try:
                self._check_expiry(offer)
                validate_official_app(offer.app_directory, offer.provenance_path)
                if _destination_parent(offer.config, offer.app_directory) != offer.parent:
                    raise InspectionError("native_karte_root")
                if offer.task_id is not None and _snapshot(offer.config, offer.task_id) != offer.snapshot:
                    raise InspectionError("native_karte_changed")
                self._check_expiry(offer)
                return self._commit(offer)
            finally:
                self._pending = None

    @staticmethod
    def _check_expiry(offer):
        if time.monotonic() >= offer.expires_at:
            raise InspectionError("native_karte_expired")

    def _commit(self, offer: NativeLaunchOffer) -> NativeLaunchResult:
        try:
            # No source data root is created, initialized, copied or passed on.
            _chain(offer.parent)
            offer.parent.mkdir(parents=True, exist_ok=True)
            _chain(offer.parent, must_exist=True)
            session = offer.parent / secrets.token_hex(16)
            session.mkdir(exist_ok=False)
            _chain(session, must_exist=True)
            data = session / "data"
            for name in ("data", "appdata", "localappdata", "temp"):
                (session / name).mkdir(exist_ok=False)
            asr = data / "data" / "asr"
            asr.mkdir(parents=True, exist_ok=False)
            self._write_new(session, asr / "config.json", b'{"enabled":false}\n')
            relative_path = None
            receipt = {"schema_version": 1, "native_commit": NATIVE_COMMIT,
                       "native_executable_sha256": OFFICIAL_PROVENANCE["executable_sha256"],
                       "kind": "dedicated-data"}
            if offer.snapshot is not None:
                relative_path = offer.snapshot.relative_path
                target = data / relative_path
                target.parent.mkdir(parents=True, exist_ok=False)
                self._write_new(session, target, offer.snapshot.markdown)
                receipt.update(dict(zip(_IDENTITY_FIELDS, offer.snapshot.identity, strict=True)))
                receipt["kind"] = "authorized-derived-markdown-copy"
            self._write_new(session, session / "snapshot-receipt.json",
                            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            environment = os.environ.copy()
            environment.update(KARTE_DATA_DIR=str(data), APPDATA=str(session / "appdata"),
                               LOCALAPPDATA=str(session / "localappdata"), TEMP=str(session / "temp"),
                               TMP=str(session / "temp"))
            executable = offer.app_directory / "karte.exe"
        except (OSError, ValueError):
            raise InspectionError("native_karte_root") from None
        try:
            # A user-owned GUI lifetime: no Pi Job Object, pipes or cleanup kill.
            process = subprocess.Popen(
                [str(executable)], cwd=session, env=environment, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except (OSError, ValueError):
            raise InspectionError("native_karte_launch") from None
        self._process = process
        return NativeLaunchResult(data, relative_path, process.pid, session)

    @staticmethod
    def _write_new(session: Path, target: Path, body: bytes):
        _chain(target.parent, must_exist=True)
        if not target.is_relative_to(session) or os.path.lexists(target):
            raise OSError("Unexpected destination")
        with target.open("xb") as output:
            output.write(body)
        if _hash_file(target, len(body)) != hashlib.sha256(body).hexdigest():
            raise OSError("Copy hash mismatch")
