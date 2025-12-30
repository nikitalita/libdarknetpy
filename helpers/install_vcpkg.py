import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

PYGIT2_EXISTS = False
# check python >= 3.8
if sys.version_info >= (3, 8) and not (os.environ.get("INSTALL_VCPKG_USE_CLI_GIT")):
    try:
        import pygit2

        PYGIT2_EXISTS = True
    except ImportError:
        PYGIT2_EXISTS = False
        pass


def clone_repository(
    url,
    path,
    bare=False,
    repository=None,
    remote=None,
    checkout_branch=None,
    callbacks=None,
    depth=0,
):
    """
    Clones a new Git repository from *url* in the given *path*.
    Modded version of the pygit2 clone_repository function that allows for shallow clones

    Returns: a Repository class pointing to the newly cloned repository.

    Parameters:

    url : str
        URL of the repository to clone.
    path : str
        Local path to clone into.
    bare : bool
        Whether the local repository should be bare.
    remote : callable
        Callback for the remote to use.

        The remote callback has `(Repository, name, url) -> Remote` as a
        signature. The Remote it returns will be used instead of the default
        one.
    repository : callable
        Callback for the repository to use.

        The repository callback has `(path, bare) -> Repository` as a
        signature. The Repository it returns will be used instead of creating a
        new one.
    checkout_branch : str
        Branch to checkout after the clone. The default is to use the remote's
        default branch.
    callbacks : RemoteCallbacks
        Object which implements the callbacks as methods.

        The callbacks should be an object which inherits from
        `pyclass:RemoteCallbacks`.
    depth : int
        Depth to clone.

        If greater than zero, the clone will be a shallow clone.
        Defaults to 0 (unshallow).
    """
    if not PYGIT2_EXISTS:
        raise Exception("pygit2 not found, please install pygit2 and try again")
    from pygit2 import RemoteCallbacks, Repository, git_clone_options, git_fetch_options
    from pygit2.ffi import C, ffi
    from pygit2.utils import to_bytes

    if callbacks is None:
        callbacks = RemoteCallbacks()

    # Add repository and remote to the payload
    payload = callbacks
    payload.repository = repository
    payload.remote = remote

    with git_clone_options(payload):
        opts = payload.clone_options
        opts.bare = bare

        if checkout_branch:
            checkout_branch_ref = ffi.new("char []", to_bytes(checkout_branch))
            opts.checkout_branch = checkout_branch_ref

        with git_fetch_options(payload, opts=opts.fetch_opts):
            opts.fetch_opts.depth = depth
            crepo = ffi.new("git_repository **")
            err = C.git_clone(crepo, to_bytes(url), to_bytes(path), opts)
            payload.check_error(err)

    # Ok
    return Repository._from_c(crepo[0], owned=True)


def git_init(target: Path, url: str):
    if PYGIT2_EXISTS:
        repo = pygit2.init_repository(str(target), bare=False)
        repo.remotes.create("origin", url)
    else:
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=target, check=True)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=target, check=True)


def git_pull(target: Path, baseline_commit: Optional[str] = None):
    if PYGIT2_EXISTS:
        print("USING PYGIT2")
        repo = pygit2.Repository(str(target))
        if repo.is_bare or len(repo.remotes) == 0 or repo.remotes["origin"] is None:
            raise Exception(
                "git repo not found, please remove the directory and try again"
            )
        refspecs = [baseline_commit] if baseline_commit else None
        repo.remotes["origin"].fetch(refspecs=refspecs, depth=1)
        fetch_head_ref = repo.lookup_reference("FETCH_HEAD")
        repo.checkout(fetch_head_ref)
    else:
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", baseline_commit],
            cwd=target,
            check=True,
        )
        subprocess.run(["git", "checkout", baseline_commit], cwd=target, check=True)


def git_clone(target: Path, url: str, baseline_commit: Optional[str] = None):
    if baseline_commit:
        git_init(target, url)
        git_pull(target, baseline_commit)
    else:
        if PYGIT2_EXISTS:
            clone_repository(url, str(target), depth=1)
        else:
            target_base_dir = (target / "..").resolve()
            target_last_part = target.name
            subprocess.run(
                ["git", "clone", "--depth=1", url, target_last_part],
                cwd=target_base_dir,
                check=True,
            )


