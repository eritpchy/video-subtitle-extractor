"""Build only static image primitives; native platform required."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import platform
import subprocess


def build(source, build_dir, prefix, cmake='cmake', jobs=3, generator=None):
    flags = dict(CMAKE_BUILD_TYPE='Release', CMAKE_INSTALL_PREFIX=str(Path(prefix).resolve()),
        CMAKE_POLICY_VERSION_MINIMUM='3.5', BUILD_LIST='core,imgproc,imgcodecs',
        BUILD_SHARED_LIBS='OFF', BUILD_TESTS='OFF', BUILD_PERF_TESTS='OFF',
        BUILD_EXAMPLES='OFF', BUILD_DOCS='OFF', BUILD_opencv_apps='OFF',
        BUILD_opencv_python2='OFF', BUILD_opencv_python3='OFF', WITH_IPP='OFF',
        WITH_ITT='OFF', WITH_OPENCL='OFF', WITH_TBB='OFF', WITH_OPENMP='OFF',
        WITH_LAPACK='OFF', WITH_EIGEN='OFF', WITH_PROTOBUF='OFF', WITH_ADE='OFF',
        WITH_FFMPEG='OFF', WITH_GSTREAMER='OFF', WITH_V4L='OFF', WITH_GTK='OFF',
        WITH_QT='OFF', WITH_JPEG='ON', BUILD_JPEG='ON', WITH_PNG='ON', BUILD_PNG='ON',
        BUILD_ZLIB='ON', WITH_TIFF='OFF', WITH_WEBP='OFF', WITH_OPENEXR='OFF',
        WITH_OPENJPEG='OFF', WITH_JASPER='OFF', WITH_GDAL='OFF', WITH_GDCM='OFF',
        WITH_AVIF='OFF', CPU_DISPATCH='', OPENCV_GENERATE_PKGCONFIG='OFF')
    if platform.system() == 'Windows':
        flags.update(CMAKE_MSVC_RUNTIME_LIBRARY='MultiThreadedDLL',
            CMAKE_C_FLAGS_RELEASE='/O2 /DNDEBUG /Gy /Gw', CMAKE_CXX_FLAGS_RELEASE='/O2 /DNDEBUG /Gy /Gw')
    else:
        flags.update(CMAKE_C_FLAGS_RELEASE='-O3 -DNDEBUG -ffunction-sections -fdata-sections',
            CMAKE_CXX_FLAGS_RELEASE='-O3 -DNDEBUG -ffunction-sections -fdata-sections')
        if platform.system() == 'Linux':
            flags['CPU_BASELINE'] = 'SSE3'
        if platform.system() == 'Darwin':
            # Reviewed ORT 1.31 arm64 SDK itself requires macOS 14.0.
            flags['CMAKE_OSX_DEPLOYMENT_TARGET'] = '14.0'
            flags['CMAKE_OSX_ARCHITECTURES'] = 'arm64'
    command = [cmake, '-S', str(Path(source).resolve()), '-B', str(Path(build_dir).resolve())]
    if generator:
        command += ['-G', generator]
    command += ['-D' + k + '=' + v for k, v in flags.items()]
    subprocess.run(command, check=True)
    subprocess.run([cmake, '--build', str(build_dir), '--config', 'Release', '--parallel', str(jobs)], check=True)
    subprocess.run([cmake, '--install', str(build_dir), '--config', 'Release'], check=True)
    return Path(prefix)

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--build-dir', required=True, type=Path)
    p.add_argument('--prefix', required=True, type=Path)
    p.add_argument('--cmake', default='cmake')
    p.add_argument('--generator')
    p.add_argument('--jobs', type=int, default=3)
    a = p.parse_args()
    build(a.source, a.build_dir, a.prefix, a.cmake, a.jobs, a.generator)
