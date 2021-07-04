#!/usr/bin/env python
# encoding: utf8

# PortableMC is a portable Minecraft launcher in only one Python script (without addons).
# Copyright (C) 2021 Théo Rozier
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


from typing import Generator, Callable, Optional, Tuple, Dict, Type, List
from http.client import HTTPConnection, HTTPSConnection
from urllib import parse as url_parse
from json import JSONDecodeError
from uuid import uuid4
from os import path
import shutil
import base64
import json
import sys
import os


LAUNCHER_NAME = "portablemc"
LAUNCHER_VERSION = "1.2.0"
LAUNCHER_AUTHORS = "Théo Rozier"


VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
ASSET_BASE_URL = "https://resources.download.minecraft.net/{}/{}"
AUTHSERVER_URL = "https://authserver.mojang.com/{}"
JVM_META_URL = "https://launchermeta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"

MS_OAUTH_CODE_URL = "https://login.live.com/oauth20_authorize.srf"
MS_OAUTH_LOGOUT_URL = "https://login.live.com/oauth20_logout.srf"
MS_OAUTH_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
MS_XBL_AUTH_DOMAIN = "user.auth.xboxlive.com"
MS_XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
MS_XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MS_GRAPH_UPN_REQUEST_URL = "https://graph.microsoft.com/v1.0/me?$select=userPrincipalName"
MC_AUTH_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"


class Context:

    def __init__(self, main_dir: Optional[str] = None, work_dir: Optional[str] = None):
        self.main_dir = Util.get_minecraft_dir() if main_dir is None else path.realpath(main_dir)
        self.work_dir = self.main_dir if work_dir is None else path.realpath(work_dir)
        self.versions_dir = path.join(self.main_dir, "versions")
        self.assets_dir = path.join(self.main_dir, "assets")
        self.libraries_dir = path.join(self.main_dir, "libraries")