def check_git_cli():
    try:
        subprocess.run(["git", "--version"], check=True)
    except subprocess.CalledProcessError:
        return False
    return True


def check_git() -> bool:
    if PYGIT2_EXISTS:
        return True
    return check_git_cli()


def get_baseline_from_vcpkgjson(vcpkg_json_path: Path) -> str:
    import json

    builtin_baseline = ""
    with open(vcpkg_json_path) as f:
        vcpkg_json = json.load(f)
        builtin_baseline = vcpkg_json["builtin-baseline"]
    return builtin_baseline


def install_vcpkg(build_temp: Path, baseline_commit: str) -> Path:
    try:
        os.environ["VCPKG_DISABLE_METRICS"] = "true"
        vcpkg_root = build_temp / "vcpkg"
        vcpkg_exists = False
        if not check_git():
            raise Exception("git not found, please install git and try again")
        if vcpkg_root.exists():
            print("vcpkg already exists, attempting to update")
            # try git pull first; check if it fails
            try:
                git_pull(vcpkg_root, baseline_commit)
                vcpkg_exists = True
            except subprocess.CalledProcessError:
                print("Failed to update vcpkg, removing and re-cloning")
                shutil.rmtree(vcpkg_root)
        if not vcpkg_exists:
            # clone vcpkg
            git_clone(
                vcpkg_root, "https://github.com/microsoft/vcpkg.git", baseline_commit
            )
        if sys.platform.startswith("win32"):
            subprocess.run([".\\bootstrap-vcpkg.bat"], cwd=vcpkg_root, shell=True, check=True)
        else:
            subprocess.run(["./bootstrap-vcpkg.sh"], cwd=vcpkg_root, check=True)
        # Set vcpkg root
        os.environ["VCPKG_ROOT"] = str(vcpkg_root)
        return vcpkg_root
    except subprocess.CalledProcessError as e:
        raise Exception(
            "Failed to detect and install vcpkg, please install vcpkg and set VCPKG_ROOT to the vcpkg root directory"
        ) from e


def get_vcpkg_triplet(plat_name: str) -> str:
    arch: str
    if (
        plat_name.find("amd64") != -1
        or plat_name.find("x86_64") != -1
        or plat_name.find("x64") != -1
    ):
        arch = "x64"
    elif (
        plat_name.find("x86") != -1
        or plat_name.find("i686") != -1
        or plat_name.find("i386") != -1
        or plat_name.find("i586") != -1
        or plat_name.find("win32") != -1
    ):
        arch = "x86"
    elif plat_name.find("arm64") != -1 or plat_name.find("aarch64") != -1:
        arch = "arm64"
    elif plat_name.find("arm") != -1 or plat_name.find("arm32") != -1:
        arch = "arm"
    elif plat_name.find("universal2") != -1:
        arch = "universal2"
    target_os: str
    if plat_name.find("linux") != -1:
        target_os = "linux"
    elif (
        plat_name.find("macos") != -1
        or plat_name.find("osx") != -1
        or plat_name.find("darwin") != -1
    ):
        target_os = "osx"
    elif plat_name.find("win") != -1:
        target_os = "windows"
    return f"{arch}-{target_os}"


def get_vcpkg_static_triplet(plat_name: str) -> str:
    triplet = get_vcpkg_triplet(plat_name)
    if triplet.find("windows") != -1:
        triplet += "-static"
    return triplet


def get_vcpkg_static_md_triplet(plat_name: str) -> str:
    triplet = get_vcpkg_triplet(plat_name)
    if triplet.find("windows") != -1:
        triplet += "-static-md"
    return triplet


def install_vcpkg_manifest(
    sourcedir: Path,
    install_dir: Optional[Path] = None,
    vcpkg_triplet: Optional[str] = None,
    vcpkg_root: Optional[Path] = None,
    **kwargs,
):
    if not vcpkg_root:
        vcpkg_root = Path(os.environ["VCPKG_ROOT"])
    vcpkg_exe = vcpkg_root / (
        "vcpkg" + (".exe" if sys.platform.startswith("win32") else "")
    )
    args = [str(vcpkg_exe), "install"]
    if vcpkg_triplet:
        args += ["--triplet", vcpkg_triplet]

    if install_dir:
        args += ["--x-install-root", str(install_dir)]
    args += ["--vcpkg-root", str(vcpkg_root)]
    if kwargs:
        for key, value in kwargs.items():
            args += [f"--{key}", str(value)]

    # copy os.environ and add VCPKG_ROOT to it
    env = os.environ.copy()
    env["VCPKG_ROOT"] = str(vcpkg_root)
    env["VCPKG_DEFAULT_TRIPLET"] = vcpkg_triplet
    subprocess.run(args, cwd=sourcedir, env=env, check=True)


