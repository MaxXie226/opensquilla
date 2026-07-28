"""Control UI route factory — serves embedded HTML console with SPA fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path, PurePosixPath

import anyio
import structlog
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from opensquilla import __version__
from opensquilla._build_info import BUILD_UI_MODE
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.control_ui_assets import (
    ControlUiArtifactManifest,
    ControlUiAssetResolver,
    ControlUiAssets,
)

log = structlog.get_logger(__name__)

# Conservative max-age for static assets. 30 days is long enough that hot
# clients save roundtrips but short enough that any deploy without a version
# bump still becomes visible within a release cycle. Templates already append
# ?v={{ version }} to every asset URL so cache invalidation on actual code
# change is immediate — this header only saves repeat hits for unchanged
# bytes within the 30-day window.
#
# Skip when OPENSQUILLA_STATIC_NO_CACHE is set (debugging / forced refresh).
# Skip on non-200 responses so 206 Range and 304 conditional reuse stay
# untouched.
_STATIC_CACHE_CONTROL = "public, max-age=2592000"

# Content-Types for the static assets the Control UI ships, keyed by lowercase
# extension. Starlette derives Content-Type from ``mimetypes.guess_type``, which
# seeds itself from the host OS MIME database. On Windows machines whose
# ``HKEY_CLASSES_ROOT\\.js`` registry entry has been rewritten to ``text/plain``
# (a common side effect of some third-party installers), every ``.js`` asset is
# served as ``text/plain``; Chromium's strict MIME check then refuses to execute
# the Vite ``<script type="module">`` entry and the console renders blank even
# though gateway boot and health checks pass. We therefore pin the Content-Type
# for these extensions at the serving boundary rather than trusting the
# environment. Extensions not listed here keep flowing through Starlette's own
# guess.
#
# For most extensions the pinned value equals what a correctly configured host
# already produces. A few are deliberately more correct than a bare host would
# emit: ``.map``/``.woff``/``.woff2``/``.ttf`` are absent from CPython's built-in
# table (so a host with no OS MIME registry falls back to ``text/plain``), and
# ``.ico`` resolves to ``image/vnd.microsoft.icon`` in the stdlib. Pinning
# normalizes these to their standard types on every host. All values are
# browser-accepted, so the change is safe on clean machines.
_PINNED_CONTENT_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class _CachedStaticFiles(StaticFiles):
    """StaticFiles subclass that attaches Cache-Control to 200 responses and
    pins Content-Type for known code assets.

    Source maps (.map) are excluded from long-term caching since they are
    only used for debugging and should not be aggressively cached.

    Content-Type is forced from ``_PINNED_CONTENT_TYPES`` for extensions the
    console depends on, so a host with a corrupt MIME database cannot mislabel
    JavaScript (which browsers refuse to execute under strict MIME checking).
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code != 200:
            return response
        if not os.environ.get("OPENSQUILLA_STATIC_NO_CACHE"):
            # Skip cache-control for source maps — debug files should not be
            # cached aggressively (or served in production at all).
            if not path.endswith(".map"):
                response.headers.setdefault("Cache-Control", _STATIC_CACHE_CONTROL)
        pinned = _PINNED_CONTENT_TYPES.get(PurePosixPath(path).suffix.lower())
        if pinned is not None:
            if pinned.startswith("text/"):
                # Match Starlette's charset convention for text/* types so
                # headers on healthy hosts stay byte-identical.
                pinned = f"{pinned}; charset=utf-8"
            response.headers["content-type"] = pinned
        return response


class _ManifestStaticFiles(_CachedStaticFiles):
    """Serve only files admitted by a validated external artifact manifest."""

    def __init__(
        self,
        *,
        directory: str,
        manifest: ControlUiArtifactManifest,
    ) -> None:
        super().__init__(directory=directory, check_dir=False)
        self._directory = Path(directory)
        self._expected = {
            entry.path: entry for entry in manifest.files if entry.path != "index.html"
        }
        self._integrity_cache: dict[str, tuple[int, int, int, int]] = {}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    async def get_response(self, path: str, scope):  # type: ignore[override]
        normalized = PurePosixPath(path.replace(os.sep, "/")).as_posix().lstrip("/")
        expected = self._expected.get(normalized)
        if expected is None:
            raise HTTPException(status_code=404)
        candidate = self._directory.joinpath(*PurePosixPath(normalized).parts)
        if candidate.is_symlink():
            raise HTTPException(status_code=404)
        try:
            file_stat = candidate.stat()
        except OSError as error:
            raise HTTPException(status_code=404) from error
        cache_key = (
            int(getattr(file_stat, "st_ino", 0)),
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        )
        if file_stat.st_size != expected.size:
            raise HTTPException(status_code=404)
        if self._integrity_cache.get(normalized) != cache_key:
            try:
                digest = await anyio.to_thread.run_sync(self._sha256, candidate)
            except OSError as error:
                raise HTTPException(status_code=404) from error
            if digest != expected.sha256:
                raise HTTPException(status_code=404)
            self._integrity_cache[normalized] = cache_key
        return await super().get_response(path, scope)