class Version:

    def __init__(self, context: Context, version: str):

        self.context = context
        self.version = version

        self.manifest: Optional[VersionManifest] = None
        self.dl = DownloadList()

        self.version_meta: Optional[dict] = None
        self.version_dir: Optional[str] = None
        self.version_jar_file: Optional[str] = None

        self.assets_index_version: Optional[int] = None
        self.assets_virtual_dir: Optional[str] = None

    def install_meta(self):

        if self.manifest is None:
            self.manifest = VersionManifest.load_from_url()

        version_meta, version_dir = self._install_meta_internal(self.version)
        while "inheritsFrom" in version_meta:  # TODO: Add a safe recursion limit
            parent_meta, _ = self._install_meta_internal(version_meta["inheritsFrom"])
            del version_meta["inheritsFrom"]
            Util.merge_dict(version_meta, parent_meta)

        self.version_meta, self.version_dir = version_meta, version_dir

    def _install_meta_internal(self, version: str) -> Tuple[dict, str]:

        version_dir = path.join(self.context.main_dir, "versions", version)
        version_meta_file = path.join(version_dir, f"{version}.json")

        try:
            with open(version_meta_file, "rt") as version_meta_fp:
                return json.load(version_meta_fp), version_dir
        except (OSError, JSONDecodeError):
            version_super_meta = self.manifest.get_version(version)
            if version_super_meta is not None:
                content = Util.json_simple_request(version_super_meta["url"])
                os.makedirs(version_dir, exist_ok=True)
                with open(version_meta_file, "wt") as version_meta_fp:
                    json.dump(content, version_meta_fp, indent=2)
                return content, version_dir
            else:
                raise VersionError(VersionError.NOT_FOUND, version)

    def install_jar(self):

        if self.version_meta is None:
            raise ValueError("You must install metadata before.")

        version_jar_file = path.join(self.version_dir, self.version)
        if not path.isfile(self.version_jar_file):
            version_downloads = self.version_meta.get("downloads")
            if version_downloads is None or "client" not in version_downloads:
                raise VersionError(VersionError.JAR_NOT_FOUND)
            self.dl.append(DownloadEntry.from_meta(version_downloads["client"], self.version_jar_file, name="{}.jar".format(self.version)))
        self.version_jar_file = version_jar_file

    def install_assets(self):

        if self.version_meta is None:
            raise ValueError("You must install metadata before.")

        assets_indexes_dir = path.join(self.context.assets_dir, "indexes")
        assets_index_version = self.version_meta["assets"]
        assets_index_file = path.join(assets_indexes_dir, "{}.json".format(assets_index_version))

        try:
            with open(assets_index_file, "rb") as assets_index_fp:
                assets_index = json.load(assets_index_fp)
        except (OSError, JSONDecodeError):
            asset_index_info = self.version_meta["assetIndex"]
            asset_index_url = asset_index_info["url"]
            assets_index = Util.json_simple_request(asset_index_url)
            os.makedirs(assets_indexes_dir, exist_ok=True)
            with open(assets_index_file, "wt") as assets_index_fp:
                json.dump(assets_index, assets_index_fp)

        assets_objects_dir = path.join(self.context.assets_dir, "objects")
        assets_virtual_dir = path.join(self.context.assets_dir, "virtual", assets_index_version)
        assets_mapped_to_resources = assets_index.get("map_to_resources", False)  # For version <= 13w23b
        assets_virtual = assets_index.get("virtual", False)  # For 13w23b < version <= 13w48b (1.7.2)

        for asset_id, asset_obj in assets_index["objects"].items():
            asset_hash = asset_obj["hash"]
            asset_hash_prefix = asset_hash[:2]
            asset_size = asset_obj["size"]
            asset_file = path.join(assets_objects_dir, asset_hash_prefix, asset_hash)
            if not path.isfile(asset_file) or path.getsize(asset_file) != asset_size:
                asset_url = ASSET_BASE_URL.format(asset_hash_prefix, asset_hash)
                self.dl.append(DownloadEntry(asset_url, asset_file, size=asset_size, sha1=asset_hash, name=asset_id))

        def finalize():
            if assets_mapped_to_resources or assets_virtual:
                for asset_id_to_cpy in assets_index["objects"].keys():
                    if assets_mapped_to_resources:
                        resources_asset_file = path.join(self.context.work_dir, "resources", asset_id_to_cpy)
                        if not path.isfile(resources_asset_file):
                            os.makedirs(path.dirname(resources_asset_file), exist_ok=True)
                            shutil.copyfile(asset_file, resources_asset_file)
                    if assets_virtual:
                        virtual_asset_file = path.join(assets_virtual_dir, asset_id_to_cpy)
                        if not path.isfile(virtual_asset_file):
                            os.makedirs(path.dirname(virtual_asset_file), exist_ok=True)
                            shutil.copyfile(asset_file, virtual_asset_file)

        self.dl.add_callback(finalize)
        self.assets_index_version = assets_index_version
        self.assets_virtual_dir = assets_virtual_dir

    def install_logger(self):

        if "logging" in self.version_meta:
            version_logging = self.version_meta["logging"]
            if "client" in version_logging:

                log_config_dir = path.join(self.context.assets_dir, "log_configs")
                client_logging = version_logging["client"]
                logging_file_info = client_logging["file"]
                logging_file = path.join(log_config_dir, logging_file_info["id"])
                logging_dirty = False

                download_entry = DownloadEntry.from_meta(logging_file_info, logging_file, name=logging_file_info["id"])
                if not path.isfile(logging_file) or path.getsize(logging_file) != download_entry.size:
                    self.dl.append(download_entry)
                    logging_dirty = True

                """if better_logging:
                    real_logging_file = path.join(log_config_dir, "portablemc-{}".format(logging_file_info["id"]))
                else:
                    real_logging_file = logging_file

                def finalize():
                    if better_logging:
                        if logging_dirty or not path.isfile(real_logging_file):
                            with open(logging_file, "rt") as logging_fp:
                                with open(real_logging_file, "wt") as custom_logging_fp:
                                    raw = logging_fp.read() \
                                        .replace("<XMLLayout />", LOGGING_CONSOLE_REPLACEMENT) \
                                        .replace("<LegacyXMLLayout />", LOGGING_CONSOLE_REPLACEMENT)
                                    custom_logging_fp.write(raw)

                self.dl.add_callback(finalize)"""
                return client_logging["argument"].replace("${path}", real_logging_file)

    def install_libraries(self):
        pass

    def install(self):
        self.install_meta()
        self.install_jar()
        self.install_assets()
        self.install_logger()
        self.install_libraries()

    def start(self):
        pass


