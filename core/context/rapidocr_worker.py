"""Isolated RapidOCR worker process.

This module exists for one reason: ONNX Runtime DirectML failures can be native
access violations.  If DML runs inside Aria's pythonw.exe process, Python cannot
catch the crash.  Running DML OCR in this helper process lets the parent treat a
native crash as "worker died" and fall back to CPU without taking down Aria.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import struct
import sys
import traceback


def _setup_protocol_streams():
    """Reserve original stdout for the binary protocol, silence normal stdout."""

    stdin = sys.stdin.buffer
    stdout_fd = sys.stdout.fileno()
    protocol_fd = os.dup(stdout_fd)

    if os.name == "nt":
        try:
            import msvcrt

            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(protocol_fd, os.O_BINARY)
            msvcrt.setmode(stdout_fd, os.O_BINARY)
        except Exception:
            pass

    # Anything printed by RapidOCR / ORT to fd=1 must not corrupt the protocol.
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull_fd, stdout_fd)
    finally:
        os.close(devnull_fd)
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")

    return stdin, os.fdopen(protocol_fd, "wb", buffering=0)


def _write_msg(out, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    out.write(struct.pack(">I", len(data)))
    out.write(data)
    out.flush()


def _read_exact(stream, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("parent closed pipe")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_msg(stream) -> dict:
    header = _read_exact(stream, 4)
    size = struct.unpack(">I", header)[0]
    if size <= 0 or size > 80_000_000:
        raise ValueError(f"invalid message size: {size}")
    data = _read_exact(stream, size)
    return json.loads(data.decode("utf-8"))


def _v5_paths() -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    model_dir = repo_root / "models" / "rapidocr" / "v5"
    return {
        "det_model_path": str(model_dir / "PP-OCRv5_det_mobile_infer.onnx"),
        "rec_model_path": str(model_dir / "PP-OCRv5_rec_mobile_infer.onnx"),
        "cls_model_path": str(model_dir / "ch_PP-OCRv4_cls_infer.onnx"),
        "rec_keys_path": str(model_dir / "ppocrv5_dict.txt"),
    }


def _component_providers(engine) -> dict[str, list[str]]:
    providers: dict[str, list[str]] = {}
    for name, attr in (
        ("det", "text_det"),
        ("cls", "text_cls"),
        ("rec", "text_rec"),
    ):
        component = getattr(engine, attr, None)
        runner = getattr(component, "infer", None) or getattr(component, "session", None)
        session = getattr(runner, "session", None)
        if hasattr(session, "get_providers"):
            try:
                providers[name] = list(session.get_providers())
            except Exception:
                providers[name] = []
        else:
            providers[name] = []
    return providers


def _init_engine(use_v5: bool, use_dml: bool):
    ort_dir = os.path.join(sys.prefix, "Lib", "site-packages", "onnxruntime", "capi")
    if os.path.isdir(ort_dir):
        try:
            os.add_dll_directory(ort_dir)
        except Exception:
            pass

    from rapidocr_onnxruntime import RapidOCR

    kwargs = {}
    if use_v5:
        kwargs.update(_v5_paths())
    if use_dml:
        kwargs.update(det_use_dml=True, cls_use_dml=True, rec_use_dml=True)
    return RapidOCR(**kwargs)


def _infer(engine, request: dict) -> dict:
    from PIL import Image
    import numpy as np

    mode = request.get("mode") or "RGB"
    width, height = request.get("size") or [0, 0]
    raw = base64.b64decode(request.get("data") or "")
    if width <= 0 or height <= 0:
        raise ValueError("invalid image size")

    img = Image.frombytes(mode, (width, height), raw)
    arr = np.array(img)
    result, elapsed = engine(arr)
    texts = [r[1] for r in result] if result else []
    return {"ok": True, "texts": texts, "elapsed": elapsed or []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-v5", action="store_true")
    parser.add_argument("--use-dml", action="store_true")
    args = parser.parse_args()

    stdin, out = _setup_protocol_streams()

    try:
        engine = _init_engine(use_v5=args.use_v5, use_dml=args.use_dml)
        _write_msg(
            out,
            {
                "ok": True,
                "event": "ready",
                "providers": _component_providers(engine),
            },
        )
    except Exception as exc:
        _write_msg(
            out,
            {
                "ok": False,
                "event": "init_error",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=8),
            },
        )
        return 2

    while True:
        try:
            request = _read_msg(stdin)
            if request.get("cmd") == "shutdown":
                _write_msg(out, {"ok": True, "event": "shutdown"})
                return 0
            if request.get("cmd") != "infer":
                _write_msg(out, {"ok": False, "error": "unknown command"})
                continue
            _write_msg(out, _infer(engine, request))
        except EOFError:
            return 0
        except Exception as exc:
            _write_msg(
                out,
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=8),
                },
            )


if __name__ == "__main__":
    raise SystemExit(main())