class _ControlUiStaticFiles(_CachedStaticFiles):
    """Keep one public static mount while allowing the Vite dist root to vary."""

    def __init__(self, assets: ControlUiAssets) -> None:
        super().__init__(
            directory=str(assets.static_root) if assets.static_root is not None else None,
            check_dir=False,
        )
        self._dist: _CachedStaticFiles | None = None
        if assets.dist_root is not None:
            if assets.mode == "external" and assets.manifest is not None:
                self._dist = _ManifestStaticFiles(
                    directory=str(assets.dist_root),
                    manifest=assets.manifest,
                )
            else:
                self._dist = _CachedStaticFiles(
                    directory=str(assets.dist_root),
                    check_dir=False,
                )

    async def get_response(self, path: str, scope):  # type: ignore[override]
        normalized = path.replace(os.sep, "/").lstrip("/")
        if normalized.startswith("dist/"):
            if self._dist is None:
                raise HTTPException(status_code=404)
            return await self._dist.get_response(normalized.removeprefix("dist/"), scope)
        if normalized == "dist":
            if self._dist is None:
                raise HTTPException(status_code=404)
            return await self._dist.get_response("", scope)
        return await super().get_response(path, scope)


_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_DIST_DIR = _STATIC_DIR / "dist"

_TEMPLATE_VERSION_SUFFIX = str(int(time.time()))

_jinja_env = None


def _get_jinja_env(template_root: Path | None = None):
    global _jinja_env
    root = template_root or _TEMPLATE_DIR
    if _jinja_env is None or root != _TEMPLATE_DIR:
        import jinja2

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(root)),
            autoescape=True,
        )
        env.filters["tojson"] = lambda v, **kw: json.dumps(v)
        if root != _TEMPLATE_DIR:
            return env
        _jinja_env = env
    return _jinja_env


def _request_ws_url(request: Request, config: GatewayConfig) -> str:
    """Build the browser-facing websocket URL from the current request."""
    host = request.headers.get("host") or f"{config.host}:{config.port}"
    if config.host in {"0.0.0.0", "::"} and host == "testserver":
        host = f"127.0.0.1:{config.port}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    ws_scheme = "wss" if scheme == "https" else "ws"
    return f"{ws_scheme}://{host}/ws"


_SUPPORTED_LOCALES = ("en", "zh-Hans", "ja", "fr", "de", "es")


def _locale_from_tag(tag: str) -> str | None:
    """Map a single BCP-47 Accept-Language tag to a supported locale, else None."""
    t = tag.strip().lower()
    if not t:
        return None
    if t.startswith("zh"):
        return "zh-Hans"
    for code in ("ja", "fr", "de", "es", "en"):
        if t == code or t.startswith(code + "-"):
            return code
    return None


def _resolve_locale(config: GatewayConfig, request: Request) -> str:
    """Resolve the first-paint locale rendered into <html lang> and #opensquilla-data.

    Honors the configured default. Only when that default is the baseline 'en'
    do we sniff Accept-Language (the first supported tag wins), so an operator
    who explicitly pins a default is never overridden. The browser's saved
    localStorage choice and the in-app switcher always win client-side.
    """
    default = getattr(config.control_ui, "default_locale", "en")
    if default in _SUPPORTED_LOCALES and default != "en":
        return default
    accept = request.headers.get("accept-language", "") or ""
    for part in accept.split(","):
        code = _locale_from_tag(part.split(";", 1)[0])
        if code:
            return code
    return "en"


def _update_payload(config: GatewayConfig) -> dict | None:
    """Cached update-availability info for the bootstrap context.

    Read-only and non-blocking: the actual GitHub check runs in a background
    thread (see start_background_update_check). Returns a small dict only when a
    newer release is known, so the front end can treat "presence" as "show the
    notice"; returns None otherwise.
    """
    try:
        from opensquilla.observability.update_check import get_cached_update_info

        info = get_cached_update_info(config=config, version=__version__)
    except Exception:  # pragma: no cover - defensive, never break page render
        return None
    if info is None or not info.update_available:
        return None
    return info.to_public_dict()