class VersionManifest:

    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load_from_url(cls):
        return cls(Util.json_simple_request(VERSION_MANIFEST_URL))

    def filter_latest(self, version: str) -> Tuple[Optional[str], bool]:
        return (self._data["latest"][version], True) if version in self._data["latest"] else (version, False)

    def get_version(self, version: str) -> Optional[dict]:
        version, _alias = self.filter_latest(version)
        for version_data in self._data["versions"]:
            if version_data["id"] == version:
                return version_data
        return None

    def all_versions(self) -> list:
        return self._data["versions"]

    def search_versions(self, inp: str) -> Generator[dict, None, None]:
        inp, alias = self.filter_latest(inp)
        for version_data in self._data["versions"]:
            if (alias and version_data["id"] == inp) or (not alias and inp in version_data["id"]):
                yield version_data


class AuthSession:

    type = "raw"
    fields = "access_token", "username", "uuid"

    def __init__(self, access_token: str, username: str, uuid: str):
        self.access_token = access_token
        self.username = username
        self.uuid = uuid

    def format_token_argument(self, legacy: bool) -> str:
        return "token:{}:{}".format(self.access_token, self.uuid) if legacy else self.access_token

    def validate(self) -> bool:
        return True

    def refresh(self):
        pass

    def invalidate(self):
        pass


class YggdrasilAuthSession(AuthSession):

    type = "yggdrasil"
    fields = "access_token", "username", "uuid", "client_token"

    def __init__(self, access_token: str, username: str, uuid: str, client_token: str):
        super().__init__(access_token, username, uuid)
        self.client_token = client_token

    def validate(self) -> bool:
        return self.request("validate", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        }, False)[0] == 204

    def refresh(self):
        _, res = self.request("refresh", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        })
        self.access_token = res["accessToken"]
        self.username = res["selectedProfile"]["name"]  # Refresh username if renamed (does it works? to check.).

    def invalidate(self):
        self.request("invalidate", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        }, False)

    @classmethod
    def authenticate(cls, email_or_username: str, password: str) -> 'YggdrasilAuthSession':
        _, res = cls.request("authenticate", {
            "agent": {
                "name": "Minecraft",
                "version": 1
            },
            "username": email_or_username,
            "password": password,
            "clientToken": uuid4().hex
        })
        return cls(res["accessToken"], res["selectedProfile"]["name"], res["selectedProfile"]["id"], res["clientToken"])

    @classmethod
    def request(cls, req: str, payload: dict, error: bool = True) -> Tuple[int, dict]:
        code, res = Util.json_request(AUTHSERVER_URL.format(req), "POST",
                                      data=json.dumps(payload).encode("ascii"),
                                      headers={"Content-Type": "application/json"},
                                      ignore_error=True)
        if error and code != 200:
            raise AuthError(AuthError.YGGDRASIL, res["errorMessage"])
        return code, res


