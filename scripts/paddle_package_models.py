#!/usr/bin/env python3
"""Prepare/verify all 15 original Paddle models, without conversion or inference.

Build preparation reuses the native project's pinned archive transport/parser.
Verification and staging use only the standard library and the fixed pins
digest below, so the same verifier works inside an offline final package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

PINNED_SOURCE_SHA256 = "db66f8583dd143a21a2949b4a4833da41ce98dcfd5e7837df7c8800f8884c015"
BUILD_PINS = Path(__file__).resolve().parents[1] / "native/ocr/model-source-pins.json"
BUILD_HELPER = BUILD_PINS.with_name("prepare_models.py")
PINS_NAME = "model-source-pins.json"
INVENTORY_NAME = "paddle-model-inventory.json"
SOURCE_FILES = frozenset(("inference.json", "inference.pdiparams", "inference.yml"))


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _plain_path(path):
    path = Path(os.path.abspath(path))
    if path.is_symlink():
        raise ValueError("Model path must not be a symlink: " + str(path))
    # macOS legitimately aliases /var and /tmp. Canonicalize ancestors while
    # rejecting links at the supplied root; verify_models checks every child.
    return path.resolve()


def _model_path(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid model identity")
    if name.startswith("PP-OCRv6_"):
        version = "V6"
    elif "_PP-OCRv5_" in name:
        version = "V5"
    elif "_PP-OCRv3_" in name:
        version = "V3"
    else:
        raise ValueError("Unsupported model generation: " + name)
    return version + "/" + name + "_infer"


def _read_pins(path):
    path = _plain_path(path)
    if not path.is_file():
        raise ValueError("Missing regular original-model pins file")
    if path.stat().st_size > 65536:
        raise ValueError("Original-model pins exceed the reviewed metadata bound")
    data = path.read_bytes()
    # The supplied artifact's inventory and hashes cannot authorize new pins.
    if hashlib.sha256(data).hexdigest() != PINNED_SOURCE_SHA256:
        raise ValueError("Original-model pins differ from the fixed trusted SHA256")
    pins = json.loads(data)
    rows = pins.get("models", [])
    if pins.get("schema_version") != 1 or len(rows) != 15:
        raise ValueError("Expected exactly 15 original Paddle models")
    names = set()
    generations = {"V6": 0, "V5": 0, "V3": 0}
    for row in rows:
        name = row["name"]
        folder = _model_path(name)
        if name in names or set(row["files"]) != SOURCE_FILES:
            raise ValueError("Duplicate model or unexpected original source file set")
        names.add(name)
        generations[folder.split("/")[0]] += 1
        if type(row["archive_bytes"]) is not int or row["archive_bytes"] <= 0:
            raise ValueError("Invalid source archive size")
        if any(not re.fullmatch("[0-9a-f]{64}", value) for value in
               [row["archive_sha256"], *row["files"].values()]):
            raise ValueError("Invalid source digest")
        expected_url = ("https://paddle-model-ecology.bj.bcebos.com/paddlex/"
                        "official_inference_model/paddle3.0.0/" + name + "_infer.tar")
        if row["url"] != expected_url:
            raise ValueError("Unapproved original-model source")
    if generations != {"V6": 4, "V5": 10, "V3": 1}:
        raise ValueError("Incomplete model generation coverage")
    return data, pins


def _inventory(root, pins):
    models = []
    files = {}
    for row in pins["models"]:
        folder = _model_path(row["name"])
        models.append({"name": row["name"], "directory": folder,
                       "source_url": row["url"], "archive_sha256": row["archive_sha256"],
                       "languages": row.get("languages", [])})
        for name, expected in row["files"].items():
            relative = folder + "/" + name
            files[relative] = {"sha256": expected, "bytes": (root / relative).stat().st_size}
    return {"schema_version": 1, "model_count": 15,
            "source_pins_sha256": PINNED_SOURCE_SHA256,
            "generations": {"V6": 4, "V5": 10, "V3": 1},
            "models": models, "files": files, "inference_verified": False}


def verify_models(root: Path) -> dict:
    """Independently verify exact source bytes; never claim model inference."""
    root = _plain_path(root)
    if not root.is_dir():
        raise ValueError("Original-model artifact must be a directory")
    _, pins = _read_pins(root / PINS_NAME)
    expected = {PINS_NAME, INVENTORY_NAME}
    directories = {"V3", "V5", "V6"}
    for row in pins["models"]:
        folder = _model_path(row["name"])
        directories.add(folder)
        expected.update(folder + "/" + name for name in SOURCE_FILES)
    actual = set()
    actual_directories = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("Invalid original-model asset: " + relative)
        if path.is_dir():
            actual_directories.add(relative)
        else:
            actual.add(relative)
    if actual != expected or actual_directories != directories:
        raise ValueError("Original-model tree differs from the complete pinned 15-model set")
    for row in pins["models"]:
        folder = _model_path(row["name"])
        for name, expected_hash in row["files"].items():
            relative = folder + "/" + name
            if _digest(root / relative) != expected_hash:
                raise ValueError("Original-model source hash mismatch: " + relative)
    report = _inventory(root, pins)
    expected_inventory = _canonical(report)
    if ((root / INVENTORY_NAME).stat().st_size != len(expected_inventory)
            or (root / INVENTORY_NAME).read_bytes() != expected_inventory):
        raise ValueError("Original-model inventory differs from independently verified sources")
    return report


def _empty_destination(path):
    if path.is_symlink():
        raise ValueError("Model output must not be a symlink: " + str(path))
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise FileExistsError("Model output must be absent or empty; refusing replacement: " + str(path))


def _publish(stage, output):
    _empty_destination(output)
    if output.exists():
        output.rmdir()
    os.replace(stage, output)


def _checked_archive(path, row):
    if (path.is_symlink() or not path.is_file()
            or path.stat().st_size != row["archive_bytes"]
            or _digest(path) != row["archive_sha256"]):
        raise ValueError("Corrupt pinned source archive; refusing replacement: " + str(path))


def _build_tools():
    # No native source checkout or third-party module is needed by verify-only.
    import importlib.util
    spec = importlib.util.spec_from_file_location("vse_original_model_transport", BUILD_HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _diagnostic_source_url(row):
    """Display only the pinned origin/path, never URL credentials or tokens."""
    from urllib.parse import urlsplit
    host = "paddle-model-ecology.bj.bcebos.com"
    path = "/paddlex/official_inference_model/paddle3.0.0/" + row["name"] + "_infer.tar"
    try:
        value = urlsplit(row["url"])
        if (value.scheme == "https" and value.hostname == host
                and value.port in (None, 443) and value.path == path):
            return "https://" + host + path
    except ValueError:
        pass
    return "<untrusted source withheld>"


def prepare_models(output: Path, cache: Path, offline=False) -> dict:
    """Atomically export all pinned original sources to a separate build tree."""
    output, cache = _plain_path(output), _plain_path(cache)
    _empty_destination(output)
    if output == cache or output in cache.parents or cache in output.parents:
        raise ValueError("Model output and archive cache must not overlap")
    pin_bytes, pins = _read_pins(BUILD_PINS)
    transport = _build_tools()
    output.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    # Check every existing hit before any network action, including dangling links.
    for row in pins["models"]:
        archive = cache / (row["archive_sha256"] + ".tar")
        if archive.exists() or archive.is_symlink():
            _checked_archive(archive, row)
    with tempfile.TemporaryDirectory(prefix=".paddle-models-", dir=output.parent) as temporary:
        stage = Path(temporary) / "stage"
        stage.mkdir()
        for version in ("V3", "V5", "V6"):
            (stage / version).mkdir()
        for row in pins["models"]:
            cached = cache / (row["archive_sha256"] + ".tar")
            context = {"model": row["name"], "archive_sha256": row["archive_sha256"],
                       "source_url": _diagnostic_source_url(row), "offline": bool(offline),
                       "cache": "present" if cached.exists() or cached.is_symlink() else "missing"}
            try:
                archive = transport.get_archive(row, cache, offline)
            except Exception as error:
                # Preserve the transport exception/status and its existing retry
                # policy. Never log a response/redirect URL supplied by the error.
                error.add_note("Original Paddle archive request: " + json.dumps(context, sort_keys=True))
                raise
            _checked_archive(archive, row)
            transport.extract_sources(archive, row, stage / _model_path(row["name"]))
        (stage / PINS_NAME).write_bytes(pin_bytes)
        (stage / INVENTORY_NAME).write_bytes(_canonical(_inventory(stage, pins)))
        report = verify_models(stage)
        _publish(stage, output)
    return report


def stage_paddle_models(source: Path, destination: Path) -> dict:
    """Copy only an independently verified complete tree; verify the copy too."""
    source, destination = _plain_path(source), _plain_path(destination)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Model source and destination must not overlap")
    _empty_destination(destination)
    verify_models(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".paddle-models-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "stage"
        shutil.copytree(source, stage, symlinks=True)
        report = verify_models(stage)
        _publish(stage, destination)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--verify-only", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    if args.verify_only is not None:
        if args.cache is not None or args.offline:
            parser.error("--verify-only is always offline and takes no cache/offline options")
        report = verify_models(args.verify_only)
    else:
        if args.cache is None:
            parser.error("--output requires --cache")
        report = prepare_models(args.output, args.cache, args.offline)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