def _link_token_from_request(request: Request) -> str:
    """Return the optional operator token carried by a Control UI deep link."""
    try:
        token = request.query_params.get("token") or ""
    except Exception:
        return ""
    return str(token).strip()


def _build_bootstrap_context(config: GatewayConfig, request: Request) -> dict:
    """Build the template context for bootstrap config injection."""
    return {
        "version": f"{__version__}+{_TEMPLATE_VERSION_SUFFIX}",
        "ws_url": _request_ws_url(request, config),
        "auth_mode": config.auth.mode,
        "base_path": config.control_ui.base_path,
        "config_path": config.config_path or "",
        "locale": _resolve_locale(config, request),
        "update": _update_payload(config),
        "link_token": _link_token_from_request(request),
        "features": {
            "diagnostics": config.diagnostics_enabled,
        },
    }


def _vite_asset_url(raw_url: str, base_path: str) -> str:
    """Normalize a Vite asset URL to the configured Control UI base path."""
    if not raw_url:
        return ""
    if raw_url.startswith(("http://", "https://", "//")):
        return raw_url

    base = base_path.rstrip("/") or ""
    asset_prefix = f"{base}/static/dist/"
    if raw_url.startswith(asset_prefix):
        return raw_url

    marker = "/static/dist/"
    if raw_url.startswith("/") and marker in raw_url:
        return f"{asset_prefix}{raw_url.split(marker, 1)[1]}"
    if raw_url.startswith("./"):
        return f"{asset_prefix}{raw_url[2:]}"
    if raw_url.startswith("assets/"):
        return f"{asset_prefix}{raw_url}"
    return raw_url


def _read_vite_assets(
    base_path: str,
    dist_root: Path | None = None,
) -> tuple[str, list[str]]:
    """Read the Vite-generated index.html and extract the main JS module and
    every entry stylesheet.

    Returns (js_url, css_urls) relative to the static directory. Vite emits more
    than one entry stylesheet (e.g. a shared Icon chunk plus the main bundle),
    and their order in index.html is not stable — extracting only the first
    drops the main bundle and renders the page unstyled, so all of them must be
    injected.
    """
    dist_index = (dist_root or _DIST_DIR) / "index.html"
    if not dist_index.exists():
        # The template turns this into an actionable diagnostic instead of a
        # blank Vue mount point. Standard distributions cannot reach this state
        # because the Hatch build hook validates the artifact fail-closed.
        return ("", [])

    html = dist_index.read_text(encoding="utf-8")

    # Extract the main JS module
    js_match = re.search(r'<script type="module"[^>]*src="([^"]+)"', html)
    js_url = _vite_asset_url(js_match.group(1) if js_match else "", base_path)

    # Extract every stylesheet link, preserving document (cascade) order.
    css_urls = [
        _vite_asset_url(href, base_path)
        for href in re.findall(r'<link rel="stylesheet"[^>]*href="([^"]+)"', html)
    ]

    return (js_url, css_urls)


def resolve_control_ui_assets(config: GatewayConfig) -> ControlUiAssets:
    """Resolve assets using the compatibility globals existing tests may patch."""

    return ControlUiAssetResolver(
        config,
        embedded_static_root=_STATIC_DIR,
        embedded_dist_root=_DIST_DIR,
        template_root=_TEMPLATE_DIR,
        allow_embedded=BUILD_UI_MODE != "headless",
    ).resolve()


def _missing_assets_detail(
    assets: ControlUiAssets,
    requested_mode: str,
) -> str:
    reason = assets.reason or "unavailable"
    if reason == "explicit_none":
        return (
            "The Gateway is intentionally running without a product Control UI. "
            "Use the CLI, a compatible client, or the public RPC interfaces."
        )
    if reason.startswith("external:"):
        return (
            "The configured external Control UI bundle was rejected by its path "
            "or manifest validation. The Gateway core remains available."
        )
    if reason == "build:headless" or (
        requested_mode == "auto" and reason == "embedded:asset_directory_missing"
    ):
        return (
            "The public Gateway runtime is running headless as expected. Use "
            "the CLI, a compatible client, or configure an explicit external "
            "Control UI artifact."
        )
    return (
        "The selected embedded Control UI artifact is missing or failed "
        "validation. The Gateway core remains available; use a compatible "
        "client or provide a verified external/embedded artifact."
    )


