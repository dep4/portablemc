"""Module providing tasks for directly launcher forge mod loader versions.
"""

from zipfile import ZipFile
from pathlib import Path
from io import BytesIO
import subprocess
import shutil
import json
import os

from .vanilla import parse_download_entry, Context, MetadataRoot, MetadataTask, \
    VersionRepository, VersionRepositories, Version, VersionNotFoundError, Jvm, JarTask
from .util import calc_input_sha1, LibrarySpecifier
from .download import DownloadList, DownloadTask
from .task import Task, State, Watcher, Sequence
from .http import http_request, HttpError

from typing import Dict, Optional, Set, List


class ForgeRoot:
    """Represent the root forge version to load.
    """
    __slots__ = "prefix", "forge_version"
    def __init__(self, prefix: str, forge_version: str) -> None:
        self.prefix = prefix
        self.forge_version = forge_version


class ForgePostProcessor:
    """Describe the execution model of a post process.
    """
    __slots__ = "jar_name", "class_path", "args", "sha1"
    def __init__(self, jar_name: str, class_path: List[str], args: List[str], sha1: Dict[str, str]) -> None:
        self.jar_name = jar_name
        self.class_path = class_path
        self.args = args
        self.sha1 = sha1


class ForgePostInfo:
    """Internal state, used only when forge installer is "modern" (>= 1.12.2-14.23.5.2851)
    describing data and post processors.
    """

    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir
        self.variables: Dict[str, str] = {}   # Data for variable replacements.
        self.libraries: Dict[str, Path] = {}  # Install-time libraries.  FIXME: Get rid of this?
        self.processors: List[ForgePostProcessor] = []


class ForgeInitTask(Task):
    """Task for initializing forge version resolving, if the `ForgeRoot` state is present,
    the forge version resolving starts and a custom repository will be used for that 
    version.

    :in ForgeRoot: Optional, the forge version to load if present.
    :in VersionRepositories: Used to register the fabric's version repository.
    :out MetadataRoot: The root version to load, for metadata task.
    """

    def execute(self, state: State, watcher: Watcher) -> None:
        
        root = state.get(ForgeRoot)
        if root is None:
            return
        
        forge_version = root.forge_version

        # Compute version id and forward to metadata task with specific repository.
        version_id = f"{root.prefix}-{forge_version}"
        state.insert(MetadataRoot(version_id))
        state[VersionRepositories].insert(version_id, FabricRepository(forge_version))
        

