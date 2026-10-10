"""可选扩展使用的 Gitea/Forgejo HTTP API 适配器。"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from gitwarden.db import Store
from gitwarden.i18n import msg as message


class PlatformError(Exception):
    """平台操作失败；status 为服务端 HTTP 状态，本地错误为 None。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class IncompleteListing(PlatformError):
    """无法确认平台仓库列表完整。"""


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    default_port = 443 if parts.scheme.lower() == "https" else 80
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or default_port


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            same_origin = _origin(req.full_url) == _origin(newurl)
        except ValueError:
            same_origin = False
        if not same_origin:
            path = urlsplit(req.full_url).path or "/"
            raise PlatformError(
                message("platform.cross_site_redirect", method=req.get_method(), path=path),
                status=code,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***")


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("平台地址必须是 HTTP(S) URL")
    base_url = base_url.strip()
    parts = urlsplit(base_url)
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("平台地址必须是 HTTP(S) URL，且不得内嵌凭据")
    return base_url.rstrip("/")


class PlatformClient:
    """使用标准库访问两平台共用的 API。"""

    _HARDEN_FIELDS = (
        "has_actions",
        "has_wiki",
        "has_issues",
        "has_pull_requests",
        "has_packages",
        "has_releases",
        "has_projects",
    )

    def __init__(self, base_url: str, token: str, page_size: int = 50) -> None:
        self.base_url = _validate_base_url(base_url)
        if not isinstance(token, str) or not token.strip():
            raise ValueError("平台令牌不能为空")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
            raise ValueError("分页大小必须大于 0")
        self.token = token
        self.page_size = page_size
        self._opener = build_opener(_SameOriginRedirectHandler())

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _http_error(self, method: str, path: str, status: int, body: bytes) -> PlatformError:
        detail = _redact(body.decode("utf-8", errors="replace"), self.token)[:200]
        return PlatformError(
            message("platform.http_error", method=method, path=path, status=status, detail=detail),
            status=status,
        )

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[Any, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"token {self.token}",
        }
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read()
                status = getattr(response, "status", None)
                response_headers = response.headers
        except HTTPError as exc:
            raise self._http_error(method, path, exc.code, exc.read()) from None
        except (URLError, TimeoutError, OSError) as exc:
            reason = _redact(str(exc), self.token)
            raise PlatformError(
                message("platform.request_failed", method=method, path=path, reason=reason)
            ) from None
        if not raw.strip():
            return {}, response_headers
        try:
            return json.loads(raw.decode("utf-8")), response_headers
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = _redact(raw.decode("utf-8", errors="replace"), self.token)[:200]
            raise PlatformError(
                message("platform.invalid_json", method=method, path=path, detail=detail),
                status=status,
            ) from None

    @staticmethod
    def _segment(value: str) -> str:
        return quote(str(value), safe="")

    def _repo_path(self, owner: str, name: str, suffix: str = "") -> str:
        return f"/api/v1/repos/{self._segment(owner)}/{self._segment(name)}{suffix}"

    def whoami(self) -> dict[str, Any]:
        result, _ = self._request("GET", "/api/v1/user")
        return result

    def list_all_repos(self) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        page = 1
        expected: int | None = None
        while True:
            query = urlencode({"private": "true", "page": page, "limit": self.page_size})
            result, headers = self._request("GET", f"/api/v1/repos/search?{query}")
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise IncompleteListing(message("platform.search_failed"))
            rows = result.get("data")
            if not isinstance(rows, list):
                raise IncompleteListing(message("platform.list_not_array", path="repos/search"))
            if expected is None:
                raw_total = headers.get("x-total-count")
                if raw_total is None:
                    raise IncompleteListing(message("platform.total_missing"))
                try:
                    expected = int(raw_total)
                except (TypeError, ValueError):
                    raise IncompleteListing(
                        message("platform.total_invalid", path="repos/search")
                    ) from None
            repos.extend(rows)
            if len(repos) >= expected or not rows:
                break
            page += 1
        if len(repos) != expected:
            raise IncompleteListing(
                message("platform.count_mismatch", received=len(repos), expected=expected)
            )
        return repos

    def _list_repo_pages(self, path: str) -> list[dict[str, Any]]:
        """读取组织或用户仓库的全部页面，并验证分页已经结束。"""
        repos: list[dict[str, Any]] = []
        page = 1
        expected: int | None = None
        while True:
            separator = "&" if "?" in path else "?"
            result, headers = self._request(
                "GET", f"{path}{separator}" + urlencode({"page": page, "limit": self.page_size})
            )
            if not isinstance(result, list):
                raise IncompleteListing(message("platform.list_not_array", path=path))
            if expected is None:
                raw_total = headers.get("x-total-count")
                if raw_total is not None:
                    try:
                        expected = int(raw_total)
                    except (TypeError, ValueError):
                        raise IncompleteListing(
                            message("platform.total_invalid", path=path)
                        ) from None
                    if expected < 0:
                        raise IncompleteListing(message("platform.total_invalid", path=path))
            repos.extend(result)
            if expected is not None:
                if len(repos) > expected:
                    raise IncompleteListing(message("platform.list_overflow", path=path))
                if len(repos) == expected:
                    break
                if not result:
                    raise IncompleteListing(message("platform.list_incomplete", path=path))
            elif not result or len(result) < self.page_size:
                break
            page += 1
        if expected is not None and len(repos) != expected:
            raise IncompleteListing(message("platform.list_incomplete", path=path))
        return repos

    def list_owner_repos(self, owner: str) -> list[dict[str, Any]]:
        """先按组织读取；组织不存在时按用户读取。"""
        organization_path = f"/api/v1/orgs/{self._segment(owner)}/repos"
        try:
            return self._list_repo_pages(organization_path)
        except PlatformError as exc:
            if exc.status != 404:
                raise
        user_path = f"/api/v1/users/{self._segment(owner)}/repos"
        return self._list_repo_pages(user_path)

    def list_repos_for_owners(self, owners: list[str]) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for owner in owners:
            if not isinstance(owner, str) or not owner.strip():
                raise IncompleteListing(message("platform.owner_invalid"))
            for repo in self.list_owner_repos(owner):
                repo_id = repo.get("id") if isinstance(repo, dict) else None
                if repo_id in seen:
                    continue
                if repo_id is not None:
                    seen.add(repo_id)
                repos.append(repo)
        return repos

    def get_repo(self, owner: str, name: str) -> dict[str, Any]:
        result, _ = self._request("GET", self._repo_path(owner, name))
        return result

    def create_repo(self, owner: str, name: str, private: bool = True) -> dict[str, Any]:
        me = self.whoami()
        login = me.get("login") or me.get("username")
        if not login:
            raise PlatformError(message("platform.no_account_name"))
        if owner == login:
            path = "/api/v1/user/repos"
        else:
            path = f"/api/v1/orgs/{self._segment(owner)}/repos"
        result, _ = self._request("POST", path, {"name": name, "private": bool(private)})
        return result

    def harden(self, owner: str, name: str) -> dict[str, Any]:
        payload = {field: False for field in self._HARDEN_FIELDS}
        payload["private"] = True
        path = self._repo_path(owner, name)
        self._request("PATCH", path, payload)
        repo = self.get_repo(owner, name)
        failed = []
        if repo.get("private") is not True:
            failed.append("private")
        failed.extend(field for field in self._HARDEN_FIELDS if repo.get(field))
        if failed:
            raise PlatformError(
                message("platform.readback_failed", path=path, failed=", ".join(failed))
            )
        return repo

    def protect_tags(
        self, owner: str, name: str, pattern: str, whitelist_user: str
    ) -> dict[str, Any]:
        path = self._repo_path(owner, name, "/tag_protections")
        result, _ = self._request(
            "POST",
            path,
            {"name_pattern": pattern, "whitelist_usernames": [whitelist_user]},
        )
        return result

    def _is_organization(self, owner: str) -> bool:
        path = f"/api/v1/orgs/{self._segment(owner)}"
        try:
            self._request("GET", path)
        except PlatformError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def _list_hooks(self, path: str) -> list[dict[str, Any]]:
        hooks = []
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            rows, _ = self._request(
                "GET", f"{path}{separator}" + urlencode({"page": page, "limit": self.page_size})
            )
            if not rows:
                return hooks
            hooks.extend(rows)
            page += 1

    def list_hooks(self, owner: str, name: str) -> dict[str, list[Any]]:
        repository_path = self._repo_path(owner, name, "/hooks")
        repository = self._list_hooks(repository_path)
        organization: list[Any] = []
        if self._is_organization(owner):
            organization_path = f"/api/v1/orgs/{self._segment(owner)}/hooks"
            organization = self._list_hooks(organization_path)

        system: list[Any] = []
        if self.whoami().get("is_admin"):
            try:
                system = self._list_hooks("/api/v1/admin/hooks?type=all")
            except PlatformError as exc:
                if exc.status not in {401, 403}:
                    raise
        return {"repository": repository, "organization": organization, "system": system}


def client_from_store(store: Store, access_id: int) -> PlatformClient:
    access = store.get_access(access_id)
    if not access:
        raise ValueError(message("error.access_missing"))
    instance = store.get_instance(access["instance_id"])
    if not instance:
        raise ValueError(message("error.instance_missing"))
    cred_id = access.get("cred_id")
    cred = store.get_cred(cred_id) if cred_id is not None else None
    if (
        not cred
        or cred.get("kind") != "https"
        or not isinstance(cred.get("username"), str)
        or not cred["username"].strip()
        or not isinstance(cred.get("secret"), str)
        or not cred["secret"].strip()
    ):
        raise ValueError(message("platform.access_credential_invalid"))
    return PlatformClient(instance["base_url"], cred["secret"])