def _fallback_index_html(assets: ControlUiAssets, requested_mode: str) -> str:
    reason = assets.reason or "unavailable"
    if reason == "explicit_none":
        title = "Gateway is running without a Control UI"
        detail = "Use a compatible client, the CLI, or the Gateway RPC interfaces."
    elif reason.startswith("external:"):
        title = "Configured Control UI assets were rejected"
        detail = "The Gateway core is healthy; verify the external bundle manifest."
    elif reason == "build:headless" or (
        requested_mode == "auto" and reason == "embedded:asset_directory_missing"
    ):
        title = "Gateway is running in headless mode"
        detail = "Use a compatible client, the CLI, or the Gateway RPC interfaces."
    else:
        title = "Control UI assets are unavailable"
        detail = "The Gateway core is healthy; verify the selected Control UI artifact."
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body><main role=\"alert\"><h1>{title}</h1>"
        f"<p>{detail}</p></main></body></html>"
    )


def create_control_ui_routes(
    config: GatewayConfig,
    assets: ControlUiAssets | None = None,
) -> list[Route | Mount]:
    """Create Control UI routes from a resolved, immutable asset description."""

    if not config.control_ui.enabled:
        return []

    resolved = assets or resolve_control_ui_assets(config)
    if not resolved.available:
        detail = _missing_assets_detail(resolved, config.control_ui.assets_mode)
        if resolved.reason == "explicit_none":
            log.info(
                "control_ui.assets_disabled",
                assets_mode="none",
                detail=detail,
            )
        elif (
            config.control_ui.assets_mode == "auto"
            and resolved.reason
            in {"build:headless", "embedded:asset_directory_missing"}
        ):
            log.info(
                "control_ui.headless",
                assets_mode="none",
                detail=detail,
            )
        else:
            # The served page already shows an actionable notice, but headless
            # operators only watch logs — surface the same guidance at startup.
            log.warning(
                "control_ui.webui_assets_missing",
                assets_mode=config.control_ui.assets_mode,
                reason=resolved.reason,
                detail=detail,
                # Preserve the existing diagnostic field for embedded/source
                # installs without exposing a configured external absolute path.
                dist_dir=(
                    str(_DIST_DIR)
                    if config.control_ui.assets_mode in {"auto", "embedded"}
                    else ""
                ),
            )

    base = config.control_ui.base_path
    try:
        template = _get_jinja_env(resolved.template_root).get_template("index.html")
    except Exception:
        template = None
        log.warning(
            "control_ui.template_missing",
            detail="The neutral Control UI bootstrap template is unavailable.",
        )
    external_entry = (
        (
            _vite_asset_url(resolved.manifest.entry_scripts[0], base),
            [
                _vite_asset_url(relative, base)
                for relative in resolved.manifest.entry_styles
            ],
        )
        if resolved.mode == "external"
        and resolved.manifest is not None
        and resolved.manifest.entry_scripts
        else None
    )

    async def serve_index(request: Request) -> HTMLResponse:
        ctx = _build_bootstrap_context(config, request)
        # Re-read the selected entrypoint on each request so an embedded source
        # build is picked up without a restart. External entry URLs are frozen
        # after validation and their files stay behind the manifest allowlist.
        if external_entry is not None:
            live_js, live_css_urls = external_entry
        elif resolved.dist_root is not None:
            live_js, live_css_urls = _read_vite_assets(base, resolved.dist_root)
        else:
            live_js, live_css_urls = ("", [])
        ctx["vite_js_url"] = live_js
        ctx["vite_css_urls"] = live_css_urls
        ctx["webui_artifact_missing"] = not live_js
        ctx["control_ui_assets_mode"] = resolved.mode
        ctx["control_ui_assets_reason"] = resolved.reason or ""
        ctx["control_ui_requested_mode"] = config.control_ui.assets_mode
        # Back-compat single URL (first) for any consumer expecting one.
        ctx["vite_css_url"] = live_css_urls[0] if live_css_urls else ""
        html = (
            template.render(**ctx)
            if template is not None
            else _fallback_index_html(resolved, config.control_ui.assets_mode)
        )
        response = HTMLResponse(html)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    routes: list[Route | Mount] = [
        Mount(
            f"{base}/static",
            app=_ControlUiStaticFiles(resolved),
            name="control_ui_static",
        ),
        Route(f"{base}/{{path:path}}", serve_index, methods=["GET"]),
        Route(f"{base}/", serve_index, methods=["GET"]),
    ]
    return routes