class FabricRepository(VersionRepository):
    """Internal class used as instance mapped to the forge version.
    """

    def __init__(self, forge_version: str) -> None:
        self.forge_version = forge_version

    def load_version(self, version: Version, state: State) -> bool:
        # TODO: Various checks for presence of libs.
        return super().load_version(version, state)
    
    def fetch_version(self, version: Version, state: State) -> None:

        context = state[Context]
        dl = state[DownloadList]

        # Extract the game version from the forge version, we'll use
        # it to add suffix to find the right forge version if needed.
        game_version = self.forge_version.split("-", 1)[0]

        # For some older game versions, some odd suffixes were used 
        # for the version scheme.
        suffixes = [""] + {
            "1.11":     ["-1.11.x"],
            "1.10.2":   ["-1.10.0"],
            "1.10":     ["-1.10.0"],
            "1.9.4":    ["-1.9.4"],
            "1.9":      ["-1.9.0", "-1.9"],
            "1.8.9":    ["-1.8.9"],
            "1.8.8":    ["-1.8.8"],
            "1.8":      ["-1.8"],
            "1.7.10":   ["-1.7.10", "-1710ls", "-new"],
            "1.7.2":    ["-mc172"],
        }.get(game_version, [])

        # Iterate suffix and find the first install JAR that works.
        install_jar = None
        for suffix in suffixes:
            try:
                install_jar = request_install_jar(f"{self.forge_version}{suffix}")
                break
            except HttpError as error:
                if error.res.status != 404:
                    raise
                # Silently ignore if the file was not found.
                pass
        
        if install_jar is None:
            raise VersionNotFoundError(version)
        
        with install_jar:

            # The install profiles comes in multiples forms:
            # 
            # >= 1.12.2-14.23.5.2851
            #  There are two files, 'install_profile.json' which 
            #  contains processors and shared data, and `version.json`
            #  which is the raw version meta to be fetched.
            #
            # <= 1.12.2-14.23.5.2847
            #  There is only an 'install_profile.json' with the version
            #  meta stored in 'versionInfo' object. Each library have
            #  two keys 'serverreq' and 'clientreq' that should be
            #  removed when the profile is returned.

            try:
                info = install_jar.getinfo("install_profile.json")
                with install_jar.open(info) as fp:
                    install_profile = json.load(fp)
            except KeyError:
                raise ForgeInstallError(self.forge_version, ForgeInstallError.INSTALL_PROFILE_NOT_FOUND)

            # print(f"{install_profile=}")

            if "json" in install_profile:

                # Forge versions since 1.12.2-14.23.5.2851
                info = install_jar.getinfo(install_profile["json"].lstrip("/"))
                with install_jar.open(info) as fp:
                    version.metadata = json.load(fp)

                # We use the bin directory if there is a need to extract temporary files.
                post_init = ForgePostInfo(context.gen_bin_dir())

                # Parse processors
                for i, processor in enumerate(install_profile["processors"]):

                    processor_sides = processor.get("sides", [])
                    if not isinstance(processor_sides, list):
                        raise ValueError(f"forge profile: /json/processors/{i}/sides must be an array")

                    if len(processor_sides) and "client" not in processor_sides:
                        continue

                    processor_jar_name = processor.get("jar")
                    if not isinstance(processor_jar_name, str):
                        raise ValueError(f"forge profile: /json/processors/{i}/jar must be a string")

                    post_init.processors.append(ForgePostProcessor(
                        processor_jar_name,
                        processor.get("classpath", []),
                        processor.get("args", []),
                        processor.get("outputs", {})
                    ))

                # We fetch all libraries used to build artifacts, and we store each path 
                # to each library here. These install profile libraries are only used for
                # building, and will be used in finalize task.
                for i, install_lib in enumerate(install_profile["libraries"]):

                    lib_name = install_lib["name"]
                    lib_spec = LibrarySpecifier.from_str(lib_name)
                    lib_artifact = install_lib["downloads"]["artifact"]
                    lib_path = context.libraries_dir / lib_spec.file_path()

                    post_init.libraries[lib_name] = lib_path
                    
                    if len(lib_artifact["url"]):
                        dl.add(parse_download_entry(lib_artifact, lib_path, "forge profile: /json/libraries/"), verify=True)
                    else:
                        # The lib should be stored inside the JAR file, under maven/ directory.
                        zip_extract_file(install_jar, f"maven/{lib_spec.file_path()}", lib_path)

                # Just keep the 'client' values.
                for data_key, data_val in install_profile["data"].items():

                    data_val = str(data_val["client"])

                    # Refer to a file inside the JAR file.
                    if data_val.startswith("/"):
                        dst_path = post_init.tmp_dir / data_val[1:]
                        zip_extract_file(install_jar, data_val[1:], dst_path)
                        data_val = str(dst_path)  # Replace by the path of extracted file.

                    post_init.variables[data_key] = data_val
                
                state.insert(post_init)

            else: 

                # Forge versions before 1.12.2-14.23.5.2847
                version.metadata = install_profile.get("versionInfo")
                if version.metadata is None:
                    raise ForgeInstallError(self.forge_version, ForgeInstallError.VERSION_METADATA_NOT_FOUND)
                
                # Older versions have non standard keys for libraries.
                for version_lib in version.metadata["libraries"]:
                    if "serverreq" in version_lib:
                        del version_lib["serverreq"]
                    if "clientreq" in version_lib:
                        del version_lib["clientreq"]
                    if "checksums" in version_lib:
                        del version_lib["checksums"]
                
                # For "old" installers, that have an "install" section.
                jar_entry_path = install_profile["install"]["filePath"]
                jar_spec = LibrarySpecifier.from_str(install_profile["install"]["path"])

                # Here we copy the forge jar stored to libraries.
                jar_path = context.libraries_dir / jar_spec.file_path()
                zip_extract_file(install_jar, jar_entry_path, jar_path)

        version.metadata["id"] = version.id
        version.write_metadata_file()