class MicrosoftAuthSession(AuthSession):

    type = "microsoft"
    fields = "access_token", "username", "uuid", "refresh_token", "client_id", "redirect_uri"

    def __init__(self, access_token: str, username: str, uuid: str, refresh_token: str, client_id: str, redirect_uri: str):
        super().__init__(access_token, username, uuid)
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self._new_username = None  # type: Optional[str]

    def validate(self) -> bool:
        self._new_username = None
        code, res = self.mc_request(MC_PROFILE_URL, self.access_token)
        if code == 200:
            username = res["name"]
            if self.username != username:
                self._new_username = username
                return False
            return True
        return False

    def refresh(self):
        if self._new_username is not None:
            self.username = self._new_username
            self._new_username = None
        else:
            res = self.authenticate_base({
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                "scope": "xboxlive.signin"
            })
            self.access_token = res["access_token"]
            self.username = res["username"]
            self.uuid = res["uuid"]
            self.refresh_token = res["refresh_token"]

    @staticmethod
    def get_authentication_url(app_client_id: str, redirect_uri: str, email: str, nonce: str):
        return "{}?{}".format(MS_OAUTH_CODE_URL, url_parse.urlencode({
            "client_id": app_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code id_token",
            "scope": "xboxlive.signin offline_access openid email",
            "login_hint": email,
            "nonce": nonce,
            "response_mode": "form_post"
        }))

    @staticmethod
    def get_logout_url(app_client_id: str, redirect_uri: str):
        return "{}?{}".format(MS_OAUTH_LOGOUT_URL, url_parse.urlencode({
            "client_id": app_client_id,
            "redirect_uri": redirect_uri
        }))

    @classmethod
    def check_token_id(cls, token_id: str, email: str, nonce: str) -> bool:
        id_token_payload = json.loads(cls.base64url_decode(token_id.split(".")[1]))
        return id_token_payload["nonce"] == nonce and id_token_payload["email"] == email

    @classmethod
    def authenticate(cls, app_client_id: str, code: str, redirect_uri: str) -> 'MicrosoftAuthSession':
        res = cls.authenticate_base({
            "client_id": app_client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
            "scope": "xboxlive.signin"
        })
        return cls(res["access_token"], res["username"], res["uuid"], res["refresh_token"], app_client_id, redirect_uri)

    @classmethod
    def authenticate_base(cls, request_token_payload: dict) -> dict:

        # Microsoft OAuth
        _, res = cls.ms_request(MS_OAUTH_TOKEN_URL, request_token_payload, payload_url_encoded=True)
        ms_refresh_token = res["refresh_token"]

        # Xbox Live Token
        _, res = cls.ms_request(MS_XBL_AUTH_URL, {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": MS_XBL_AUTH_DOMAIN,
                "RpsTicket": "d={}".format(res["access_token"])
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT"
        })

        xbl_token = res["Token"]
        xbl_user_hash = res["DisplayClaims"]["xui"][0]["uhs"]

        # Xbox Live XSTS Token
        _, res = cls.ms_request(MS_XSTS_AUTH_URL, {
            "Properties": {
                "SandboxId": "RETAIL",
                "UserTokens": [xbl_token]
            },
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT"
        })
        xsts_token = res["Token"]

        if xbl_user_hash != res["DisplayClaims"]["xui"][0]["uhs"]:
            raise AuthError(AuthError.MICROSOFT_INCONSISTENT_USER_HASH)

        # MC Services Auth
        _, res = cls.ms_request(MC_AUTH_URL, {
            "identityToken": "XBL3.0 x={};{}".format(xbl_user_hash, xsts_token)
        })
        mc_access_token = res["access_token"]

        # MC Services Profile
        code, res = cls.mc_request(MC_PROFILE_URL, mc_access_token)

        if code == 404:
            raise AuthError(AuthError.MICROSOFT_DOES_NOT_OWN_MINECRAFT)
        elif code == 401:
            raise AuthError(AuthError.MICROSOFT_OUTDATED_TOKEN)
        elif "error" in res or code != 200:
            raise AuthError(AuthError.MICROSOFT, res.get("errorMessage", res.get("error", "Unknown error")))

        return {
            "refresh_token": ms_refresh_token,
            "access_token": mc_access_token,
            "username": res["name"],
            "uuid": res["id"]
        }

    @classmethod
    def ms_request(cls, url: str, payload: dict, *, payload_url_encoded: bool = False) -> Tuple[int, dict]:
        data = (url_parse.urlencode(payload) if payload_url_encoded else json.dumps(payload)).encode("ascii")
        content_type = "application/x-www-form-urlencoded" if payload_url_encoded else "application/json"
        return Util.json_request(url, "POST", data=data, headers={"Content-Type": content_type})

    @classmethod
    def mc_request(cls, url: str, bearer: str) -> Tuple[int, dict]:
        return Util.json_request(url, "GET", headers={"Authorization": "Bearer {}".format(bearer)})

    @classmethod
    def base64url_decode(cls, s: str) -> bytes:
        rem = len(s) % 4
        if rem > 0:
            s += "=" * (4 - rem)
        return base64.urlsafe_b64decode(s)


