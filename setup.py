# mypy: ignore-errors

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.dist import Distribution

if sys.version_info < (3, 12):
    pass
else:
    pass
import importlib
from importlib.machinery import SourceFileLoader

import pybind11_stubgen
from pybind11 import get_cmake_dir

# load the helpers module from the helpers package

# get current dir
current_dir = os.path.dirname(os.path.realpath(__file__))
install_vcpkg_path = os.path.join(current_dir, "helpers", "install_vcpkg.py")
install_vcpkg_module = SourceFileLoader(
    "install_vcpkg", install_vcpkg_path
).load_module()
install_vcpkg = install_vcpkg_module.install_vcpkg
get_baseline_from_vcpkgjson = install_vcpkg_module.get_baseline_from_vcpkgjson
get_vcpkg_static_triplet = install_vcpkg_module.get_vcpkg_static_triplet
make_vcpkg_universal2_binaries = install_vcpkg_module.make_vcpkg_universal2_binaries
install_vcpkg_manifest = install_vcpkg_module.install_vcpkg_manifest
install_vcpkg_universal2_binaries = (
    install_vcpkg_module.install_vcpkg_universal2_binaries
)

# Convert distutils Windows platform specifiers to CMake -A arguments
PLAT_TO_CMAKE = {
    "win32": "Win32",
    "win-amd64": "x64",
    "win-arm32": "ARM",
    "win-arm64": "ARM64",
}


class _PackageFinder:
    """
    Custom loader to allow loading built modules from their location
    in the build directory (as opposed to their install location)
    """

    mapping: ClassVar[dict] = {}

    @classmethod
    def find_spec(cls, fullname, path, target=None):
        m = cls.mapping.get(fullname)
        if m:
            return importlib.util.spec_from_file_location(fullname, m)


# A CMakeExtension needs a sourcedir instead of a file list.
# The name must be the _single_ output extension from the CMake build.
# If you need multiple extensions, see scikit-build.
class CMakeExtension(Extension):
    def __init__(self, name: str, sourcedir: str = "") -> None:
        super().__init__(name, sources=[])
        self.sourcedir = os.fspath(Path(sourcedir).resolve())