class ForgeFinalizeTask(Task):
    """Finalize task that run the post install processors for modern installer, after the
    first forge download.

    :in ForgePostInfo: Optional, if present the corresponding post install is executed.
    :in Jvm: The JVM, used for running the processors.
    """

    def execute(self, state: State, watcher: Watcher) -> None:
        
        info = state.get(ForgePostInfo)
        if info is None:
            return  # No post processing to do
        
        context = state[Context]
        jvm = state[Jvm]
        version = state[Version]
        
        # Additional missing variables, the version's jar file is the same as the vanilla
        # one, so we use its path.
        info.variables["SIDE"] = "client"
        info.variables["MINECRAFT_JAR"] = str(version.jar_file())

        def replace_install_args(txt: str) -> str:
            txt = txt.format_map(info.variables)
            # Replace the pattern [lib name] with lib path.
            if txt[0] == "[" and txt[-1] == "]":
                spec = LibrarySpecifier.from_str(txt[1:-1])
                txt = str(context.libraries_dir / spec.file_path())
            elif txt[0] == "'" and txt[-1] == "'":
                txt = txt[1:-1]
            return txt

        for processor in info.processors:

            # Extract the main-class from manifest. Required because we cannot use 
            # both -cp and -jar.
            jar_path = info.libraries[processor.jar_name]
            main_class = None
            with ZipFile(jar_path) as jar_fp:
                with jar_fp.open("META-INF/MANIFEST.MF") as manifest_fp:
                    for manifest_line in manifest_fp.readlines():
                        if manifest_line.startswith(b"Main-Class: "):
                            main_class = manifest_line[12:].decode().strip()
                            break
            
            if main_class is None:
                raise ValueError(f"cannot find main class in {jar_path}")

            # Try to find the task name in the arguments, just for information purpose.
            if len(processor.args) >= 2 and processor.args[0] == "--task":
                task = processor.args[1]
            elif processor.jar_name.startswith("net.minecraftforge:jarsplitter:"):
                task = "JAR_SPLITTER"
            elif processor.jar_name.startswith("net.minecraftforge:ForgeAutoRenamingTool:"):
                task = "AUTO_RENAMING"
            elif processor.jar_name.startswith("net.minecraftforge:binarypatcher:"):
                task = "BINARY_PATCHER"
            else:
                task = "UNKNOWN"

            # Compute the full arguments list.
            args = [
                str(jvm.executable_file),
                "-cp", os.pathsep.join([str(jar_path), *(str(info.libraries[lib_name]) for lib_name in processor.class_path)]),
                main_class,
                *(replace_install_args(arg) for arg in processor.args)
            ]

            watcher.on_event(ForgePostProcessingEvent(task))

            completed = subprocess.run(args, cwd=context.work_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if completed.returncode != 0:
                raise ValueError("ERROR")
            
            # If there are sha1, check them.
            for lib_name, expected_sha1 in processor.sha1.items():
                lib_name = replace_install_args(lib_name)
                expected_sha1 = replace_install_args(expected_sha1)
                with open(lib_name, "rb") as fp:
                    actual_sha1 = calc_input_sha1(fp)
                    if actual_sha1 != expected_sha1:
                        raise ValueError(f"invalid sha1 for '{lib_name}', got {actual_sha1}, expected {expected_sha1}")
        
        # Finally, remove the temporary directory.
        shutil.rmtree(info.tmp_dir, ignore_errors=True)

        watcher.on_event(ForgePostProcessedEvent())


class ForgeInstallError(Exception):
    """Errors that can happen while trying to install forge.
    """

    INSTALL_PROFILE_NOT_FOUND = "install_profile_not_found"
    VERSION_METADATA_NOT_FOUND = "version_meta_not_found"

    def __init__(self, version: str, code: str):
        self.version = version
        self.code = code


class ForgePostProcessingEvent:
    """Event triggered when a post processing task is starting.
    """
    def __init__(self, task: str) -> None:
        self.task = task

class ForgePostProcessedEvent:
    """Event triggered when forge post processing has finished, the game is ready to run.
    """


def request_promo_versions() -> Dict[str, str]:
    return http_request("GET", "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json", 
        accept="application/json").json()["promos"]


def request_maven_versions() -> Optional[Set[str]]:
    """Internal function that parses maven metadata of forge in order to get all 
    supported forge versions.
    """

    text = http_request("GET", "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml", 
        accept="application/xml").text()
    
    versions = set()
    last_idx = 0

    # It's not really correct to parse XML like this, but I find this
    # acceptable since the schema is well known and it should be a
    # little bit easier to do thing like this.
    while True:
        start_idx = text.find("<version>", last_idx)
        if start_idx == -1:
            break
        end_idx = text.find("</version>", start_idx + 9)
        if end_idx == -1:
            break
        versions.add(text[(start_idx + 9):end_idx])
        last_idx = end_idx + 10

    return versions


def request_install_jar(version: str) -> ZipFile:
    """Internal function to request the installation JAR file.
    """
    
    res = http_request("GET", f"https://maven.minecraftforge.net/net/minecraftforge/forge/{version}/forge-{version}-installer.jar",
        accept="application/java-archive")
    
    return ZipFile(BytesIO(res.data))


def zip_extract_file(zf: ZipFile, entry_path: str, dst_path: Path):
    """Special function used to extract a specific file entry to a destination. 
    This is different from ZipFile.extract because the latter keep the full entry's path.
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(entry_path) as src, dst_path.open("wb") as dst:
        shutil.copyfileobj(src, dst)


def add_forge_tasks(seq: Sequence) -> None:
    """Add tasks to a sequence for installing and running a Fabric mod loader version.

    The fabric tasks will run if the `FabricRoot` state is present, in such case a 
    `MetadataRoot` will be created if version resolution succeed.

    :param seq: The sequence to alter and add tasks to.
    """
    seq.prepend_task(ForgeInitTask(), before=MetadataTask)
    seq.append_task(ForgeFinalizeTask(), after=JarTask)  # Run after JVM/Jar because need it.
    seq.prepend_task(DownloadTask(), before=ForgeFinalizeTask)  # Between forge tasks.