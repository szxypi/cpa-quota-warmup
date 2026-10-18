#!/usr/bin/env python3
"""Exercise the compiled cpa-quota-warmup plugin through CPA's public C ABI.

This test does not wire up a real host (host_ctx/call are left NULL), so any
host.* callback the plugin makes (host.auth.list, host.log) fails immediately
inside the plugin's own cgo bridge. The plugin is expected to degrade
gracefully in that situation -- this is exactly what it does whenever the
management "status"/"run" routes are hit outside of a live CPA process too.
"""

import base64
import ctypes
import json
import pathlib
import sys
import tempfile


class Buffer(ctypes.Structure):
    _fields_ = [("ptr", ctypes.c_void_p), ("len", ctypes.c_size_t)]


PluginCall = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.POINTER(ctypes.c_uint8),
    ctypes.c_size_t,
    ctypes.POINTER(Buffer),
)
PluginFree = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_size_t)
PluginShutdown = ctypes.CFUNCTYPE(None)


class HostAPI(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("host_ctx", ctypes.c_void_p),
        ("call", ctypes.c_void_p),
        ("free_buffer", ctypes.c_void_p),
    ]


class PluginAPI(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("call", PluginCall),
        ("free_buffer", PluginFree),
        ("shutdown", PluginShutdown),
    ]


def invoke(plugin: PluginAPI, method: str, request: dict) -> dict:
    encoded = json.dumps(request, separators=(",", ":")).encode()
    request_buffer = (ctypes.c_uint8 * max(1, len(encoded))).from_buffer_copy(
        encoded or b"\0"
    )
    response = Buffer()
    rc = plugin.call(
        method.encode(), request_buffer, len(encoded), ctypes.byref(response)
    )
    try:
        raw = ctypes.string_at(response.ptr, response.len) if response.ptr else b""
    finally:
        if response.ptr:
            plugin.free_buffer(response.ptr, response.len)
    decoded = json.loads(raw) if raw else {}
    if rc != 0 or not decoded.get("ok"):
        raise RuntimeError(f"{method} failed: rc={rc}, response={decoded}")
    return decoded["result"]


def register_request(yaml_text: str) -> dict:
    return {"config_yaml": base64.b64encode(yaml_text.encode()).decode()}


def management_request(path: str, query: dict | None = None) -> dict:
    return {
        "Method": "GET",
        "Path": path,
        "Headers": {},
        "Query": {key: [value] for key, value in (query or {}).items()},
        "Body": None,
    }


def decode_management(result: dict) -> tuple[int, dict, bytes]:
    body = base64.b64decode(result.get("Body") or b"")
    headers = {
        key.lower(): values[0] for key, values in (result.get("Headers") or {}).items()
    }
    return result.get("StatusCode", 0), headers, body


def file_mode_config(config_file: str) -> str:
    # v0.4.0 default: file mode, just enabled + config-file (advanced still
    # applies the same way it did in v0.3.0's inline config).
    return f"""
enabled: true
config-file: "{config_file}"
advanced:
  message: "hi"
"""


# V3_INLINE_CONFIG exercises the v0.3.0 top-level time/model/accounts shape,
# which must still parse (and be detected as v3InlineMode, not file mode)
# unchanged.
V3_INLINE_CONFIG = """
time: "05:30"
model: "auto"
accounts: ["antigravity-*"]
advanced:
  message: "hi"
"""

# LEGACY_CONFIG exercises the pre-v0.3.0 top-level shape, which must still
# parse (and be detected as legacy) unchanged.
LEGACY_CONFIG = """
timezone: "UTC"
message: "hi"
providers:
  antigravity: { model: "gemini-3.7-flash-high" }
"""