class CMakeBuild(build_ext):
    def build_extension(self, ext: CMakeExtension) -> None:
        # Must be in this form due to bug in .resolve() only fixed in Python 3.10+
        ext_fullpath = Path.cwd() / self.get_ext_fullpath(ext.name)
        extdir = ext_fullpath.parent.resolve()
        # Using this requires trailing slash for auto-detection & inclusion of
        # auxiliary "native" libs
        plat_name: str = self.plat_name

        debug = int(os.environ.get("DEBUG", 0)) if self.debug is None else self.debug
        cfg = "Debug" if debug else "Release"

        # get the target triplet
        target_triplet = os.environ.get(
            "VCPKG_DEFAULT_TRIPLET", get_vcpkg_static_triplet(plat_name)
        )
        os.environ["VCPKG_DEFAULT_TRIPLET"] = target_triplet

        # CMake lets you override the generator - we need to check this.
        # Can be set with Conda-Build, for example.
        cmake_generator = os.environ.get("CMAKE_GENERATOR", "")

        # Set Python_EXECUTABLE instead if you use PYBIND11_FINDPYTHON
        # EXAMPLE_VERSION_INFO shows you how to pass a value into the C++ code
        # from Python.
        cmake_args = [
            f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={extdir}{os.sep}",
            f"-DPYTHON_EXECUTABLE={sys.executable}",
            f"-DCMAKE_BUILD_TYPE={cfg}",  # not used on MSVC, but no harm
        ]
        build_args = []
        # Adding CMake arguments set as environment variable
        # (needed e.g. to build for ARM OSx on conda-forge)
        if "CMAKE_ARGS" in os.environ:
            cmake_args += [item for item in os.environ["CMAKE_ARGS"].split(" ") if item]

        # In this example, we pass in the version to C++. You might not need to.
        cmake_args += [f"-DEXAMPLE_VERSION_INFO={self.distribution.get_version()}"]

        if self.compiler.compiler_type != "msvc":
            # Using Ninja-build since it a) is available as a wheel and b)
            # multithreads automatically. MSVC would require all variables be
            # exported for Ninja to pick it up, which is a little tricky to do.
            # Users can override the generator with CMAKE_GENERATOR in CMake
            # 3.15+.
            if not cmake_generator or cmake_generator == "Ninja":
                try:
                    import ninja

                    ninja_executable_path = Path(ninja.BIN_DIR) / "ninja"
                    cmake_args += [
                        "-GNinja",
                        f"-DCMAKE_MAKE_PROGRAM:FILEPATH={ninja_executable_path}",
                    ]
                except ImportError:
                    pass

        else:
            # Single config generators are handled "normally"
            single_config = any(x in cmake_generator for x in {"NMake", "Ninja"})

            # CMake allows an arch-in-generator style for backward compatibility
            contains_arch = any(x in cmake_generator for x in {"ARM", "Win64"})

            # Specify the arch if using MSVC generator, but only if it doesn't
            # contain a backward-compatibility arch spec already in the
            # generator name.
            if not single_config and not contains_arch:
                cmake_args += ["-A", PLAT_TO_CMAKE[self.plat_name]]

            # Multi-config generators have a different way to specify configs
            if not single_config:
                cmake_args += [
                    f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_{cfg.upper()}={extdir}"
                ]
                build_args += ["--config", cfg]

        if sys.platform.startswith("darwin"):
            # Cross-compile support for macOS - respect ARCHFLAGS if set
            archs = re.findall(r"-arch (\S+)", os.environ.get("ARCHFLAGS", ""))
            if archs:
                cmake_args += ["-DCMAKE_OSX_ARCHITECTURES={}".format(";".join(archs))]

        # Set CMAKE_BUILD_PARALLEL_LEVEL to control the parallel build level
        # across all generators.
        if "CMAKE_BUILD_PARALLEL_LEVEL" not in os.environ:
            # self.parallel is a Python 3 only way to set parallel jobs by hand
            # using -j in the build_ext call, not supported by pip or PyPA-build.
            if hasattr(self, "parallel") and self.parallel:
                # CMake 3.12+ only.
                build_args += [f"-j{self.parallel}"]

        build_temp = (Path(self.build_temp) / ext.name).resolve()
        if not build_temp.exists():
            build_temp.mkdir(parents=True)
        if not os.environ.get("VCPKG_ROOT") and not os.environ.get(
            "LIBDARKNETPY_NO_VCPKG"
        ):
            print("VCPKG_ROOT not set, attempting to install vcpkg")
            vcpkg_json_path = self.get_root(ext) / "vcpkg.json"
            baseline = get_baseline_from_vcpkgjson(vcpkg_json_path)
            install_vcpkg(build_temp, baseline)
        print("VCPKG_ROOT set to {}".format(os.environ.get("VCPKG_ROOT")))
        # include vcpkg toolchain file from VCPKG_ROOT
        if not os.environ.get("VCPKG_ROOT"):
            raise Exception(
                "VCPKG_ROOT not set, please install vcpkg and set VCPKG_ROOT to the vcpkg root directory"
            )
        vcpkg_root = Path(os.environ.get("VCPKG_ROOT") or "")
        toolchain_file = vcpkg_root / "scripts" / "buildsystems" / "vcpkg.cmake"
        print(f"Toolchain file: {toolchain_file!s}")
        cmake_args += [f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file!s}"]
        cmake_args += [f"-DVCPKG_ROOT={vcpkg_root!s}"]
        cmake_args += [f"-DZ_VCPKG_ROOT_DIR={vcpkg_root!s}"]
        cmake_args += ["-DVCPKG_INSTALL_OPTIONS=--clean-after-build"]
        # set pybind11_DIR
        cmake_args += [f"-Dpybind11_DIR={get_cmake_dir()}"]

        if target_triplet == "universal2-osx":
            # make two child dirs in build_temp, one for each target

            # find the DCMAKE_LIBRARY_OUTPUT_DIRECTORY in the cmake_args
            # and replace it with the build_temp

            build_temp_x86_64 = build_temp / "x86_64"
            build_temp_arm64 = build_temp / "arm64"
            build_temp_x86_64.mkdir(parents=True, exist_ok=True)
            build_temp_arm64.mkdir(parents=True, exist_ok=True)
            build_temp_x86_64_out = build_temp_x86_64 / "out"
            build_temp_arm64_out = build_temp_arm64 / "out"

            cmake_args_x86_64 = [
                *cmake_args,
                "-DCMAKE_OSX_ARCHITECTURES=x86_64",
                "-DVCPKG_TARGET_TRIPLET=x64-osx",
            ]
            cmake_args_arm64 = [
                *cmake_args,
                "-DCMAKE_OSX_ARCHITECTURES=arm64",
                "-DVCPKG_TARGET_TRIPLET=arm64-osx",
            ]
            for i, arg in enumerate(cmake_args):
                if arg.startswith("-DCMAKE_LIBRARY_OUTPUT_DIRECTORY"):
                    cmake_args_x86_64[i] = "-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={}".format(
                        build_temp_x86_64_out
                    )
                    cmake_args_arm64[i] = "-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={}".format(
                        build_temp_arm64_out
                    )
                    break

            os.environ["VCPKG_DEFAULT_TRIPLET"] = "arm64-osx"
            # same for arm64
            subprocess.run(
                ["cmake", ext.sourcedir, *cmake_args_arm64],
                env=os.environ,
                cwd=build_temp_arm64,
                check=True,
            )
            subprocess.run(
                ["cmake", "--build", ".", *build_args],
                env=os.environ,
                cwd=build_temp_arm64,
                check=True,
            )
            # now find the libraries and merge them
            lib_arm64 = next(
                f
                for f in os.listdir(build_temp_arm64_out)
                if f.startswith("_libdarknetpy") and f.endswith(".so")
            )
            lib_arm64_temp_path = build_temp_arm64_out / lib_arm64

            # x86_64
            # run cmake for each target
            os.environ["VCPKG_DEFAULT_TRIPLET"] = "x64-osx"
            subprocess.run(
                ["cmake", ext.sourcedir, *cmake_args_x86_64],
                env=os.environ,
                cwd=build_temp_x86_64,
                check=True,
            )
            subprocess.run(
                ["cmake", "--build", ".", *build_args],
                env=os.environ,
                cwd=build_temp_x86_64,
                check=True,
            )
            lib_x86_64 = next(
                f
                for f in os.listdir(build_temp_x86_64_out)
                if f.startswith("_libdarknetpy") and f.endswith(".so")
            )
            lib_x86_64_temp_path = build_temp_x86_64_out / lib_x86_64

            # run lipo
            subprocess.run(
                [
                    "lipo",
                    "-create",
                    "-output",
                    lib_x86_64,
                    str(lib_x86_64_temp_path),
                    str(lib_arm64_temp_path),
                ],
                env=os.environ,
                cwd=extdir,
                check=True,
            )
        else:
            try:
                cmake_args += [f"-DVCPKG_TARGET_TRIPLET={target_triplet}"]
                subprocess.run(
                    ["cmake", ext.sourcedir, *cmake_args],
                    env=os.environ,
                    cwd=build_temp,
                    check=True,
                )
                subprocess.run(
                    ["cmake", "--build", ".", *build_args],
                    env=os.environ,
                    cwd=build_temp,
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                print(f"Error building {ext.name}: {e}")
                raise e

        if self.inplace:
            # copy the library to the source directory
            # get the library name (It's something like libdarknetpy.cpython-310-darwin.so)
            library_name = next(
                f
                for f in os.listdir(build_temp)
                if f.startswith("_libdarknetpy") and f.endswith(".so")
            )
            shutil.copy(
                build_temp / library_name,
                Path(ext.sourcedir) / "src" / "libdarknetpy" / library_name,
            )

        # This isn't working right now
        # self.generate_pyi(build_temp)

    def get_root(self, ext: CMakeExtension) -> Path:
        sourcedir = Path(ext.sourcedir)
        print(f"Root is {sourcedir.resolve()}")
        return sourcedir.resolve()

    def generate_pyi(self, build_temp: Path) -> None:
        # Configure custom loader
        _PackageFinder.mapping = {"libdarknetpy": str(build_temp / "libdarknetpy")}
        sys.meta_path.insert(0, _PackageFinder)

        # Generate pyi modules
        stubgen_args = [
            "--root-suffix=''",
            "--ignore-all-errors",
            "libdarknetpy",
        ]
        # subprocess.run(
        #     ["pybind11-stubgen"] + stubgen_args, cwd=build_temp, check=True
        # )
        args = pybind11_stubgen.arg_parser().parse_args(stubgen_args)

        parser = pybind11_stubgen.stub_parser_from_args(args)
        printer = pybind11_stubgen.Printer(
            invalid_expr_as_ellipses=not args.print_invalid_expressions_as_is
        )

        out_dir, sub_dir = pybind11_stubgen.to_output_and_subdir(
            output_dir=args.output_dir,
            module_name=args.module_name,
            root_suffix=args.root_suffix,
        )

        pybind11_stubgen.run(
            parser,
            printer,
            args.module_name,
            out_dir,
            sub_dir=sub_dir,
            dry_run=args.dry_run,
            writer=pybind11_stubgen.Writer(stub_ext=args.stub_extension),
        )


class LibdarknetpyDistribution(Distribution):
    def has_ext_modules(self) -> bool:
        return True


# The information here can also be placed in setup.cfg - better separation of
# logic and declaration, and simpler if you include description/version in a file.
setup(
    name="libdarknetpy",
    version="0.0.1",
    author="NikitaLita",
    author_email="nikitalita@fakeemail.com",
    description="A test project using pybind11 and CMake",
    long_description="",
    packages=["libdarknetpy"],
    package_data={"libdarknetpy": ["py.typed", "*.so", "*.pyi"]},
    package_dir={"libdarknetpy": "src/libdarknetpy"},
    data_files=[],
    ext_modules=[CMakeExtension("libdarknetpy._libdarknetpy")],
    cmdclass={"build_ext": CMakeBuild},
    zip_safe=False,
    extras_require={"test": ["pytest>=6.0"]},
    python_requires=">=3.7",
    distclass=LibdarknetpyDistribution,
    requires=["pybind11", "helpers"],
)