class AuthDatabase:

    types = {
        YggdrasilAuthSession.type: YggdrasilAuthSession,
        MicrosoftAuthSession.type: MicrosoftAuthSession
    }

    def __init__(self, filename: str, legacy_filename: str):
        self._filename = filename
        self._legacy_filename = legacy_filename
        self._sessions = {}  # type: Dict[str, Dict[str, AuthSession]]

    def load(self):
        self._sessions.clear()
        if not path.isfile(self._filename):
            self._load_legacy_and_delete()
        try:
            with open(self._filename, "rb") as fp:
                data = json.load(fp)
                for typ, typ_data in data.items():
                    if typ not in self.types:
                        continue
                    sess_type = self.types[typ]
                    sessions = self._sessions[typ] = {}
                    sessions_data = typ_data["sessions"]
                    for email, sess_data in sessions_data.items():
                        sess_params = []
                        for field in sess_type.fields:
                            sess_params.append(sess_data.get(field, ""))
                        sessions[email] = sess_type(*sess_params)
        except (OSError, KeyError, TypeError, JSONDecodeError):
            pass

    def _load_legacy_and_delete(self):
        try:
            with open(self._legacy_filename, "rt") as fp:
                for line in fp.readlines():
                    parts = line.split(" ")
                    if len(parts) == 5:
                        self.put(parts[0], YggdrasilAuthSession(parts[4], parts[2], parts[3], parts[1]))
            os.remove(self._legacy_filename)
        except OSError:
            pass

    def save(self):
        with open(self._filename, "wt") as fp:
            data = {}
            for typ, sessions in self._sessions.items():
                if typ not in self.types:
                    continue
                sess_type = self.types[typ]
                sessions_data = {}
                data[typ] = {"sessions": sessions_data}
                for email, sess in sessions.items():
                    sess_data = sessions_data[email] = {}
                    for field in sess_type.fields:
                        sess_data[field] = getattr(sess, field)
            json.dump(data, fp, indent=2)

    def get(self, email_or_username: str, sess_type: Type[AuthSession]) -> Optional[AuthSession]:
        sessions = self._sessions.get(sess_type.type)
        return None if sessions is None else sessions.get(email_or_username)

    def put(self, email_or_username: str, sess: AuthSession):
        sessions = self._sessions.get(sess.type)
        if sessions is None:
            if sess.type not in self.types:
                raise ValueError("Given session's type is not supported.")
            sessions = self._sessions[sess.type] = {}
        sessions[email_or_username] = sess

    def remove(self, email_or_username: str, sess_type: Type[AuthSession]) -> Optional[AuthSession]:
        sessions = self._sessions.get(sess_type.type)
        if sessions is not None:
            session = sessions.get(email_or_username)
            if session is not None:
                del sessions[email_or_username]
                return session


class Util:

    @staticmethod
    def json_request(url: str, method: str, *,
                     data: Optional[bytes] = None,
                     headers: Optional[dict] = None,
                     ignore_error: bool = False,
                     timeout: Optional[int] = None) -> Tuple[int, dict]:

        url_parsed = url_parse.urlparse(url)
        conn_type = {"http": HTTPConnection, "https": HTTPSConnection}.get(url_parsed.scheme)
        if conn_type is None:
            raise JsonRequestError(JsonRequestError.INVALID_URL_SCHEME, url_parsed.scheme)
        conn = conn_type(url_parsed.netloc, timeout=timeout)
        if headers is None:
            headers = {}
        if "Accept" not in headers:
            headers["Accept"] = "application/json"
        headers["Connection"] = "close"

        try:
            conn.request(method, url, data, headers)
            res = conn.getresponse()
            try:
                return res.status, json.load(res)
            except JSONDecodeError:
                if ignore_error:
                    return res.status, {}
                else:
                    raise JsonRequestError(JsonRequestError.INVALID_RESPONSE_NOT_JSON, res.status)
        except OSError:
            raise JsonRequestError(JsonRequestError.SOCKET_ERROR)
        finally:
            conn.close()

    @classmethod
    def json_simple_request(cls, url: str, *, ignore_error: bool = False, timeout: Optional[int] = None) -> dict:
        return cls.json_request(url, "GET", ignore_error=ignore_error, timeout=timeout)[1]

    @classmethod
    def merge_dict(cls, dst: dict, other: dict):
        """ Merge the 'other' dict into the 'dst' dict. For every key/value in 'other', if the key is present in 'dst'
        it does nothing. Unless if the value in both dict are also dict, in this case the merge is recursive. If the
        value in both dict are list, the 'dst' list is extended (.extend()) with the one of 'other'. """
        for k, v in other.items():
            if k in dst:
                if isinstance(dst[k], dict) and isinstance(other[k], dict):
                    cls.merge_dict(dst[k], other[k])
                elif isinstance(dst[k], list) and isinstance(other[k], list):
                    dst[k].extend(other[k])
            else:
                dst[k] = other[k]

    @staticmethod
    def get_minecraft_dir() -> str:
        pf = sys.platform
        home = path.expanduser("~")
        if pf.startswith("freebsd") or pf.startswith("linux") or pf.startswith("aix") or pf.startswith("cygwin"):
            return path.join(home, ".minecraft")
        elif pf == "win32":
            return path.join(home, "AppData", "Roaming", ".minecraft")
        elif pf == "darwin":
            return path.join(home, "Library", "Application Support", "minecraft")