# COPIED AND pasted from lipo_dir_merge because of depedenency issues
def make_merger(primary_path, secondary_path):
    # Merge the libraries at `src1` and `src2` and create a
    # universal binary at `dst`
    def merge_libs(src1, src2, dst):
        subprocess.run(["lipo", "-create", src1, src2, "-output", dst])

    # Find the library at `src` in the `secondary_path` and then
    # merge the two versions, creating a universal binary at `dst`.
    def find_and_merge_libs(src, dst):
        rel_path = os.path.relpath(src, primary_path)
        lib_in_secondary = os.path.join(secondary_path, rel_path)

        if os.path.exists(lib_in_secondary) is False:
            print(f"Lib not found in secondary source: {lib_in_secondary}")
            return

        merge_libs(src, lib_in_secondary, dst)

    # Either copy the file at `src` to `dst`, or, if it is a static
    # library, merge it with its version from `secondary_path` and
    # write the universal binary to `dst`.
    def copy_file_or_merge_libs(src, dst, *, follow_symlinks=True):
        _, file_ext = os.path.splitext(src)
        if file_ext == ".a":
            find_and_merge_libs(src, dst)
        else:
            shutil.copy2(src, dst, follow_symlinks=follow_symlinks)

    return copy_file_or_merge_libs


def lipo_dir_merge(primary_path, secondary_path, destination_path):
    shutil.copytree(
        primary_path,
        destination_path,
        copy_function=make_merger(primary_path, secondary_path),
    )


def make_vcpkg_universal2_binaries(
    vcpkg_install_root: Path,
    arm64_triplet: str = "arm64-osx",
    x64_triplet: str = "x64-osx",
) -> str:
    """
    Make universal2 binaries
    Run this after vcpkg install of both x64 and arch64 binaries
    """
    arm64 = vcpkg_install_root / arm64_triplet
    x64 = vcpkg_install_root / x64_triplet
    universal2 = vcpkg_install_root / "universal2-osx"
    lipo_dir_merge(arm64, x64, universal2)
    return "universal2-osx"


def install_vcpkg_universal2_binaries(
    sourcedir: Path,
    install_dir: Optional[Path] = None,
    vcpkg_root: Optional[Path] = None,
    arm64_triplet: str = "arm64-osx",
    x64_triplet: str = "x64-osx",
):
    """
    install universal2 binaries
    vcpkg installs both x64 and arch64 binaries and then lipos them together
    """
    if not vcpkg_root:
        vcpkg_root = Path(os.environ["VCPKG_ROOT"])
    # check the host processor
    cross_install_dir = (install_dir / ".." / "cross_installed").resolve()
    host_install_dir = (install_dir / ".." / "host_installed").resolve()
    cross_install_triplet: str
    host_install_triplet: str
    # we need to install the non-host triplets to a temp directory first before merging, otherwise vcpkg just uninstalls them
    if platform.machine() != "arm64":
        cross_install_triplet = arm64_triplet
        host_install_triplet = x64_triplet
    else:
        cross_install_triplet = x64_triplet
        host_install_triplet = arm64_triplet
    install_vcpkg_manifest(
        sourcedir, host_install_dir, host_install_triplet, vcpkg_root
    )
    install_vcpkg_manifest(
        sourcedir, cross_install_dir, cross_install_triplet, vcpkg_root
    )
    # move the files from the temp directory to the install directory
    shutil.move(
        cross_install_dir / cross_install_triplet,
        host_install_dir / cross_install_triplet,
    )
    shutil.rmtree(cross_install_dir, ignore_errors=True)
    uni2_triplet = make_vcpkg_universal2_binaries(
        host_install_dir, arm64_triplet, x64_triplet
    )
    shutil.rmtree(install_dir, ignore_errors=True)
    shutil.move(host_install_dir, install_dir)
    return uni2_triplet

    # Make universal2 binaries