def main() -> None:
    library_path = pathlib.Path(sys.argv[1]).resolve()
    library = ctypes.CDLL(str(library_path))
    init = library.cliproxy_plugin_init
    init.argtypes = [ctypes.POINTER(HostAPI), ctypes.POINTER(PluginAPI)]
    init.restype = ctypes.c_int

    # host_ctx/call are intentionally left NULL: every host.* callback this
    # plugin makes must fail safely rather than crash or hang.
    host = HostAPI(abi_version=1)
    plugin = PluginAPI()
    if init(ctypes.byref(host), ctypes.byref(plugin)) != 0:
        raise RuntimeError("cliproxy_plugin_init failed")

    tmp_dir = tempfile.mkdtemp(prefix="cpa-quota-warmup-abi-")
    warmup_file = str(pathlib.Path(tmp_dir) / "quota-warmup.yaml")

    try:
        registration = invoke(plugin, "plugin.register", register_request(file_mode_config(warmup_file)))
        capabilities = registration["capabilities"]
        if not capabilities.get("usage_plugin") or not capabilities.get("management_api"):
            raise AssertionError(f"unexpected capabilities: {capabilities}")
        for field in ("Name", "Version", "Author", "GitHubRepository", "Logo"):
            if not registration["metadata"].get(field):
                raise AssertionError(f"required metadata field missing: {field}")
        plugin_id = registration["metadata"]["Name"]
        if plugin_id != "cpa-quota-warmup":
            raise AssertionError(f"unexpected plugin metadata name: {plugin_id}")
        # v0.4.0 reduced ConfigFields to exactly these 3 entries (enabled/
        # config-file/advanced) -- see main.go's registrationPayload.
        field_names = [f["Name"] for f in registration["metadata"]["ConfigFields"]]
        if field_names != ["enabled", "config-file", "advanced"]:
            raise AssertionError(f"unexpected ConfigFields names: {field_names}")

        mgmt_routes = invoke(
            plugin,
            "management.register",
            {
                "Plugin": registration["metadata"],
                "BasePath": "/v0/management",
                "ResourceBasePath": f"/v0/resource/plugins/{plugin_id}",
            },
        )
        resources = mgmt_routes["Resources"]
        paths = [item["Path"] for item in resources]
        # v0.5.0 appended the "编辑配置文件" (edit config file) online editor's
        # two routes at the end.
        if paths != ["/panel", "/status", "/run", "/set", "/config-yaml", "/config-yaml/save"]:
            raise AssertionError(f"unexpected resource paths: {paths}")
        if not resources[0]["Menu"] or any(r.get("Menu") for r in resources[1:]):
            raise AssertionError("only the panel route may carry a menu label")

        # The panel page must render as a self-contained HTML document that
        # follows the Management Center's own language (and theme) choice.
        panel_status, panel_headers, panel_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/panel"),
            )
        )
        if panel_status != 200 or "text/html" not in panel_headers.get("content-type", ""):
            raise AssertionError(f"panel response: status={panel_status}, headers={panel_headers}")
        if b"cli-proxy-language" not in panel_body:
            raise AssertionError("panel HTML does not read the cli-proxy-language localStorage key")
        if b"cli-proxy-theme" not in panel_body:
            raise AssertionError("panel HTML does not follow the cli-proxy-theme localStorage key")
        if b"{{" in panel_body:
            raise AssertionError("panel HTML has an unreplaced {{...}} template placeholder")
        # v0.2.1: static labels must carry data-i18n so a client whose actual
        # cli-proxy-language disagrees with the server's Accept-Language
        # guess (e.g. a headless browser) can still re-translate them.
        if b'data-i18n="ui_page_title"' not in panel_body:
            raise AssertionError("panel HTML title/h1 is missing its data-i18n attribute")
        if b"data-i18n-placeholder=" not in panel_body:
            raise AssertionError("panel HTML input placeholder is missing its data-i18n-placeholder attribute")

        # usage.handle must never error, even for an arbitrary/unrelated record.
        usage_record = {
            "Provider": "antigravity",
            "Model": "gemini-3.7-flash-high",
            "SessionID": "header:cpa-quota-warmup-deadbeef",
            "AuthID": "auth-1",
            "RequestedAt": "2026-09-13T05:30:00Z",
            "Failed": False,
            "Detail": {"InputTokens": 5, "OutputTokens": 3},
        }
        invoke(plugin, "usage.handle", usage_record)

        status, headers, body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/status", {"lang": "ru"}),
            )
        )
        if status != 200 or "application/json" not in headers.get("content-type", ""):
            raise AssertionError(f"status response: status={status}, headers={headers}")
        payload = json.loads(body)
        if payload["config"]["message"] != "hi":
            raise AssertionError(f"unexpected config summary: {payload['config']}")
        if payload.get("lang") != "ru":
            raise AssertionError(f"expected ?lang=ru to be honored, got lang={payload.get('lang')!r}")
        if not payload.get("auths_error"):
            # host.auth.list has no real host behind it in this test, so the
            # status route is expected to surface that as auths_error rather
            # than silently reporting zero auths.
            raise AssertionError(f"expected auths_error with no host wired up: {payload}")

        # v0.4.0: file mode's mode/config_file/legacy_mode must round-trip
        # into the status JSON, and available_models must be present (an
        # empty list is fine -- there is no real CPA listening at the
        # configured base-url in this sandboxed test).
        cfg_summary = payload["config"]
        if cfg_summary.get("mode") != "file":
            raise AssertionError(f"unexpected config.mode: {cfg_summary}")
        if cfg_summary.get("config_file") != warmup_file:
            raise AssertionError(f"unexpected config.config_file: {cfg_summary}")
        if cfg_summary.get("legacy_mode"):
            raise AssertionError(f"expected legacy_mode=false for file mode: {cfg_summary}")
        if not cfg_summary.get("timezone_auto"):
            raise AssertionError(f"expected timezone_auto=true when advanced.timezone is unset: {cfg_summary}")
        if "available_models" not in payload or not isinstance(payload["available_models"], list):
            raise AssertionError(f"expected available_models to be a list, got {payload.get('available_models')!r}")

        # The run route degrades the same way: no host.auth.list means no
        # accounts to trigger, surfaced as a 500 with an error body rather
        # than a crash or a hang.
        run_status, _, run_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/run", {"auth": "*", "lang": "zh-TW"}),
            )
        )
        if run_status != 500 or b"error" not in run_body:
            raise AssertionError(f"run response: status={run_status}, body={run_body!r}")
        run_payload = json.loads(run_body)
        if run_payload.get("lang") != "zh-TW":
            raise AssertionError(f"expected ?lang=zh-TW to be honored on /run, got {run_payload}")

        # plugin.reconfigure must hot-swap the config without erroring, and
        # the change must be visible through the status route immediately.
        reconfigured = invoke(
            plugin, "plugin.reconfigure", register_request(file_mode_config(warmup_file).replace('"hi"', '"yo"'))
        )
        if reconfigured["metadata"]["Name"] != plugin_id:
            raise AssertionError("reconfigure returned unexpected metadata")
        status, _, body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/status"),
            )
        )
        payload = json.loads(body)
        if payload["config"]["message"] != "yo":
            raise AssertionError(f"reconfigure did not take effect: {payload['config']}")

        # File mode's /set: a missing auth is rejected with 400 before ever
        # touching quota-warmup.yaml.
        set_status, _, set_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/set", {"model": "x"}),
            )
        )
        if set_status != 400:
            raise AssertionError(f"expected 400 for a missing auth, got {set_status} body={set_body!r}")

        # With an auth given, the route is reachable and fails safely (500,
        # with a clear error) rather than crashing or hanging: there is no
        # real host.auth.list in this sandboxed test, so quota-warmup.yaml
        # was never actually loaded/generated, exactly like /run's 500 above.
        set_status, _, set_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(
                    f"/v0/resource/plugins/{plugin_id}/set",
                    {"auth": "antigravity-alice.json", "enabled": "true", "lang": "zh-TW"},
                ),
            )
        )
        if set_status != 500 or b"error" not in set_body:
            raise AssertionError(f"file-mode set response: status={set_status}, body={set_body!r}")
        set_payload = json.loads(set_body)
        if set_payload.get("lang") != "zh-TW":
            raise AssertionError(f"expected ?lang=zh-TW to be honored on /set, got {set_payload}")

        # --- v0.5.0: GET .../config-yaml and GET .../config-yaml/save -----
        # host.auth.list has no real host behind it in this sandboxed test
        # (see /run's and /set's own 500s above), so quota-warmup.yaml is
        # written directly here rather than relying on ensureFresh to
        # generate it -- this exercises the actual read/save/validate
        # contract end to end, independent of that unrelated limitation.
        initial_yaml = (
            "defaults:\n"
            "  time: \"05:30\"\n"
            "  model: auto\n"
            "accounts:\n"
            "  demo.json:  # 注释：中文测试\n"
            "    enabled: false\n"
            "    time: \"05:30\"\n"
            "    model: auto\n"
        )
        pathlib.Path(warmup_file).write_text(initial_yaml, encoding="utf-8")

        # 1. Read: exact content, a path, and a non-empty mtime round-trip.
        cfg_status, _, cfg_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/config-yaml"),
            )
        )
        if cfg_status != 200:
            raise AssertionError(f"config-yaml read: status={cfg_status}, body={cfg_body!r}")
        cfg_payload = json.loads(cfg_body)
        if cfg_payload.get("content") != initial_yaml:
            raise AssertionError(f"config-yaml read did not return the file's exact content: {cfg_payload}")
        if not cfg_payload.get("mtime"):
            raise AssertionError(f"config-yaml read did not return an mtime: {cfg_payload}")
        if cfg_payload.get("path") != warmup_file:
            raise AssertionError(f"config-yaml read returned unexpected path: {cfg_payload}")
        mtime = cfg_payload["mtime"]

        # 2. Save: a valid edit containing Chinese comments, quotes, a
        # backslash and a literal "#" (inside a quoted scalar) -- exercises
        # the base64url round trip end to end -- with the mtime just read.
        # Expect 200, a fresh mtime, and the file on disk to match exactly.
        edited_yaml = (
            "defaults:\n"
            "  time: \"05:30\"\n"
            "  model: auto\n"
            "accounts:\n"
            "  demo.json:  # 注释：中文/引号\"/反斜杠\\/# 号\n"
            "    enabled: true\n"
            "    time: \"05:30, 10:30\"\n"
            "    model: auto\n"
        )
        encoded_content = base64.urlsafe_b64encode(edited_yaml.encode("utf-8")).rstrip(b"=").decode("ascii")
        save_status, _, save_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(
                    f"/v0/resource/plugins/{plugin_id}/config-yaml/save",
                    {"content": encoded_content, "mtime": mtime},
                ),
            )
        )
        if save_status != 200:
            raise AssertionError(f"config-yaml save: status={save_status}, body={save_body!r}")
        save_payload = json.loads(save_body)
        new_mtime = save_payload.get("mtime")
        if not new_mtime:
            raise AssertionError(f"config-yaml save did not return a new mtime: {save_payload}")
        on_disk = pathlib.Path(warmup_file).read_text(encoding="utf-8")
        if on_disk != edited_yaml:
            raise AssertionError(f"config-yaml save did not write the exact decoded content to disk: {on_disk!r}")

        # 3. Validation failure: syntactically valid YAML, but an unparsable
        # time expression -- must be rejected (400, {error, line, column})
        # and must NOT touch the file on disk.
        bad_yaml = (
            "defaults:\n"
            "  time: \"not-a-time\"\n"
            "  model: auto\n"
            "accounts: {}\n"
        )
        encoded_bad = base64.urlsafe_b64encode(bad_yaml.encode("utf-8")).rstrip(b"=").decode("ascii")
        bad_status, _, bad_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(
                    f"/v0/resource/plugins/{plugin_id}/config-yaml/save",
                    {"content": encoded_bad, "mtime": new_mtime},
                ),
            )
        )
        if bad_status != 400:
            raise AssertionError(f"config-yaml save (invalid time expr): status={bad_status}, body={bad_body!r}")
        bad_payload = json.loads(bad_body)
        if not bad_payload.get("error") or "line" not in bad_payload or "column" not in bad_payload:
            raise AssertionError(f"config-yaml save (invalid time expr) missing error/line/column: {bad_payload}")
        still_on_disk = pathlib.Path(warmup_file).read_text(encoding="utf-8")
        if still_on_disk != edited_yaml:
            raise AssertionError("config-yaml save must not write to disk when validation fails")

        # Bonus: a stale mtime (the *original* one, now superseded by the
        # save above) on otherwise-valid content is rejected as a conflict.
        stale_status, _, stale_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(
                    f"/v0/resource/plugins/{plugin_id}/config-yaml/save",
                    {"content": encoded_content, "mtime": mtime},
                ),
            )
        )
        if stale_status != 409:
            raise AssertionError(f"config-yaml save (stale mtime) expected 409, got {stale_status} body={stale_body!r}")

        # The v0.3.0 top-level time/model/accounts shape (v3InlineMode) must
        # still register cleanly, be detected as inline (not legacy), and
        # keep its own overrides.json-backed /set behavior working exactly
        # as it did in v0.3.0.
        v3_reconfigured = invoke(plugin, "plugin.reconfigure", register_request(V3_INLINE_CONFIG))
        if v3_reconfigured["metadata"]["Name"] != plugin_id:
            raise AssertionError("reconfigure (v3 inline) returned unexpected metadata")
        status, _, body = decode_management(
            invoke(plugin, "management.handle", management_request(f"/v0/resource/plugins/{plugin_id}/status"))
        )
        payload = json.loads(body)
        if payload["config"].get("mode") != "inline" or payload["config"].get("legacy_mode"):
            raise AssertionError(f"expected mode=inline legacy_mode=false for v3 inline config: {payload['config']}")

        # A valid scope=global set: there is no real CPA at the configured
        # base-url in this sandboxed test, so the GET /v1/models precheck
        # itself fails -- per spec that must not block saving, only add a
        # warning to the response.
        set_status, _, set_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(
                    f"/v0/resource/plugins/{plugin_id}/set",
                    {"scope": "global", "model": "gemini-3.7-flash-high", "lang": "zh-TW"},
                ),
            )
        )
        if set_status != 200:
            raise AssertionError(f"expected 200 for a global set, got {set_status} body={set_body!r}")
        set_payload = json.loads(set_body)
        if set_payload.get("lang") != "zh-TW" or not set_payload.get("warning"):
            raise AssertionError(f"expected a precheck-failed warning with lang=zh-TW: {set_payload}")

        # model=auto clears the override rather than failing.
        set_status, _, set_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/set", {"scope": "global", "model": "auto"}),
            )
        )
        if set_status != 200:
            raise AssertionError(f"expected 200 clearing the global override, got {set_status} body={set_body!r}")

        # The legacy (pre-v0.3.0) config shape must still register cleanly,
        # be detected as legacy in the status JSON (mode=inline,
        # legacy_mode=true), and /set must be gated off entirely (501).
        legacy_reconfigured = invoke(plugin, "plugin.reconfigure", register_request(LEGACY_CONFIG))
        if legacy_reconfigured["metadata"]["Name"] != plugin_id:
            raise AssertionError("reconfigure (legacy) returned unexpected metadata")
        status, _, body = decode_management(
            invoke(plugin, "management.handle", management_request(f"/v0/resource/plugins/{plugin_id}/status"))
        )
        payload = json.loads(body)
        if payload["config"].get("mode") != "inline" or not payload["config"].get("legacy_mode"):
            raise AssertionError(f"expected mode=inline legacy_mode=true after reconfiguring with the legacy shape: {payload['config']}")

        set_status, _, set_body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/set", {"scope": "global", "model": "x"}),
            )
        )
        if set_status != 501:
            raise AssertionError(f"expected 501 for /set under the legacy config, got {set_status} body={set_body!r}")

        invoke(plugin, "plugin.quiesce", {})

        invoke(plugin, "plugin.shutdown", {})
        status, _, body = decode_management(
            invoke(
                plugin,
                "management.handle",
                management_request(f"/v0/resource/plugins/{plugin_id}/status"),
            )
        )
        if status != 503:
            raise AssertionError(f"expected 503 after shutdown, got status={status} body={body!r}")

        print(
            "ABI integration passed: "
            f"schema={registration['schema_version']}, plugin={plugin_id} "
            f"v{registration['metadata']['Version']}"
        )
    finally:
        plugin.shutdown()


if __name__ == "__main__":
    main()