class DownloadEntry:

    __slots__ = "url", "size", "sha1", "dst", "name"

    def __init__(self, url: str, dst: str, *, size: Optional[int] = None, sha1: Optional[str] = None, name: Optional[str] = None):
        self.url = url
        self.dst = dst
        self.size = size
        self.sha1 = sha1
        self.name = url if name is None else name

    @classmethod
    def from_meta(cls, info: dict, dst: str, *, name: Optional[str] = None) -> 'DownloadEntry':
        return DownloadEntry(info["url"], dst, size=info["size"], sha1=info["sha1"], name=name)


class DownloadList:

    __slots__ = "entries", "callbacks", "count", "size"

    def __init__(self):
        self.entries: Dict[str, List[DownloadEntry]] = {}
        self.callbacks: List[Callable[[], None]] = []
        self.count = 0
        self.size = 0

    def append(self, entry: DownloadEntry):
        url_parsed = url_parse.urlparse(entry.url)
        if url_parsed.scheme not in ("http", "https"):
            raise ValueError("Illegal URL scheme for HTTP connection.")
        host_key = "{}{}".format(int(url_parsed.scheme == "https"), url_parsed.netloc)
        entries = self.entries.get(host_key)
        if entries is None:
            self.entries[host_key] = entries = []
        entries.append(entry)
        self.count += 1
        if entry.size is not None:
            self.size += entry.size

    def add_callback(self, callback: Callable[[], None]):
        self.callbacks.append(callback)


class DownloadEntryProgress:
    __slots__ = "name", "size", "total"
    def __init__(self):
        self.name = ""
        self.size = 0
        self.total = 0


class DownloadProgress:
    __slots__ = "entries", "size", "total"
    def __init__(self, total: int):
        self.entries: List[DownloadEntryProgress] = []
        self.size: int = 0
        self.total = total


class BaseError(Exception):
    def __init__(self, code: str, *args):
        super().__init__(code, args)
        self.code = code


class JsonRequestError(BaseError):
    INVALID_URL_SCHEME = "invalid_url_scheme"
    INVALID_RESPONSE_NOT_JSON = "invalid_response_not_json"
    SOCKET_ERROR = "socket_error"


class AuthError(BaseError):
    YGGDRASIL = "yggdrasil"
    MICROSOFT = "microsoft"
    MICROSOFT_INCONSISTENT_USER_HASH = "microsoft.inconsistent_user_hash"
    MICROSOFT_DOES_NOT_OWN_MINECRAFT = "microsoft.does_not_own_minecraft"
    MICROSOFT_OUTDATED_TOKEN = "microsoft.outdated_token"


class VersionError(BaseError):
    NOT_FOUND = "not_found"
    JAR_NOT_FOUND = "jar_not_found"


if __name__ == '__main__':

    def cli_start():

        ctx = Context()
        ver = Version(ctx, "1.16.5")

    cli_start()